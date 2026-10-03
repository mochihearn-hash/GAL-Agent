// 开发模式同时管理 Vite 和 Electron：先等待页面可访问，再启动桌面，避免启动时白屏。
// 子进程使用独立参数和隐藏窗口，不另外打开终端窗口。
const { spawn } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const vite = spawn(process.execPath, [path.join(root, 'node_modules', 'vite', 'bin', 'vite.js'), '--config', 'frontend/vite.config.ts'], { cwd: root, stdio: 'inherit', windowsHide: true });
let desktop;
let stopped = false;
async function wait() {
  // 轮询次数和单次请求均有限；超时后统一停止开发进程，不留下孤立的前端服务。
  for (let i = 0; i < 100; i++) {
    if (stopped) return;
    try {
      const response = await fetch('http://127.0.0.1:5173', { signal: AbortSignal.timeout(500) });
      if (response.ok) {
        desktop = spawn(require('electron'), ['.'], { cwd: root, stdio: 'inherit', windowsHide: true, env: { ...process.env, GAL_AGENT_DEV_URL: 'http://127.0.0.1:5173' } });
        desktop.on('exit', stop);
        return;
      }
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  console.error('前端启动超时'); stop();
}
function stop() { if (stopped) return; stopped = true; vite.kill(); desktop?.kill(); }
// 两个子进程任一退出或用户终止脚本时，都清理另一方；stopped 保证重复触发不重复执行。
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
vite.on('exit', stop);
wait();
