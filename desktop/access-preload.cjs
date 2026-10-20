'use strict';
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('accessForm', Object.freeze({
  save: token => ipcRenderer.invoke('violet:access-save', token),
  cancel: () => ipcRenderer.send('violet:access-cancel')
}));
