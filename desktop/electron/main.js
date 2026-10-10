'use strict';
// Strata in its own window: starts the model if it is not running (and shows how the loading goes), opens the chat
// page, keeps a tray icon, and stops the model again when you quit.  The page itself is the server's: this shell only
// holds a window around http://127.0.0.1:8080.  Settings: <userData>/desktop.json (written with the defaults on first run).
const { app, BrowserWindow, Tray, Menu, shell, nativeImage, ipcMain, session } = require('electron');
const path = require('path');
const fs = require('fs');
const http = require('http');
const { spawn, execFile } = require('child_process');

const SMOKE = process.argv.includes('--smoke');          // load the page, save a picture of it, exit (a check, no tray)
const DEFAULTS = {
  url: 'http://127.0.0.1:8080',
  startServer: true,                                     // start the model when it is not running
  launcher: 'J:\\Strata-work\\run-strata.ps1',           // what starts it (PowerShell script)
  stopper: 'J:\\Strata-work\\Stop-Strata.bat',           // what stops it
  consoleLog: 'J:\\Strata-work\\strata-console.txt',     // shown while it loads
  stopOnQuit: true,                                      // quitting stops the model the app started
  closeToTray: true,                                     // the window's X hides it; Quit in the tray menu ends the app
  loadTimeoutMinutes: 10,
  window: { width: 1280, height: 860 },
};

let cfg = { ...DEFAULTS };
let cfgPath = null;
let win = null, tray = null;
let child = null;                                        // the launcher this app started
let startedByUs = false;
let quitting = false;
let loadTimer = null;

// a small log next to the settings (desktop.log): what the app tried, for when something does not start
function log(msg) {
  try {
    const f = path.join(path.dirname(cfgPath || app.getPath('userData')), 'desktop.log');
    fs.mkdirSync(path.dirname(f), { recursive: true });
    fs.appendFileSync(f, new Date().toISOString() + ' ' + msg + '\n');
  } catch (e) { /* ignore */ }
}

// ------------------------------------------------------------------ settings
function loadConfig() {
  cfgPath = process.env.STRATA_DESKTOP_CONFIG || path.join(app.getPath('userData'), 'desktop.json');   // (the variable: for tests)
  try {
    cfg = { ...DEFAULTS, ...JSON.parse(fs.readFileSync(cfgPath, 'utf8')) };
    cfg.window = { ...DEFAULTS.window, ...(cfg.window || {}) };
  } catch (e) {
    cfg = { ...DEFAULTS };
    saveConfig();
  }
}
function saveConfig() {
  try {
    fs.mkdirSync(path.dirname(cfgPath), { recursive: true });
    fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));
  } catch (e) { /* the window still works without it */ }
}
const appOrigin = () => new URL(cfg.url).origin;

// ------------------------------------------------------------------ the server
function probe() {
  return new Promise((resolve) => {
    const req = http.get(cfg.url + '/v1/status', { timeout: 2000 }, (res) => {
      let body = '';
      res.on('data', (d) => { body += d; });
      res.on('end', () => {
        try { resolve(res.statusCode === 200 && JSON.parse(body).loaded !== false); } catch (e) { resolve(false); }
      });
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}
function tailLog(lines = 8) {
  try {
    const stat = fs.statSync(cfg.consoleLog);
    const fd = fs.openSync(cfg.consoleLog, 'r');
    const size = Math.min(stat.size, 16384);
    const buf = Buffer.alloc(size);
    fs.readSync(fd, buf, 0, size, stat.size - size);
    fs.closeSync(fd);
    return buf.toString('utf8').split(/\r?\n/).filter((l) => l.trim()).slice(-lines).map((l) => l.slice(0, 160));
  } catch (e) { return []; }
}
function startServer() {
  if (child || !cfg.startServer) { log(`not starting the server (child ${!!child}, startServer ${cfg.startServer})`); return; }
  if (!fs.existsSync(cfg.launcher)) { log('the launcher does not exist: ' + cfg.launcher); return; }
  child = spawn('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', cfg.launcher], {
    env: { ...process.env, STRATA_NO_OPEN: '1' }, windowsHide: true, stdio: 'ignore',      // (not detached: with no console at all PowerShell exits at once)
  });
  startedByUs = true;
  log('started the launcher, pid ' + child.pid);
  child.on('error', (e) => { log('the launcher failed to start: ' + e.message); child = null; });
  child.on('exit', (code) => { log('the launcher exited, code ' + code); child = null; });
  child.unref();
}
function stopServer() {
  return new Promise((resolve) => {
    if (!fs.existsSync(cfg.stopper)) { resolve(); return; }
    execFile('cmd.exe', ['/c', cfg.stopper], { windowsHide: true, timeout: 30000 }, () => { child = null; startedByUs = false; resolve(); });
  });
}

// ------------------------------------------------------------------ the window
function send(channel, payload) { if (win && !win.isDestroyed()) win.webContents.send(channel, payload); }

async function openChat() {
  clearInterval(loadTimer);
  log('opening ' + cfg.url + ' (settings: ' + cfgPath + ')');
  const started = Date.now();
  if (await probe()) { return loadPage(); }
  win.loadFile(path.join(__dirname, 'loading.html'));
  startServer();
  loadTimer = setInterval(async () => {
    const secs = Math.round((Date.now() - started) / 1000);
    if (await probe()) { clearInterval(loadTimer); loadPage(); return; }
    if (secs > cfg.loadTimeoutMinutes * 60) {
      clearInterval(loadTimer);
      send('status', { state: 'failed', secs, lines: tailLog(14), started: !!child });
      return;
    }
    send('status', { state: cfg.startServer ? 'loading' : 'waiting', secs, lines: tailLog(), started: !!child });
  }, 1500);
}
function loadPage() { win.loadURL(cfg.url); }

function createWindow() {
  const b = cfg.window;
  win = new BrowserWindow({
    width: b.width, height: b.height, x: b.x, y: b.y, minWidth: 720, minHeight: 520, show: false,
    backgroundColor: '#0e1113', title: 'Strata', autoHideMenuBar: true,
    icon: path.join(__dirname, 'build', 'icon.png'),
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  if (b.maximized) win.maximize();
  win.once('ready-to-show', () => { if (!SMOKE) win.show(); });

  // the window only ever shows Strata's own page (or the loading screen): anything else opens in the browser
  const external = (url) => { try { const u = new URL(url); if (u.protocol === 'http:' || u.protocol === 'https:') shell.openExternal(url); } catch (e) { /* ignore */ } };
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(appOrigin())) { setImmediate(() => win.loadURL(url)); } else { external(url); }
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e, url) => {
    if (!url.startsWith(appOrigin()) && !url.startsWith('file://')) { e.preventDefault(); external(url); }
  });

  let saveTimer = null;
  const remember = () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      if (!win || win.isDestroyed() || win.isMinimized()) return;
      cfg.window = { ...win.getNormalBounds(), maximized: win.isMaximized() };
      saveConfig();
    }, 500);
  };
  win.on('resize', remember);
  win.on('move', remember);
  win.on('close', (e) => {
    if (!quitting && cfg.closeToTray && tray) {
      e.preventDefault();
      win.hide();
      if (!global.hintShown) { global.hintShown = true; tray.displayBalloon({ title: 'Strata is still running', content: 'It stays in the tray. Right-click the icon to quit.' }); }
    }
  });
  openChat();
}

