# Strata desktop (Tauri)

The same app as [`../electron`](../electron), built with Tauri 2: the window is the system's Edge WebView2 instead of a
bundled Chromium, and the shell is Rust. Strata is [Niko1221/Strata](https://github.com/Niko1221/Strata) (MIT, see
`LICENSE`); this app only holds a window around it.

Same behaviour: opens `http://127.0.0.1:8080`; if the model is not running it starts it and shows the loading screen (the
last lines of the console log); a tray icon (Show, Restart the model, Stop the model, Open the console log, Open the
settings file, Quit); the window's X hides it to the tray; Quit stops the model the app started; one window; window size
and place remembered; links to other sites open in the browser.

Settings are the same file as the Electron app's, `%APPDATA%\Strata\desktop.json` (see `../electron/README.md` for the
keys); the log is `desktop-tauri.log` next to it. `STRATA_DESKTOP_CONFIG=<file>` uses another settings file.

## Size and memory (this PC, measured)

| | Electron | Tauri |
| --- | --- | --- |
| installer | 111 MB | 1.2 MB |
| installed on disk | 369 MB (its own Chromium) | about 3.5 MB (the exe; it uses Windows' WebView2) |
| memory while showing the chat | about 106 MB private | about 120 MB private (the app is 5 MB of it, the rest is WebView2's processes) |

The saving is on disk, not in memory: the page is rendered by a separate WebView2 process in both.

## Safety

The chat page is a remote origin to the app: it gets no access to the app's commands (checked: `retry`, `quit_app`,
`open_log` and the event listener are all "not allowed by ACL" from the page). Only the local loading screen may listen
for progress and press its three buttons. Navigation away from Strata's own origin opens in the browser instead.

## Build

```
npm install
npm run build        # src-tauri/target/release/bundle/nsis/Strata Tauri_<version>_x64-setup.exe (needs Rust: https://rustup.rs)
```

The first build compiles about 400 crates (a few minutes); the next ones are fast. The installer is per user and is named
"Strata Tauri", so it can sit beside the Electron app's "Strata".
