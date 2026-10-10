// Strata in its own window (Tauri): the same job as desktop/electron, with the system's WebView2 instead of a bundled
// Chromium.  It starts the model if it is not running (and shows how the loading goes), opens the chat page, keeps a
// tray icon, and stops the model again when you quit.  The page itself is the server's: this is a window around
// http://127.0.0.1:8080.  Settings: %APPDATA%\Strata\desktop.json (shared with the Electron app; written on first run).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_opener::OpenerExt;

const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Clone, Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase")]
struct Config {
    url: String,
    start_server: bool,
    launcher: String,
    stopper: String,
    console_log: String,
    stop_on_quit: bool,
    close_to_tray: bool,
    load_timeout_minutes: u64,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            url: "http://127.0.0.1:8080".into(),
            start_server: true,
            launcher: r"J:\Strata-work\run-strata.ps1".into(),
            stopper: r"J:\Strata-work\Stop-Strata.bat".into(),
            console_log: r"J:\Strata-work\strata-console.txt".into(),
            stop_on_quit: true,
            close_to_tray: true,
            load_timeout_minutes: 10,
        }
    }
}

struct State {
    cfg: Config,
    cfg_path: PathBuf,
    child: Mutex<Option<Child>>,
    started_by_us: AtomicBool,
    quitting: AtomicBool,
    generation: AtomicU64, // a newer "open the chat" ends the older waiting loop
}

#[derive(Clone, Serialize)]
struct Status {
    state: &'static str, // loading | waiting | failed
    secs: u64,
    lines: Vec<String>,
}

// ------------------------------------------------------------------ settings and the log
fn config_path() -> PathBuf {
    if let Ok(p) = std::env::var("STRATA_DESKTOP_CONFIG") {
        return PathBuf::from(p);
    }
    let base = std::env::var("APPDATA").unwrap_or_else(|_| ".".into());
    PathBuf::from(base).join("Strata").join("desktop.json")
}

fn load_config(path: &PathBuf) -> Config {
    match std::fs::read_to_string(path).ok().and_then(|t| serde_json::from_str::<Config>(t.trim_start_matches('\u{feff}')).ok()) {
        Some(c) => c,
        None => {
            let c = Config::default();
            if let Some(dir) = path.parent() {
                let _ = std::fs::create_dir_all(dir);
            }
            if !path.exists() {
                let _ = std::fs::write(path, serde_json::to_string_pretty(&c).unwrap_or_default());
            }
            c
        }
    }
}

fn log(state: &State, msg: &str) {
    if let Some(dir) = state.cfg_path.parent() {
        let _ = std::fs::create_dir_all(dir);
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(dir.join("desktop-tauri.log")) {
            let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
            let _ = writeln!(f, "{} {}", secs, msg);
        }
    }
}

fn tail_log(path: &str, n: usize) -> Vec<String> {
    let Ok(mut f) = std::fs::File::open(path) else { return vec![] };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let take = len.min(16384);
    let mut buf = vec![0u8; take as usize];
    use std::io::Seek;
    if f.seek(std::io::SeekFrom::Start(len - take)).is_err() || f.read_exact(&mut buf).is_err() {
        return vec![];
    }
    let text = String::from_utf8_lossy(&buf).to_string();
    let lines: Vec<String> = text.lines().filter(|l| !l.trim().is_empty()).map(|l| l.chars().take(160).collect()).collect();
    lines[lines.len().saturating_sub(n)..].to_vec()
}

// ------------------------------------------------------------------ the server
// http://host:port -> (host:port, host)
fn host_port(url: &str) -> Option<(String, String)> {
    let rest = url.strip_prefix("http://")?;
    let hostport = rest.split('/').next()?.to_string();
    let host = hostport.split(':').next()?.to_string();
    Some((if hostport.contains(':') { hostport } else { format!("{}:80", hostport) }, host))
}

// GET /v1/status: true when it answers 200 and says the model is not "loaded": false
fn probe(url: &str) -> bool {
    let Some((addr, host)) = host_port(url) else { return false };
    let Some(sock) = addr.to_socket_addrs().ok().and_then(|mut a| a.next()) else { return false };
    let Ok(mut s) = TcpStream::connect_timeout(&sock, Duration::from_secs(2)) else { return false };
    let _ = s.set_read_timeout(Some(Duration::from_secs(2)));
    let req = format!("GET /v1/status HTTP/1.0\r\nHost: {}\r\nConnection: close\r\n\r\n", host);
    if s.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut body = Vec::new();
    let _ = s.take(65536).read_to_end(&mut body);
    let text = String::from_utf8_lossy(&body);
    (text.starts_with("HTTP/1.0 200") || text.starts_with("HTTP/1.1 200")) && !text.replace(' ', "").contains("\"loaded\":false")
}

fn start_server(state: &State) {
    let mut child = state.child.lock().unwrap();
    if child.is_some() || !state.cfg.start_server {
        log(state, "not starting the server (already started, or startServer is off)");
        return;
    }
    if !std::path::Path::new(&state.cfg.launcher).exists() {
        log(state, &format!("the launcher does not exist: {}", state.cfg.launcher));
        return;
    }
    match Command::new("powershell.exe")
        .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", &state.cfg.launcher])
        .env("STRATA_NO_OPEN", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
    {
        Ok(c) => {
            log(state, &format!("started the launcher, pid {}", c.id()));
            state.started_by_us.store(true, Ordering::SeqCst);
            *child = Some(c);
        }
        Err(e) => log(state, &format!("the launcher failed to start: {}", e)),
    }
}

fn stop_server(state: &State) {
    if std::path::Path::new(&state.cfg.stopper).exists() {
        let _ = Command::new("cmd.exe").args(["/c", &state.cfg.stopper]).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW).status();
    }
    *state.child.lock().unwrap() = None;
    state.started_by_us.store(false, Ordering::SeqCst);
}

