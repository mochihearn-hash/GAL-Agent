// IPC 到本地 HTTP 的路径白名单：只有应用实际使用的接口可以由页面调用。
// 会话/配置标识要求32位十六进制 UUID，图片只允许对应会话下的 PNG。
const routes = [
  /^\/(health|state|windows|configs|sessions)$/,
  /^\/configs\/[a-f0-9]{32}(\/test)?$/,
  /^\/sessions\/[a-f0-9]{32}(\/(select|settings|bind|start|pause|guidance|events|checkpoints|reconcile|preview|export))?$/,
  /^\/sessions\/[a-f0-9]{32}\/(guidance|restore)\/[a-f0-9]{32}$/,
  /^\/images\/[a-f0-9]{32}\/[a-f0-9]{32}\.png$/
];
function allowedPath(method, input) {
  // 禁止路径穿越、反斜杠和百分号编码绕过；查询参数只开放时间线分页。
  // 这里检查允许的路径形状，实际资源是否存在和动作状态由后台再次验证。
  if (!['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].includes(method) || typeof input !== 'string') return false;
  if (input.length > 300 || input.includes('..') || input.includes('\\') || input.includes('%')) return false;
  const [path, query] = input.split('?');
  if (query && (!/\/events$/.test(path) || !/^(before=\d+&)?limit=\d+$/.test(query))) return false;
  return routes.some(r => r.test(path));
}
module.exports = { allowedPath };
