const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('desktop', {
  connection: () => ipcRenderer.invoke('core:connection'),
  takePhoto: (options) => ipcRenderer.invoke('camera:photo', options),
  cancelPhoto: () => ipcRenderer.invoke('camera:cancel-photo'),
  setTestPhoto: (image) => ipcRenderer.invoke('camera:test-photo', image),
  diagnostic: (event, detail) => ipcRenderer.invoke('diagnostics:write', event, detail),
  focus: (enabled) => ipcRenderer.invoke('focus:set', !!enabled),
  onFocus: (fn) => {
    const listener = (_, value) => fn(value);
    ipcRenderer.on('focus:changed', listener);
    return () => ipcRenderer.removeListener('focus:changed', listener);
  },
  onSuspend: (fn) => {
    const listener = (_, reason) => fn(reason);
    ipcRenderer.on('media:suspend', listener);
    return () => ipcRenderer.removeListener('media:suspend', listener);
  },
  setFinalizing: (value) => {
    ipcRenderer.sendSync('finalization:guard-sync', !!value);
    return Promise.resolve();
  },
  saveKeys: (keys) => ipcRenderer.invoke('keys:save', keys),
  board: (html, bounds) => ipcRenderer.invoke('board:show', html, bounds),
  closeBoard: () => ipcRenderer.invoke('board:close'),
  resizeBoard: (bounds) => ipcRenderer.invoke('board:bounds', bounds),
});