// ------------------------------------------------------------------ the window
fn navigate(app: &AppHandle, url: &str) {
    if let Some(w) = app.get_webview_window("main") {
        if let Ok(u) = url.parse() {
            let _ = w.navigate(u);
        }
    }
}

fn open_chat(app: &AppHandle) {
    let state = app.state::<State>();
    let gen = state.generation.fetch_add(1, Ordering::SeqCst) + 1;
    let app = app.clone();
    std::thread::spawn(move || {
        let state = app.state::<State>();
        let url = state.cfg.url.clone();
        log(&state, &format!("opening {}", url));
        if probe(&url) {
            navigate(&app, &url);
            return;
        }
        // not up: the loading screen, the launcher, then wait for the server
        if let Some(w) = app.get_webview_window("main") {
            let _ = w.eval("if (!location.href.includes('tauri.localhost') && !location.href.startsWith('tauri://')) location.replace('http://tauri.localhost/');");
        }
        start_server(&state);
        let started = Instant::now();
        loop {
            std::thread::sleep(Duration::from_millis(1500));
            if state.generation.load(Ordering::SeqCst) != gen {
                return; // a newer open_chat took over
            }
            if probe(&url) {
                navigate(&app, &url);
                return;
            }
            let secs = started.elapsed().as_secs();
            if secs > state.cfg.load_timeout_minutes * 60 {
                let _ = app.emit("status", Status { state: "failed", secs, lines: tail_log(&state.cfg.console_log, 14) });
                return;
            }
            let st = if state.cfg.start_server { "loading" } else { "waiting" };
            let _ = app.emit("status", Status { state: st, secs, lines: tail_log(&state.cfg.console_log, 8) });
        }
    });
}

fn show_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn open_path(app: &AppHandle, path: &str) {
    if std::path::Path::new(path).exists() {
        let _ = app.opener().open_path(path, None::<&str>);
    }
}

#[tauri::command]
fn retry(app: AppHandle) {
    open_chat(&app);
}

#[tauri::command]
fn open_log(app: AppHandle) {
    let state = app.state::<State>();
    open_path(&app, &state.cfg.console_log);
}

#[tauri::command]
fn quit_app(app: AppHandle) {
    app.exit(0);
}

fn main() {
    let cfg_path = config_path();
    let cfg = load_config(&cfg_path);
    let origin = cfg.url.trim_end_matches('/').to_string();

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_window(app)))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_opener::init())
        .manage(State {
            cfg,
            cfg_path,
            child: Mutex::new(None),
            started_by_us: AtomicBool::new(false),
            quitting: AtomicBool::new(false),
            generation: AtomicU64::new(0),
        })
        .invoke_handler(tauri::generate_handler![retry, open_log, quit_app])
        .setup(move |app| {
            let handle = app.handle().clone();
            let allowed = origin.clone();
            // the window only ever shows the loading screen or Strata's own page: anything else opens in the browser
            let nav_handle = handle.clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Strata")
                .inner_size(1280.0, 860.0)
                .min_inner_size(720.0, 520.0)
                .on_navigation(move |url| {
                    let s = url.as_str();
                    if s.starts_with(&allowed) || url.host_str() == Some("tauri.localhost") || url.scheme() == "tauri" {
                        return true;
                    }
                    if url.scheme() == "http" || url.scheme() == "https" {
                        let _ = nav_handle.opener().open_url(s, None::<&str>);
                    }
                    false
                })
                .build()?;

            // the tray
            let show = MenuItem::with_id(app, "show", "Show Strata", true, None::<&str>)?;
            let restart = MenuItem::with_id(app, "restart", "Restart the model", true, None::<&str>)?;
            let stop = MenuItem::with_id(app, "stop", "Stop the model", true, None::<&str>)?;
            let log_item = MenuItem::with_id(app, "log", "Open the console log", true, None::<&str>)?;
            let settings = MenuItem::with_id(app, "settings", "Open the settings file", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let sep1 = PredefinedMenuItem::separator(app)?;
            let sep2 = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(app, &[&show, &sep1, &restart, &stop, &log_item, &settings, &sep2, &quit])?;
            let mut tray = TrayIconBuilder::new()
                .tooltip("Strata")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => show_window(app),
                    "restart" => {
                        let app = app.clone();
                        std::thread::spawn(move || {
                            stop_server(&app.state::<State>());
                            show_window(&app);
                            open_chat(&app);
                        });
                    }
                    "stop" => {
                        let app = app.clone();
                        std::thread::spawn(move || stop_server(&app.state::<State>()));
                    }
                    "log" => {
                        let state = app.state::<State>();
                        open_path(app, &state.cfg.console_log);
                    }
                    "settings" => {
                        let state = app.state::<State>();
                        open_path(app, &state.cfg_path.to_string_lossy());
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                        show_window(tray.app_handle());
                    }
                });
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;

            open_chat(&handle);
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let state = window.app_handle().state::<State>();
                if !state.quitting.load(Ordering::SeqCst) && state.cfg.close_to_tray {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building the Strata desktop app");

    app.run(|app, event| {
        if let RunEvent::ExitRequested { api, .. } = event {
            let state = app.state::<State>();
            if state.quitting.swap(true, Ordering::SeqCst) {
                return; // the second time round: really exit
            }
            if state.started_by_us.load(Ordering::SeqCst) && state.cfg.stop_on_quit {
                api.prevent_exit(); // stop the model this app started, then exit
                let app = app.clone();
                std::thread::spawn(move || {
                    stop_server(&app.state::<State>());
                    app.exit(0);
                });
            }
        }
    });
}
