// 前端不直接请求后台端口，只经 preload 的受限桥接调用；失败统一转成界面可显示的异常。
export async function api<T>(method: string,path: string,body?: unknown): Promise<T> {
  if (!window.gal) throw new Error('请通过桌面应用打开，浏览器页面不能控制游戏');
  const response = await window.gal.api(method,path,body);
  if (!response.ok) throw new Error(response.error || '操作未完成');
  return response.data as T;
}
export const shortDate = (value: string) => new Date(value).toLocaleString('zh-CN', { month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false });
export const labels: Record<string,string> = {
  // 这些是运行状态的展示文案，是否允许开始仍需检查绑定、模型配置和进度对齐状态。
  paused:'已暂停',waiting_focus:'等待切回游戏',reading:'正在阅读',thinking:'正在思考',acting:'正在操作'
};
export function actionLabel(action: any): string {
  // 描述模型提议的动作；实际点击或按键来自 RuntimeState.execution，不能把两者混淆。
  if (!action) return '等待下一步';
  return ({wait:'等待画面',advance:'推进一段对话',choose_option:`选择第 ${action.option_index} 项`,click:'点击游戏窗口',press_key:`按 ${action.key}`,request_help:'请求你的引导'} as Record<string,string>)[action.type] || '等待下一步';
}
