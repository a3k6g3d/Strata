'use strict';
// The loading screen's only link to the app: it listens for progress and can ask for a retry, the log, or a quit.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('strataDesktop', {
  onStatus: (cb) => ipcRenderer.on('status', (_e, s) => cb(s)),
  retry: () => ipcRenderer.send('retry'),
  openLog: () => ipcRenderer.send('open-log'),
  quit: () => ipcRenderer.send('quit'),
});
