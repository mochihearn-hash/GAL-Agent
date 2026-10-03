// 与 Python 数据结构对应的前端类型；实际输入仍由后端进行运行时校验。
// 客户区和边界坐标是物理像素，配置中的位置和区域则使用0—1比例。
export interface GameWindow {
  hwnd: number; pid: number; process_started: number; title: string; process_name: string;
  client: number[]; frame: number[]; dpi: number; minimized: boolean;
}
export interface Profile {
  // advance_key 决定推进方式，advance_point 是点击位置；三个时间段分别用于移动、按住、操作间隔。
  advance_key: string; allowed_keys: string[]; dialogue_region: number[];
  advance_point: number[]; move_settle_ms: number; input_hold_ms: number;
  settle_ms: number; action_interval_ms: number; text_change_threshold: number;
  // 稳定、反馈等待、阈值、对话区域保留以读取旧会话；Agent运行不再依赖这些像素参数。
  feedback_timeout_ms: number; max_action_retries: number;
}
export const defaultProfile: Profile = {
  // 和后端 GameProfile 默认值保持一致，读取旧会话的 Partial<Profile> 时补齐新增字段。
  advance_key: 'SPACE', allowed_keys: ['SPACE','ENTER','UP','DOWN','LEFT','RIGHT'],
  advance_point: [0.5,0.85], move_settle_ms: 50, input_hold_ms: 100,
  dialogue_region: [0.05,0.58,0.9,0.38], settle_ms: 900, action_interval_ms: 1000, text_change_threshold: 2,
  feedback_timeout_ms: 5000, max_action_retries: 2
};
export interface Guidance { id: string; text: string; scope: 'next'|'persistent'; created_at: string }
export interface Session {
  // needs_reconcile=1 表示进度尚需确认；Agent 检查点不能代替游戏自己的存档。
  id: string; name: string; created_at: string; updated_at: string; needs_reconcile: number;
  settings: { config_id: string|null; profile: Partial<Profile> };
  memory: { recent: any[]; chapters: any[]; facts: any[]; hypotheses: any[]; questions: any[]; summarized_through: number };
  guidance: Guidance[]; last_image: string|null; last_window: GameWindow|null; branch: string;
}
export type SessionItem = Pick<Session,'id'|'name'|'created_at'|'updated_at'|'needs_reconcile'>;
export interface ModelConfig {
  id: string; name: string; url: string; model: string; protocol: 'compatible'|'anthropic'|'gemini';
  has_key: boolean; temperature: number|null; context_tokens: number; timeout: number; json_mode: boolean;
}
export interface RuntimeState {
  // session_id 是选中会话，bound_session_id 是当前捕获绑定；next_action 为提议，execution 为已发送结果。
  session_id: string|null; bound_session_id: string|null; status: string; message: string;
  understanding: string; reason: string; next_action: any; window: GameWindow|null; run_version: number;
  execution?: { input_type?: string; key?: string|null; screen_point?: number[]; effective_action?: string; system_accepted?: boolean }|null;
}
export interface StoryEvent { id: string; seq: number; kind: string; payload: any; created_at: string; branch: string }
export interface Checkpoint { id: string; note: string; created_at: string; snapshot: any }
export interface Bridge {
  // preload 实际公开的接口契约；页面没有 Node.js 权限，只能调用这些具名方法。
  bootstrap(): Promise<{ready: boolean; error: string}>;
  api(method: string,path: string,body?: unknown): Promise<{ok: boolean; data?: any; error?: string}>;
  onEvent(callback: (event: any)=>void): ()=>void;
  showOverlay(): Promise<boolean>;
  setOverlayInteractive(value: boolean): Promise<boolean>;
  setOverlayCollapsed(value: boolean): Promise<boolean>;
  hideOverlay(): Promise<boolean>;
  exportSession(id: string): Promise<boolean>;
  close(): Promise<void>;
}
declare global { interface Window { gal?: Bridge } }
