// Electron 主进程负责启动后端、管理窗口、转发受限 IPC，并持有本地控制 token。
// 页面不直接接触 Node.js、系统命令或后台端口；游戏捕获和输入由 Python 后端完成。
const { app, BrowserWindow, ipcMain, dialog, screen } = require('electron');
const { spawn } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const path = require('node:path');
const fs = require('node:fs/promises');
const { allowedPath } = require('./policy.cjs');

let mainWindow, overlay, backend, port, heartbeat, socket, quitting = false, reconnectTimer;
let startupError = '', activeId = null;
let overlayCollapsed = false, overlayHeight = 610;
const token = randomBytes(32).toString('hex');
// 应用名称保持固定，使不同发布版本继续读取同一用户数据目录；测试可通过环境变量隔离。
app.setName('GAL Agent');
const root = path.resolve(__dirname, '..');
const development = !app.isPackaged;
if (process.env.GAL_AGENT_SHELL_DATA_DIR) app.setPath('userData', process.env.GAL_AGENT_SHELL_DATA_DIR);

function emit(value) {
  // 同一后台事件同时通知主窗口和悬浮窗，让两处运行状态和当前会话保持一致。
  for (const win of [mainWindow, overlay]) if (win && !win.isDestroyed()) win.webContents.send('gal:event', value);
}
function trusted(event) {
  // 只接受应用自己创建的 webContents；不能仅凭 IPC 通道名称判定调用者可信。
  return [mainWindow, overlay].some(w => w && !w.isDestroyed() && w.webContents === event.sender);
}
async function request(method, route, body) {
  // HTTP 只由主进程发送，自动附加本次启动的 token；不会把 token 交给渲染层。
  // 普通控制请求与连接测试使用不同超时，返回图片时统一转成可显示的数据地址。
  if (!port) throw new Error(startupError || '后台尚未就绪');
  const response = await fetch(`http://127.0.0.1:${port}${route}`, {
    method, headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(route.endsWith('/test') ? 370000 : 15000)
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === 'string' ? error.detail : '操作未完成，请检查配置');
  }
  if (response.headers.get('content-type')?.startsWith('image/')) {
    return `data:image/png;base64,${Buffer.from(await response.arrayBuffer()).toString('base64')}`;
  }
  return response.json();
}
function listen() {
  // WebSocket 推送用于刷新状态，断线时重连；游戏操作是否允许仍取决于后台心跳和版本检查。
  if (quitting || !port) return;
  socket = new WebSocket(`ws://127.0.0.1:${port}/events`, ['gal-agent', token]);
  socket.addEventListener('message', event => {
    try {
      const data = JSON.parse(event.data);
      if (data.type === 'status') activeId = data.data.session_id;
      emit(data);
    } catch {}
  });
  socket.addEventListener('error', () => {});
  socket.addEventListener('close', () => {
    if (!quitting) {
      emit({ type: 'connection_error', message: '后台连接已断开，Agent 已停止；正在尝试恢复连接' });
      reconnectTimer = setTimeout(listen, 1500);
    }
  });
}
async function registerWindows() {
  // Windows 原生句柄按实际指针宽度读取，交给后台排除主界面和悬浮窗，避免误选自己。
  const hwnds = [mainWindow, overlay].filter(w => w && !w.isDestroyed()).map(w => {
    const buffer = w.getNativeWindowHandle();
    return buffer.length >= 8 ? Number(buffer.readBigUInt64LE()) : buffer.readUInt32LE();
  });
  await request('POST', '/control/windows', { pids: [process.pid], hwnds });
}
function startBackend() {
  // 开发模式运行虚拟环境 Python；打包模式使用自带后端，无需用户另装 Python。
  // 用户数据路径与程序资源分开，升级程序目录不会覆盖现有剧情记录。
  return new Promise((resolve, reject) => {
    const executable = development ? (process.env.GAL_AGENT_PYTHON || path.join(root, '.venv', 'Scripts', 'python.exe')) : path.join(process.resourcesPath, 'backend', 'gal-backend.exe');
    const args = development ? [path.join(root, 'backend', 'launcher.py')] : [];
    backend = spawn(executable, args, {
      cwd: development ? path.join(root, 'backend') : process.resourcesPath,
      windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, GAL_AGENT_TOKEN: token, GAL_AGENT_PARENT_PID: String(process.pid),
        GAL_AGENT_DATA_DIR: process.env.GAL_AGENT_DATA_DIR || (development ? path.join(root, 'data') : path.join(app.getPath('userData'), 'data')) }
    });
    // 标准输出可能分多段到达，按行积累后解析；只在收到 ready + 合法端口时认为启动成功。
    let output = '', settled = false;
    const timer = setTimeout(() => {
      if (!settled) { settled = true; backend.kill(); reject(new Error('后台启动超时，请检查独立 Python 环境或安装包')); }
    }, 30000);
    backend.stdout.on('data', chunk => {
      output += chunk.toString();
      const lines = output.split('\n'); output = lines.pop();
      for (const line of lines) {
        try {
          const item = JSON.parse(line);
          if (item.ready && Number.isInteger(item.port) && !settled) {
            settled = true; clearTimeout(timer); port = item.port; resolve();
          }
        } catch {}
      }
    });
    // Do not relay backend stderr, which may include provider diagnostics.
    backend.stderr.on('data', () => {});
    backend.on('error', () => {
      clearTimeout(timer);
      if (!settled) { settled = true; reject(new Error('无法启动后台，请先运行项目初始化脚本')); }
    });
    backend.on('exit', () => {
      clearTimeout(timer);
      if (!settled) { settled = true; reject(new Error('后台启动失败，请检查依赖安装')); }
      if (!quitting) { port = null; emit({ type: 'connection_error', message: '后台已退出，输入已停止。请关闭后重新打开应用。' }); }
    });
  });
}
function createWindow(isOverlay = false) {
  // 主界面和悬浮窗加载同一 React 页面，通过 #overlay 区分。
  // 悬浮窗默认置顶但不可获取键盘焦点，按钮可操作而游戏仍能保持前台。
  const area = screen.getPrimaryDisplay().workArea;
  const win = new BrowserWindow({
    width: isOverlay ? 380 : 1320, height: isOverlay ? overlayHeight : 860,
    minWidth: isOverlay ? 340 : 1000, minHeight: isOverlay ? 230 : 680,
    x: isOverlay ? area.x + area.width - 400 : undefined, y: isOverlay ? area.y + 24 : undefined,
    title: 'GAL Agent · 剧情观测室', backgroundColor: isOverlay ? '#17232a' : '#f4f3ef',
    show: false, alwaysOnTop: isOverlay, focusable: !isOverlay, skipTaskbar: isOverlay,
    frame: !isOverlay,
    autoHideMenuBar: true,
    // 隔离页面与 Node.js；preload 只公开应用允许的有限接口。
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true }
  });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', event => event.preventDefault());
  if (process.env.GAL_AGENT_DEV_URL) {
    win.loadURL(process.env.GAL_AGENT_DEV_URL + (isOverlay ? '/#overlay' : '/'));
  } else {
    win.loadFile(path.join(root, 'frontend', 'dist', 'index.html'), isOverlay ? { hash: 'overlay' } : {});
  }
  win.once('ready-to-show', () => isOverlay ? win.showInactive() : win.show());
  if (isOverlay) {
    win.on('close', event => { if (!quitting) { event.preventDefault(); win.hide(); } });
  } else {
    win.on('close', event => { if (!quitting) { event.preventDefault(); app.quit(); } });
  }
  return win;
}