function showWindow() {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

// ------------------------------------------------------------------ tray
function createTray() {
  tray = new Tray(nativeImage.createFromPath(path.join(__dirname, 'build', 'icon.png')).resize({ width: 16, height: 16 }));
  tray.setToolTip('Strata');
  const menu = Menu.buildFromTemplate([
    { label: 'Show Strata', click: showWindow },
    { type: 'separator' },
    { label: 'Restart the model', click: async () => { await stopServer(); showWindow(); openChat(); } },
    { label: 'Stop the model', click: async () => { await stopServer(); } },
    { label: 'Open the console log', click: () => { if (fs.existsSync(cfg.consoleLog)) shell.openPath(cfg.consoleLog); } },
    { label: 'Open the settings file', click: () => shell.openPath(cfgPath) },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ]);
  tray.setContextMenu(menu);
  tray.on('click', showWindow);
}

function createMenu() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: 'View', submenu: [
      { role: 'reload' }, { role: 'forceReload' }, { type: 'separator' },
      { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { type: 'separator' }, { role: 'togglefullscreen' },
      ...(process.env.STRATA_DESKTOP_DEBUG ? [{ type: 'separator' }, { role: 'toggleDevTools' }] : []),
    ] },
  ]));
}

// ------------------------------------------------------------------ the app
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', showWindow);
  app.whenReady().then(() => {
    loadConfig();
    // nothing the page asks of the browser (camera, location, notifications ...) is granted; copying text is
    session.defaultSession.setPermissionRequestHandler((wc, permission, cb) => cb(permission === 'clipboard-sanitized-write'));
    createMenu();
    createWindow();
    if (!SMOKE) createTray();
    ipcMain.on('retry', () => openChat());
    ipcMain.on('open-log', () => { if (fs.existsSync(cfg.consoleLog)) shell.openPath(cfg.consoleLog); });
    ipcMain.on('quit', () => app.quit());
    if (SMOKE) smoke();
  });
  app.on('before-quit', (e) => {
    if (quitting) return;
    quitting = true;
    if (startedByUs && cfg.stopOnQuit) {                  // stop the model this app started, then really quit
      e.preventDefault();
      stopServer().finally(() => app.quit());
    }
  });
  app.on('window-all-closed', () => { if (SMOKE || !tray) app.quit(); });
}

// a check: wait for the page, save a picture and what the page says about itself, exit
async function smoke() {
  const out = process.env.STRATA_DESKTOP_SMOKE_OUT || path.join(app.getPath('temp'), 'strata-desktop-smoke');
  const done = (code, info) => { try { fs.writeFileSync(out + '.json', JSON.stringify(info, null, 2)); } catch (e) { /* ignore */ } app.exit(code); };
  win.webContents.on('did-finish-load', () => {
    if (!win.webContents.getURL().startsWith(appOrigin())) return;      // still the loading screen: wait for the chat page
    setTimeout(async () => {
      try {
        const url = win.webContents.getURL();
        const info = { url, title: win.getTitle(), isChatPage: url.startsWith(appOrigin()) };
        if (info.isChatPage) info.page = await win.webContents.executeJavaScript(
          '({chatTop: !document.getElementById("chat-top").hidden, sessions: !!document.getElementById("sessions"), pinned: typeof pinned})');
        const img = await win.webContents.capturePage();
        fs.writeFileSync(out + '.png', img.toPNG());
        done(info.isChatPage ? 0 : 2, info);
      } catch (e) { done(3, { error: String(e) }); }
    }, 4000);
  });
  setTimeout(() => done(4, { error: 'timed out', url: win.webContents.getURL(), log: tailLog() }), 150000);
}
