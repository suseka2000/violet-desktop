'use strict';
const { contextBridge, ipcRenderer } = require('electron');
// No filesystem, shell, arbitrary IPC or backend token is exposed to the page.
contextBridge.exposeInMainWorld('violetDesktop', Object.freeze({
  info: () => ipcRenderer.invoke('violet:info'),
  checkUpdates: () => ipcRenderer.invoke('violet:check-updates')
}));