const ready = app.whenReady().then(async () => {
  // 先建立界面，再启动后端并登记自身窗口；心跳和事件订阅就绪后前端才能正常操作。
  mainWindow = createWindow();
  try {
    await startBackend();
    await registerWindows();
    await request('POST', '/control/heartbeat');
    heartbeat = setInterval(() => {
      request('POST', '/control/heartbeat').catch(() => emit({ type: 'connection_error', message: '后台连接不可用，Agent 将停止操作' }));
    }, 2000);
    listen();
  } catch (error) { startupError = error.message; }
});
ipcMain.handle('gal:bootstrap', async event => {
  if (!trusted(event)) throw new Error('未授权的窗口');
  await ready;
  return { ready: Boolean(port), error: startupError };
});
ipcMain.handle('gal:api', async (event, method, route, body) => {
  // 先检查调用者、路径白名单和请求体大小，再转发到固定本地服务。
  // route 不是可任意填写的 URL，渲染页面不能把此桥接用作通用网络代理。
  if (!trusted(event) || !allowedPath(method, route)) throw new Error('不允许的操作');
  if (body !== undefined && JSON.stringify(body).length > 20000) throw new Error('提交内容过长');
  await ready;
  try { return { ok: true, data: await request(method, route, body) }; }
  catch (error) { return { ok: false, error: error.message }; }
});
ipcMain.handle('gal:overlay', async event => {
  // 已存在时复用窗口并返回观看模式；尽量把悬浮窗摆到游戏旁边，避免覆盖点击区域。
  if (!trusted(event)) throw new Error('未授权的窗口');
  if (!overlay || overlay.isDestroyed()) { overlayCollapsed=false;overlay = createWindow(true); await registerWindows(); }
  else { overlay.setFocusable(false); overlay.showInactive(); }
  overlay.webContents.send('gal:event',{type:'overlay_mode',interactive:false,collapsed:overlayCollapsed});
  const current = await request('GET','/state');
  if(current.window){
    const [px,py,pw,ph]=current.window.frame;
    // Backend uses physical pixels; Electron placement uses display DIP units.
    const {x:gx,y:gy,width:gw,height:gh}=screen.screenToDipRect(null,{x:px,y:py,width:pw,height:ph});
    const bounds=overlay.getBounds();
    for(const {workArea:area} of screen.getAllDisplays()){
      const positions=[gx+gw+12,gx-bounds.width-12,area.x+12,area.x+area.width-bounds.width-12];
      const y=Math.max(area.y,Math.min(gy,area.y+area.height-bounds.height));
      const x=positions.find(x=>x>=area.x&&x+bounds.width<=area.x+area.width&&y+bounds.height<=area.y+area.height&&(x+bounds.width<=gx||x>=gx+gw||y+bounds.height<=gy||y>=gy+gh));
      if(x!==undefined){overlay.setPosition(x,y);break;}
    }
  }
  return true;
});
ipcMain.handle('gal:overlay-interactive', async (event, interactive) => {
  // 编辑引导前必须先暂停后台，再允许悬浮窗获取焦点；退出编辑不自动恢复托管。
  if (!trusted(event) || !overlay) return false;
  if (interactive && activeId) await request('POST', `/sessions/${activeId}/pause`);
  overlay.setFocusable(interactive);
  if (interactive) overlay.focus();
  overlay.webContents.send('gal:event',{type:'overlay_mode',interactive,collapsed:overlayCollapsed});
  return true;
});
ipcMain.handle('gal:overlay-collapsed', async (event, collapsed) => {
  // 收起只缩短窗口，固定控制栏仍保留；展开恢复原高度，并限制在显示器工作区内。
  if (!trusted(event) || !overlay) return false;
  if (collapsed && overlay.isFocusable() && activeId) await request('POST',`/sessions/${activeId}/pause`);
  overlay.setFocusable(false);
  if(collapsed!==overlayCollapsed){
    if(collapsed)overlayHeight=overlay.getBounds().height;
    overlayCollapsed=collapsed;
    const bounds=overlay.getBounds();
    const area=screen.getDisplayMatching(bounds).workArea;
    const height=collapsed?280:Math.min(overlayHeight,area.height);
    overlay.setBounds({...bounds,height,y:Math.max(area.y,Math.min(bounds.y,area.y+area.height-height))});
  }
  overlay.webContents.send('gal:event',{type:'overlay_mode',interactive:false,collapsed:overlayCollapsed});
  return true;
});
ipcMain.handle('gal:overlay-hide', async event => {
  if (!trusted(event) || !overlay) return false;
  overlay.setFocusable(false);overlay.hide();
  overlay.webContents.send('gal:event',{type:'overlay_mode',interactive:false,collapsed:overlayCollapsed});
  return true;
});
ipcMain.handle('gal:export', async (event, sid) => {
  // 保存路径由系统对话框交给用户选择，页面不能直接指定任意文件写入位置。
  if (!trusted(event) || !/^[a-f0-9]{32}$/.test(sid)) throw new Error('无效会话');
  const result = await request('GET', `/sessions/${sid}/export`);
  const selection = await dialog.showSaveDialog(mainWindow, { title: '导出会话记录', defaultPath: 'GAL-Agent-session.json', filters: [{ name: 'JSON 记录', extensions: ['json'] }] });
  if (!selection.canceled && selection.filePath) await fs.writeFile(selection.filePath, JSON.stringify(result, null, 2), 'utf8');
  return !selection.canceled;
});
ipcMain.handle('gal:close', event => { if (trusted(event)) app.quit(); });
app.on('before-quit', event => {
  // 第一次退出先异步通知后台保存并释放输入，等待它结束；再次 app.quit 才真正关闭窗口。
  // 后台超时未退出时终止子进程，避免桌面关闭后残留托管服务。
  if (quitting) return;
  event.preventDefault(); quitting = true;
  clearInterval(heartbeat); clearTimeout(reconnectTimer);
  (async () => {
    try { if (port) await request('POST', '/shutdown'); } catch {}
    socket?.close();
    if (backend && backend.exitCode === null) {
      await new Promise(resolve => { backend.once('exit', resolve); setTimeout(resolve, 3000); });
      if (backend.exitCode === null) backend.kill();
    }
    app.quit();
  })();
});
app.on('window-all-closed', () => app.quit());
