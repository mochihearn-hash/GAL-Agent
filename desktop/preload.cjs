// 页面唯一的桌面能力入口。只公开具名方法，不公开 ipcRenderer、文件系统或任意命令执行。
// 参数和调用来源还会在 main.cjs 再次验证，不能把前端校验当作权限边界。
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('gal', {
  bootstrap: () => ipcRenderer.invoke('gal:bootstrap'),
  api: (method, path, body) => ipcRenderer.invoke('gal:api', method, path, body),
  onEvent: (callback) => {
    // 返回取消订阅函数，React 组件卸载时必须调用，避免重复事件监听和失效页面回调。
    const listener = (_, value) => callback(value);
    ipcRenderer.on('gal:event', listener);
    return () => ipcRenderer.removeListener('gal:event', listener);
  },
  showOverlay: () => ipcRenderer.invoke('gal:overlay'),
  setOverlayInteractive: (value) => ipcRenderer.invoke('gal:overlay-interactive', Boolean(value)),
  setOverlayCollapsed: (value) => ipcRenderer.invoke('gal:overlay-collapsed', Boolean(value)),
  hideOverlay: () => ipcRenderer.invoke('gal:overlay-hide'),
  exportSession: (id) => ipcRenderer.invoke('gal:export', id),
  close: () => ipcRenderer.invoke('gal:close')
});
