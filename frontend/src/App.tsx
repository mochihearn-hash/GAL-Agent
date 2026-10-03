// 主界面和悬浮窗共用组件，通过 #overlay 显示不同布局，并共享后台事实来源。
// 页面仅管理展示与用户命令，不直接截取桌面、执行系统输入或持有后台控制 token。
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { BookOpen, Plus, MoreHorizontal, Settings2, Play, Pause, Monitor, ArrowUpRight, Sparkles, MessageSquare, Clock3, ChevronDown, X, Check, LoaderCircle, RotateCcw, Download, Save, Eye, Send, Pencil, Trash2, Cable, CircleHelp, PanelLeftClose } from 'lucide-react';
import { api, shortDate, labels, actionLabel } from './api';
import { defaultProfile, type Session, type SessionItem, type ModelConfig, type RuntimeState, type StoryEvent, type Checkpoint, type GameWindow, type Profile } from './types';

const initial: RuntimeState = { session_id:null,bound_session_id:null,status:'paused',message:'准备开始一段新的剧情',understanding:'',reason:'',next_action:null,window:null,run_version:0 };
const isOverlay = location.hash === '#overlay';

function Modal({title,children,onClose,wide=false}: {title:string;children:ReactNode;onClose:()=>void;wide?:boolean}) {
  // 统一处理 Escape 和点击遮罩关闭；卸载时移除监听，不影响后续页面输入。
  useEffect(()=>{
    const handler=(event:KeyboardEvent)=>{if(event.key==='Escape')onClose();};
    document.addEventListener('keydown',handler);return()=>document.removeEventListener('keydown',handler);
  },[onClose]);
  return <div className="modal-backdrop" onMouseDown={e=>{if(e.target===e.currentTarget)onClose();}}>
    <section className={`modal ${wide?'wide':''}`} role="dialog" aria-modal="true" aria-label={title}>
      <div className="modal-heading"><h2>{title}</h2><button className="icon-button" onClick={onClose} aria-label="关闭"><X size={18}/></button></div>{children}
    </section>
  </div>;
}

export function App() {
  // ready/bootError 管理桌面握手，session 是会话数据，state 是后台当前运行状态。
  // 配置、时间线和检查点通过接口加载，不能把服务端文本直接当作可执行动作。
  const [ready,setReady]=useState(false);
  const [bootError,setBootError]=useState('');
  const [sessions,setSessions]=useState<SessionItem[]>([]);
  const [configs,setConfigs]=useState<ModelConfig[]>([]);
  const [selected,setSelected]=useState<string|null>(null);
  const selectedRef=useRef<string|null>(null);
  // ref 供异步回调读取当前会话；旧会话加载完成时必须确认 ID 仍匹配，才更新界面。
  const [session,setSession]=useState<Session|null>(null);
  const [state,setState]=useState(initial);
  const [events,setEvents]=useState<StoryEvent[]>([]);
  const [checkpoints,setCheckpoints]=useState<Checkpoint[]>([]);
  const [preview,setPreview]=useState<string|null>(null);
  const [previewError,setPreviewError]=useState('');
  const [toast,setToast]=useState('');
  const [busy,setBusy]=useState(false);
  const [nameModal,setNameModal]=useState<{id?:string;name:string}|null>(null);
  const [menuId,setMenuId]=useState<string|null>(null);
  const [deleteId,setDeleteId]=useState<string|null>(null);
  const [configModal,setConfigModal]=useState<ModelConfig|null|false>(false);
  const [windowModal,setWindowModal]=useState(false);
  const [profileModal,setProfileModal]=useState(false);
  const [restoreModal,setRestoreModal]=useState<Checkpoint|null>(null);
  const [checkpointModal,setCheckpointModal]=useState(false);
  const [note,setNote]=useState('');
  const [tab,setTab]=useState<'story'|'memory'|'checkpoints'>('story');
  const [guidance,setGuidance]=useState('');
  const [scope,setScope]=useState<'next'|'persistent'>('next');
  const [interactive,setInteractive]=useState(false);
  // 观看模式不可获取焦点；只有编辑引导时 interactive=true，并先由桌面暂停 Agent。
  const [collapsed,setCollapsed]=useState(false);
  const [imageModal,setImageModal]=useState<string|null>(null);
  const toastTimer=useRef<ReturnType<typeof setTimeout>|null>(null);

  const notify=useCallback((text:string)=>{setToast(text);if(toastTimer.current)clearTimeout(toastTimer.current);toastTimer.current=setTimeout(()=>setToast(''),6500);},[]);
  const load=useCallback(async(sid:string)=>{
    // 独立查询并行执行，结果返回后核对 selectedRef，避免覆盖刚切换的新会话。
    const [detail,history,points]=await Promise.all([
      api<Session>('GET',`/sessions/${sid}`),api<StoryEvent[]>('GET',`/sessions/${sid}/events?limit=100`),api<Checkpoint[]>('GET',`/sessions/${sid}/checkpoints`)
    ]);
    if(selectedRef.current===sid){setSession(detail);setEvents(history);setCheckpoints(points);}
  },[]);
  const refresh=useCallback(async()=>{
    // 刷新会话/模型共享列表，再拉取选中会话；主界面与悬浮窗都按后台数据刷新。
    const [items,models]=await Promise.all([api<SessionItem[]>('GET','/sessions'),api<ModelConfig[]>('GET','/configs')]);
    setSessions(items);setConfigs(models);
    if(selectedRef.current&&items.some(x=>x.id===selectedRef.current))await load(selectedRef.current);
  },[load]);
  const refreshRef=useRef(refresh);refreshRef.current=refresh;

  useEffect(()=>{
    // 订阅后台并完成初始化。状态通知用250毫秒合并，避免频繁事件重复触发查询。
    // alive 防止卸载后的请求更新组件，退出时清理订阅和待执行定时器。
    let alive=true;let unsubscribe=()=>{};let refreshTimer:ReturnType<typeof setTimeout>|undefined;
    if(!window.gal){setBootError('请运行桌面应用启动入口；普通浏览器不能访问游戏窗口。');return;}
    unsubscribe=window.gal.onEvent(event=>{
      if(!alive)return;
      if(isOverlay&&event.type==='overlay_mode'){
        setInteractive(event.interactive);setCollapsed(event.collapsed);
      }
      if(event.type==='status'){
        // 悬浮窗跟随后台当前会话，不允许继续展示或控制上一段旅程。
        setState(event.data);
        if(isOverlay&&event.data.session_id!==selectedRef.current){selectedRef.current=event.data.session_id;setSelected(event.data.session_id);}
      }
      if(event.type==='connection_error')notify(event.message);
      if(['event','refresh','sessions_changed','status'].includes(event.type)){
        if(!refreshTimer)refreshTimer=setTimeout(()=>{refreshTimer=undefined;refreshRef.current().catch(()=>{});},250);
      }
    });
    (async()=>{
      try{
        const bootstrap=await window.gal!.bootstrap();
        if(!alive)return;
        if(!bootstrap.ready){setBootError(bootstrap.error||'后台启动失败');return;}
        const [items,models,current]=await Promise.all([api<SessionItem[]>('GET','/sessions'),api<ModelConfig[]>('GET','/configs'),api<RuntimeState>('GET','/state')]);
        if(!alive)return;
        setSessions(items);setConfigs(models);setState(current);setReady(true);
        const id=isOverlay?current.session_id:(current.session_id||items[0]?.id||null);
        selectedRef.current=id;setSelected(id);
        if(id){if(!isOverlay)await api('POST',`/sessions/${id}/select`);await load(id);}
      }catch(error){if(alive)setBootError((error as Error).message);}
    })();
    return()=>{alive=false;unsubscribe();if(refreshTimer)clearTimeout(refreshTimer);};
  },[load,notify]);
  useEffect(()=>{selectedRef.current=selected;if(selected&&ready)load(selected).catch(error=>notify(error.message));},[selected,ready,load,notify]);
  useEffect(()=>{
    setPreview(null);setPreviewError('');
    // 1200毫秒只控制主窗口预览刷新，不是 Agent 决策的截图间隔。
    // 预览请求未完成时不发下一次，且悬浮窗不重复请求图片，避免积压。
    if(isOverlay||!ready||!selected||state.bound_session_id!==selected)return;
    let alive=true,inFlight=false;
    const update=async()=>{
      if(inFlight)return;inFlight=true;
      try{const image=await api<string>('GET',`/sessions/${selected}/preview`);if(alive){setPreview(image);setPreviewError('');}}
      catch(error){if(alive)setPreviewError((error as Error).message);}
      finally{inFlight=false;}
    };
    update();const timer=setInterval(update,1200);return()=>{alive=false;clearInterval(timer);};
  },[selected,state.bound_session_id,ready]);
  const perform=async(work:()=>Promise<void>)=>{setBusy(true);try{await work();}catch(error){notify((error as Error).message);}finally{setBusy(false);}};
  const selectSession=(sid:string)=>perform(async()=>{
    // 先让后台暂停旧任务并切换，再更新界面；不同会话之间不复制剧情记忆。
    await api('POST',`/sessions/${sid}/select`);selectedRef.current=sid;setSelected(sid);setSession(null);setTab('story');await load(sid);await refresh();
  });
  const saveName=()=>perform(async()=>{
    if(!nameModal)return;
    if(nameModal.id){await api('PATCH',`/sessions/${nameModal.id}`,{name:nameModal.name});}
    else{const created=await api<Session>('POST','/sessions',{name:nameModal.name});await api('POST',`/sessions/${created.id}/select`);selectedRef.current=created.id;setSelected(created.id);setSession(created);setTab('story');}
    setNameModal(null);await refresh();
  });
  const deleteSession=()=>perform(async()=>{
    if(!deleteId)return;await api('DELETE',`/sessions/${deleteId}`);
    if(selected===deleteId){selectedRef.current=null;setSelected(null);setSession(null);setEvents([]);setPreview(null);}
    setDeleteId(null);await refresh();
  });
  const running=state.session_id===selected&&state.status!=='paused';
  // 等待游戏焦点仍属于运行任务，不能当作暂停来启用重复开始或进度确认。
  const currentState=state.session_id===selected?state:initial;
  const bound=Boolean(selected&&state.bound_session_id===selected);
  const model=configs.find(x=>x.id===session?.settings.config_id);
  const startDisabled=!ready||busy||running||!selected||!model||!bound||!session||session.needs_reconcile===1;
  // 禁用按钮只改善体验，后台 start 仍会独立核对模型、绑定、对齐标记和心跳。
  const startLabel=events.some(e=>e.kind==='run')?'继续':'开始';
  const startHint=!selected?'请先在主窗口创建或选择会话':!model?'请先在主窗口选择模型配置':!bound?'请先在主窗口选择游戏窗口':session?.needs_reconcile===1&&!running?'请核对当前游戏画面与记忆，并确认进度已对齐':interactive?'开始前会返回观看模式，请切回游戏':'观看模式下，点击按钮不抢游戏焦点';
  const start=()=>perform(async()=>{if(!selected)return;if(isOverlay){await window.gal?.setOverlayInteractive(false);setInteractive(false);}setState(await api<RuntimeState>('POST',`/sessions/${selected}/start`));});
  // 暂停不等待 busy；发送引导后需用户另点继续，不能自动恢复旧模型动作。
  const pause=async()=>{if(selected)try{setState(await api<RuntimeState>('POST',`/sessions/${selected}/pause`));await refresh();}catch(error){notify((error as Error).message);}};
  const sendGuidance=()=>perform(async()=>{if(!selected||!guidance.trim())return;await api('POST',`/sessions/${selected}/guidance`,{text:guidance,scope});setGuidance('');await refresh();notify('引导已保存，继续时会重新观察剧情');});
  const reconcile=()=>perform(async()=>{if(!selected)return;await api('POST',`/sessions/${selected}/reconcile`);await refresh();notify('进度已对齐，点击继续后会重新观察游戏');});
  // 悬浮窗复用主窗口的对齐接口。确认只更新状态，不会执行 start 或向游戏输入。
  const closeModal=()=>setNameModal(null);
  const enterGuidance=()=>perform(async()=>{await window.gal?.setOverlayInteractive(true);setInteractive(true);});
  const leaveGuidance=()=>perform(async()=>{await window.gal?.setOverlayInteractive(false);setInteractive(false);});

  // 控制栏放在可滚动主体之外，收起后仍显示暂停、确认进度和开始/继续三个按钮。
  if(isOverlay)return <div className={`overlay-app ${collapsed?'collapsed':''}`}>
    <div className="overlay-header"><span className="brand-mark small"><BookOpen size={19}/></span><div><strong>{session?.name||'GAL Agent'}</strong><span>{interactive?'引导模式 · 已暂停':labels[currentState.status]||'已暂停'} · 拖动这里移动</span></div><button className="icon-button light" aria-label={collapsed?'展开悬浮窗':'收起悬浮窗'} disabled={busy} onClick={()=>perform(async()=>{await window.gal?.setOverlayCollapsed(!collapsed);setCollapsed(!collapsed);})}><ChevronDown size={16} style={{transform:collapsed?'rotate(180deg)':undefined}}/></button><button className="icon-button light" aria-label="隐藏悬浮窗" onClick={()=>perform(async()=>{await window.gal?.hideOverlay();})}><X size={16}/></button></div>
    {!collapsed&&<div className="overlay-body">
      <div className="overlay-card"><h2><Sparkles size={15}/>此刻，它在想什么</h2><p>{currentState.understanding||'开始后，这里会显示对当前剧情的简短理解。'}</p></div>
      <div className="overlay-next"><Sparkles size={15}/>模型建议：{actionLabel(currentState.next_action)}</div>{currentState.execution&&<p className="overlay-execution">实际操作：{currentState.execution.effective_action||(currentState.execution.key?`按 ${currentState.execution.key}`:'点击游戏窗口')}</p>}
      {currentState.reason&&<p className="overlay-reason">{currentState.reason}</p>}
      <section className="overlay-guidance"><h2><MessageSquare size={15}/>给它一点引导</h2><p className="overlay-reason">{interactive?'Agent 已暂停。发送引导后，点击继续并切回游戏。':'观看模式不抢焦点。编辑引导前会先暂停 Agent。'}</p><button className="secondary dark compact" disabled={busy||!selected} onClick={interactive?leaveGuidance:enterGuidance}>{interactive?<Eye size={14}/>:<Pencil size={14}/>} {interactive?'返回观看':'编辑引导'}</button>
      <div className="overlay-compose"><textarea aria-label="引导内容" readOnly={!interactive} placeholder={interactive?'告诉 Agent 它遗漏了什么…':'点击“编辑引导”后输入…'} value={guidance} onChange={e=>setGuidance(e.target.value)}/><select aria-label="引导范围" disabled={!interactive} value={scope} onChange={e=>setScope(e.target.value as 'next'|'persistent')}><option value="next">仅下一步</option><option value="persistent">持续生效</option></select><button className="primary" disabled={!interactive||busy||!guidance.trim()} onClick={sendGuidance}><Send size={14}/>发送引导</button></div>
      {session?.guidance.map(item=><div className="guidance-item" key={item.id}><span>{item.scope==='next'?'下一步':'持续'} · {item.text}</span><button className="icon-button light" aria-label={`取消引导 ${item.text}`} onClick={()=>perform(async()=>{await api('DELETE',`/sessions/${selected}/guidance/${item.id}`);await refresh();})}><X size={12}/></button></div>)}
      </section>
    </div>}<div className="overlay-footer" role="region" aria-label="运行控制"><div className="overlay-status" role="status"><span className={`status-dot ${running?'active':''}`}/><p>{bootError||currentState.message}</p></div><div className="overlay-buttons"><button className="secondary dark" disabled={!ready||!selected} onClick={pause}><Pause size={15}/>暂停 / 接管</button><button className="secondary dark" disabled={!ready||!bound||busy||running} title="确认当前游戏画面与会话进度一致" onClick={reconcile}><Check size={15}/>确认进度对齐</button><button className="primary" disabled={startDisabled} title={startHint} onClick={start}><Play size={15}/>{startLabel}</button></div><p className="overlay-hint">{startHint}</p></div>{toast&&<div className="toast overlay-toast" role="status">{toast}</div>}
  </div>;

  // 主界面展示会话、游戏预览、模型设置和记录；所有用户操作仍通过统一接口提交。
  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><BookOpen size={23}/></span><div><strong>GAL Agent</strong><span>剧情观测室</span></div></div>
      <button className="new-session" disabled={!ready||busy} onClick={()=>setNameModal({name:''})}><Plus size={18}/>新建会话</button>
      <div className="sidebar-caption">你的剧情旅程 <span>{sessions.length}</span></div>
      <nav className="session-list" aria-label="会话列表">{sessions.map(item=><div key={item.id} className={`session-row ${selected===item.id?'selected':''}`}>
        <button className="session-select" disabled={busy} onClick={()=>selectSession(item.id)}><MessageSquare size={16}/><span><strong>{item.name}</strong><small>{shortDate(item.updated_at)}{state.session_id===item.id&&state.status!=='paused'?' · 运行中':''}</small></span></button>
        <button className="session-more" aria-label={`管理 ${item.name}`} onClick={()=>setMenuId(menuId===item.id?null:item.id)}><MoreHorizontal size={17}/></button>
        {menuId===item.id&&<div className="session-menu"><button onClick={()=>{setNameModal({id:item.id,name:item.name});setMenuId(null);}}><Pencil size={14}/>重命名</button><button className="danger-text" onClick={()=>{setDeleteId(item.id);setMenuId(null);}}><Trash2 size={14}/>删除会话</button></div>}
      </div>)}{sessions.length===0&&<p className="sidebar-empty">每段剧情，都有独立的记忆。<br/>创建你的第一个会话。</p>}</nav>
      <div className="sidebar-bottom"><div className="local-indicator"><span className={`status-dot ${ready?'active':''}`}/>{ready?'本地会话已就绪':'正在连接后台'}</div><p>记录自动保存到这台电脑</p><span className="version">WINDOWS · v0.2.0</span></div>
    </aside>
    <main className="main-area">
      <header className="topbar"><div className="breadcrumb"><BookOpen size={15}/><span>会话</span><span className="slash">/</span><strong>{session?.name||'欢迎来到剧情观测室'}</strong></div><div className="top-actions"><button className="text-button" disabled={!selected||!ready} onClick={()=>perform(async()=>{await window.gal?.exportSession(selected!);})}><Download size={15}/>导出记录</button><button className="secondary compact" disabled={!ready} onClick={()=>perform(async()=>{await window.gal?.showOverlay();})}><PanelLeftClose size={15}/>悬浮窗</button></div></header>
      {!ready?<div className="welcome"><span className="welcome-symbol"><BookOpen size={42}/></span><h1>{bootError?'应用暂时未就绪':'正在打开剧情观测室'}</h1><p>{bootError||'正在启动本地后台，读取已保存的会话…'}</p>{!bootError&&<LoaderCircle className="spin" size={22}/>}</div>:!selected?<div className="welcome"><span className="welcome-symbol"><BookOpen size={42}/></span><span className="eyebrow">A DIFFERENT READER, A DIFFERENT STORY</span><h1>让模型，走进故事。</h1><p>观察它如何读懂人物、面对选择。<br/>每段旅程独立记录，随时暂停，随时继续。</p><button className="primary large" onClick={()=>setNameModal({name:''})}><Plus size={18}/>创建第一个会话</button><div className="welcome-features"><span><Monitor size={16}/>只观察选定窗口</span><span><Save size={16}/>剧情记忆持续保存</span><span><MessageSquare size={16}/>随时给出引导</span></div></div>:<div className="workspace">
        <div className="workspace-heading"><div><span className="eyebrow">STORY SESSION</span><h1>{session?.name||'正在加载…'}</h1></div><div className="heading-status"><span className={`status-dot ${running?'active':''}`}/>{labels[currentState.status]||'已暂停'}</div></div>
        <div className="workspace-grid"><div className="story-column">
          <section className="preview-card"><div className="card-heading"><h2><Monitor size={16}/>游戏视窗</h2><div><button className="text-button" disabled={busy} onClick={()=>setProfileModal(true)}><Settings2 size={14}/>操作设置</button><button className="secondary compact" disabled={busy} onClick={()=>setWindowModal(true)}>{bound?'更换窗口':'选择窗口'}<ArrowUpRight size={14}/></button></div></div>
            <div className={`game-preview ${preview?'has-image':''}`}>{preview?<img src={preview} alt="当前选定的游戏窗口"/>:<div className="preview-empty"><span className="preview-symbol"><Monitor size={32}/></span><h3>{bound?'正在读取游戏画面':'连接你的游戏窗口'}</h3><p>{previewError||'打开游戏后，选择要让 Agent 观察的窗口。'}</p>{!bound&&<button className="secondary" disabled={busy} onClick={()=>setWindowModal(true)}><Plus size={15}/>选择窗口</button>}</div>}{preview&&previewError&&<div className="preview-warning">{previewError}</div>}{bound&&<div className="preview-label"><span className="status-dot active"/>{state.window?.title||'游戏窗口'}</div>}</div>
            <div className="playback-bar"><div><span className={`status-dot ${running?'active':''}`}/><p>{currentState.message}</p></div><div className="playback-buttons"><button className="secondary" disabled={!selected} onClick={pause}><Pause size={15}/>暂停</button><button className="primary" disabled={startDisabled} onClick={start}><Play size={15}/>{startLabel}</button></div></div>
          </section>
          {session?.needs_reconcile===1&&<div className="reconcile-banner"><RotateCcw size={20}/><div><strong>继续前，核对游戏进度</strong><p>载入对应游戏存档。Agent 记忆不能替代游戏存档，未确认的操作不会自动重放。</p></div><button className="secondary compact" disabled={!bound||busy||running} onClick={()=>perform(async()=>{await api('POST',`/sessions/${selected}/reconcile`);await refresh();})}><Check size={15}/>进度已对齐</button></div>}
          <section className="journal-card"><div className="journal-tabs"><div>{([['story','剧情记录',MessageSquare],['memory','剧情记忆',BookOpen],['checkpoints','恢复进度',Clock3]] as const).map(([value,title,Icon])=><button key={value} className={tab===value?'active':''} onClick={()=>setTab(value)}><Icon size={15}/>{title}</button>)}</div><span className="autosave"><Check size={13}/>自动保存</span></div>
            {tab==='story'?<Timeline events={events} selected={selected} onImage={async path=>{try{setImageModal(await api<string>('GET',`/images/${path}`));}catch(error){notify((error as Error).message);}}} onOlder={()=>perform(async()=>{if(!events.length)return;const older=await api<StoryEvent[]>('GET',`/sessions/${selected}/events?before=${events[0].seq}&limit=100`);setEvents(old=>[...older,...old.filter(e=>!older.some(x=>x.id===e.id))]);if(!older.length)notify('已到最早的记录');})}/>:tab==='memory'?<MemoryView session={session}/>:<div className="checkpoints"><div className="checkpoint-intro"><p>Agent 记忆检查点需与游戏存档一起恢复。</p><button className="secondary compact" disabled={busy} onClick={()=>{setNote('');setCheckpointModal(true);}}><Save size={14}/>保存检查点</button></div>{checkpoints.length===0?<div className="empty-state"><Clock3 size={24}/><p>开始体验后，进度会持续保存。<br/>也可以手动保存并备注游戏存档位置。</p></div>:checkpoints.map(point=><div className="checkpoint-row" key={point.id}><span className="checkpoint-icon"><Clock3 size={17}/></span><div><strong>{point.note||'未命名检查点'}</strong><small>{shortDate(point.created_at)} · {point.snapshot.memory?.recent?.length||0} 段近期剧情</small></div><button className="text-button" disabled={busy} onClick={()=>setRestoreModal(point)}><RotateCcw size={14}/>恢复</button></div>)}</div>}
          </section>
        </div><aside className="details-column">
          <section className="model-card"><div className="card-heading"><h2><Cable size={16}/>模型连接</h2><button className="icon-button" aria-label="新增模型配置" onClick={()=>setConfigModal(null)}><Plus size={17}/></button></div><label className="field-label" htmlFor="model-select">当前模型配置</label><div className="select-wrap"><select id="model-select" disabled={busy} value={session?.settings.config_id||''} onChange={e=>perform(async()=>{if(!session)return;await api('PUT',`/sessions/${selected}/settings`,{...session.settings,config_id:e.target.value||null});await refresh();})}><option value="">请选择模型配置</option>{configs.map(item=><option value={item.id} key={item.id}>{item.name}</option>)}</select><ChevronDown size={15}/></div>{model?<><div className="model-info"><span className="model-badge"><Sparkles size={17}/></span><div><strong>{model.model}</strong><small>{({compatible:'兼容接口',anthropic:'Anthropic',gemini:'Gemini'})[model.protocol]}</small></div></div><p className="endpoint" title={model.url}>{model.url}</p><button className="secondary full" onClick={()=>setConfigModal(model)}><Settings2 size={14}/>编辑与测试连接</button></>:<div className="model-empty"><p>接入一个支持图片的大模型，<br/>开始观察它的剧情选择。</p><button className="secondary full" onClick={()=>setConfigModal(null)}><Plus size={14}/>添加模型配置</button></div>}</section>
          <section className="insight-card"><h2><Sparkles size={16}/>此刻，它在想什么</h2><p>{currentState.understanding||'运行后，这里会显示模型对当前剧情的简短理解。'}</p><div className="next-action"><span>下一步</span><strong>{actionLabel(currentState.next_action)}</strong></div>{currentState.reason&&<p className="reason">{currentState.reason}</p>}</section>
          <section className="guidance-card"><h2><MessageSquare size={16}/>给它一点引导</h2><p className="card-description">指出遗漏的信息，或告诉它你希望关注什么。发送后会先暂停。</p><textarea aria-label="引导内容" placeholder="例如：刚才那句话是在开玩笑，可以再考虑一下她的态度。" value={guidance} maxLength={4000} onChange={e=>setGuidance(e.target.value)}/><div className="guidance-controls"><select aria-label="引导范围" value={scope} onChange={e=>setScope(e.target.value as 'next'|'persistent')}><option value="next">仅下一步</option><option value="persistent">持续生效</option></select><button className="primary compact" disabled={busy||!guidance.trim()} onClick={sendGuidance}><Send size={14}/>发送</button></div>{session?.guidance.map(item=><div className="guidance-item" key={item.id}><span>{item.scope==='next'?'下一步':'持续'} · {item.text}</span><button className="icon-button" title="取消引导" onClick={()=>perform(async()=>{await api('DELETE',`/sessions/${selected}/guidance/${item.id}`);await refresh();})}><X size={13}/></button></div>)}</section>
          <div className="note-card"><CircleHelp size={16}/><p>切回游戏窗口后才会操作。<br/>退出后，Agent 记忆会保留；<br/>游戏进度请在游戏内存档。</p></div>
        </aside></div>
      </div>}
    </main>
    {toast&&<div className="toast" role="status">{toast}<button className="icon-button" aria-label="关闭提示" onClick={()=>setToast('')}><X size={15}/></button></div>}
    {nameModal&&<Modal title={nameModal.id?'重命名会话':'新建会话'} onClose={closeModal}><form onSubmit={e=>{e.preventDefault();saveName();}}><label className="field-label" htmlFor="session-name">会话名称</label><input id="session-name" autoFocus maxLength={120} placeholder="为这段旅程起个名字" value={nameModal.name} onChange={e=>setNameModal({...nameModal,name:e.target.value})}/>{!nameModal.id&&<p className="form-hint">创建独立的剧情记忆，自动复用最近选择的模型配置，再选择游戏窗口。</p>}<div className="modal-actions"><button type="button" className="secondary" onClick={closeModal}>取消</button><button className="primary" disabled={busy||!nameModal.name.trim()}>{busy?<LoaderCircle className="spin" size={15}/>:<Check size={15}/>} {nameModal.id?'保存名称':'创建会话'}</button></div></form></Modal>}
    {deleteId&&<Modal title="删除会话" onClose={()=>setDeleteId(null)}><p className="modal-copy">删除“{sessions.find(s=>s.id===deleteId)?.name}”的剧情记录、记忆和 Agent 检查点。游戏自己的存档会保留。</p><div className="modal-actions"><button className="secondary" onClick={()=>setDeleteId(null)}>取消</button><button className="danger-button" disabled={busy} onClick={deleteSession}><Trash2 size={15}/>删除会话</button></div></Modal>}
    {configModal!==false&&<ModelDialog model={configModal} busy={busy} onClose={()=>{setConfigModal(false);refresh().catch(error=>notify(error.message));}} notify={notify} onSaved={async id=>{setConfigModal(false);if(session&&!session.settings.config_id)await api('PUT',`/sessions/${session.id}/settings`,{...session.settings,config_id:id});await refresh();}} onDeleted={async()=>{setConfigModal(false);await refresh();}}/>}
    {windowModal&&<WindowDialog busy={busy} onClose={()=>setWindowModal(false)} onSelect={window=>perform(async()=>{await api('POST',`/sessions/${selected}/bind`,{hwnd:window.hwnd,pid:window.pid,process_started:window.process_started});setWindowModal(false);await refresh();setState(await api<RuntimeState>('GET','/state'));})}/>} 
    {profileModal&&session&&<ProfileDialog preview={preview} profile={{...defaultProfile,...session.settings.profile}} onClose={()=>setProfileModal(false)} onSave={profile=>perform(async()=>{await api('PUT',`/sessions/${selected}/settings`,{...session.settings,profile});setProfileModal(false);await refresh();})}/>}
    {checkpointModal&&<Modal title="保存 Agent 检查点" onClose={()=>setCheckpointModal(false)}><label className="field-label" htmlFor="save-note">备注（可选）</label><input id="save-note" autoFocus value={note} onChange={e=>setNote(e.target.value)} maxLength={1000} placeholder="例如：游戏存档第 3 槽，第二章结束"/><p className="form-hint">先在游戏内保存，再记录对应的 Agent 记忆位置。</p><div className="modal-actions"><button className="secondary" onClick={()=>setCheckpointModal(false)}>取消</button><button className="primary" disabled={busy} onClick={()=>perform(async()=>{await api('POST',`/sessions/${selected}/checkpoints`,{note});setCheckpointModal(false);await refresh();})}><Save size={15}/>保存</button></div></Modal>}
    {restoreModal&&<Modal title="恢复剧情记忆" onClose={()=>setRestoreModal(null)}><p className="modal-copy">恢复到“{restoreModal.note||shortDate(restoreModal.created_at)}”。当前记忆会先自动保存。恢复后，请载入对应的游戏存档，并重新选择窗口。</p><div className="modal-actions"><button className="secondary" onClick={()=>setRestoreModal(null)}>取消</button><button className="primary" disabled={busy} onClick={()=>perform(async()=>{await api('POST',`/sessions/${selected}/restore/${restoreModal.id}`);setRestoreModal(null);await refresh();})}><RotateCcw size={15}/>恢复检查点</button></div></Modal>}
    {imageModal&&<Modal title="当时的游戏画面" wide onClose={()=>setImageModal(null)}><img className="history-image" src={imageModal} alt="历史游戏截图"/></Modal>}
  </div>;
}

function Timeline({events,selected,onImage,onOlder}:{events:StoryEvent[];selected:string|null;onImage:(image:string)=>void;onOlder:()=>void}){
  // 分开展示模型提议、实际输入和核对结果，不能把“系统接收”显示成“游戏推进”。
  // 决策关联原截图；较早 observation 不在本页时，按固定图片命名规则回查。
  const visible=events.filter(x=>['decision','decision_error','error','guidance','run','summary','pause','restore','reconcile','checkpoint','discarded','guidance_cancelled','retry','wait_retry','action','feedback','summary_attempt'].includes(x.kind));
  const observationMap=new Map(events.filter(x=>x.kind==='observation').map(x=>[x.payload.observation_id,x.payload.image]));
  return <div className="timeline">{events.length>=100&&<button className="text-button older" onClick={onOlder}><Clock3 size={14}/>查看更早记录</button>}{visible.length===0?<div className="empty-state"><BookOpen size={26}/><h3>故事还未开始</h3><p>连接模型和游戏窗口后，点击开始。<br/>对话、选择和你的引导都会记录在这里。</p></div>:visible.map(event=>{
    const payload=event.payload;
    if(event.kind==='decision')return <article className="story-entry" key={event.id}><div className="entry-marker"><BookOpen size={14}/></div><div className="entry-content"><div className="entry-meta"><strong>{payload.speaker||'剧情观察'}</strong><span>{shortDate(event.created_at)}</span><small>{payload.elapsed_ms?`${(payload.elapsed_ms/1000).toFixed(1)}s`:''}</small></div>{payload.dialogue&&<p className="dialogue">{payload.dialogue}</p>}{payload.options?.length>0&&<div className="choice-list">{payload.options.map((option:any,index:number)=><div key={index} className={payload.action.option_index===index+1?'chosen':''}><span>{index+1}</span>{option.text}{payload.action.option_index===index+1&&<Check size={14}/>}</div>)}</div>}{payload.reason&&<p className="entry-reason"><Sparkles size={13}/>{payload.reason}</p>}<div className="entry-footer"><span>{actionLabel(payload.action)} · 提议</span><button className="text-button" onClick={()=>onImage(observationMap.get(payload.observation_id)||`${selected}/${payload.observation_id}.png`)}><Eye size={13}/>查看画面</button></div></div></article>;
    if(event.kind==='action')return <div className="system-entry" key={event.id}><span className="system-dot"/><p>实际操作：{payload.execution?.effective_action||(payload.execution?.key?`按 ${payload.execution.key}`:actionLabel(payload.action))} · {payload.phase==='observed'?(payload.result?.content_changed?'已确认剧情变化':'已核对，剧情未变化'):payload.phase==='unconfirmed'?'反馈未确认':payload.phase==='abandoned'?'未发送':payload.execution?'系统已接收，待确认':'准备发送'}</p><time>{shortDate(event.created_at)}</time></div>;
    if(event.kind==='feedback')return <div className="system-entry" key={event.id}><span className="system-dot"/><p>{payload.verdict==='changed'?'核对结果：剧情已变化':payload.verdict==='same'?'核对结果：仍是原对白或选项':payload.verdict==='pending'?'模型判断：画面未就绪，按决策等待重读':'模型判断：无法确认，等待或请求引导'}</p><button className="text-button" onClick={()=>onImage(payload.image)}>查看画面</button></div>;
    if(event.kind==='summary_attempt')return <div className="system-entry" key={event.id}><span className="system-dot"/><p>摘要第 {payload.attempt} 次格式校验失败，已保存响应{payload.attempt<3?'，正在修正':''}</p></div>;
    if(event.kind==='decision_error')return <article className="model-error-entry" key={event.id}><strong>模型回复未通过校验</strong><p>{payload.errors?.map((error:any)=>`${error.field||'动作与画面状态'}：${error.message}`).join('；')}</p><details><summary>查看模型原始回复</summary><pre>{payload.raw_text}</pre></details><button className="text-button" onClick={()=>onImage(payload.image)}>查看当时画面</button></article>;
    if(event.kind==='guidance')return <article className="human-entry" key={event.id}><MessageSquare size={15}/><div><strong>你的引导 <small>{payload.scope==='next'?'仅下一步':'持续生效'}</small></strong><p>{payload.text}</p></div><span>{shortDate(event.created_at)}</span></article>;
    if(event.kind==='summary')return <article className="summary-entry" key={event.id}><BookOpen size={15}/><div><strong>剧情记忆已整理</strong><p>{payload.summary?.summary}</p></div></article>;
    return <div key={event.id} className={`system-entry ${event.kind==='error'?'error':''}`}><span className="system-dot"/><p>{payload.message||({run:`开始使用 ${payload.model}`,checkpoint:payload.note||'检查点已保存',guidance_cancelled:'已取消一条引导'})[event.kind as 'run']||'状态已更新'}</p><time>{shortDate(event.created_at)}</time></div>;
  })}</div>;
}

function MemoryView({session}:{session:Session|null}){
  // 只读显示摘要、事实/推测/问题和来源，帮助用户判断当前游戏与 Agent 进度是否对齐。
  const memory=session?.memory;
  if(!memory)return <div className="empty-state">正在加载记忆…</div>;
  return <div className="memory-view"><p className="form-hint">记忆来自本会话已读剧情。事实与推测分开保存，原始记录可以回查。</p>{memory.chapters.map((chapter,index)=><div className="memory-summary" key={index}><h3>此前剧情摘要</h3><p>{chapter.summary}</p><small>来源记录 #{chapter.from_seq} — #{chapter.through_seq}</small></div>)}{([['facts','已发生的事实'],['hypotheses','尚未证实的判断'],['questions','未解决的问题']] as const).map(([key,title])=><div className="memory-group" key={key}><h3>{title}<span>{memory[key].length}</span></h3>{memory[key].length?memory[key].map((item,index)=><div className="memory-item" key={index}><span>{item.text}</span><small>来源 #{item.source_seq}</small></div>):<p className="muted">还没有相关记录。</p>}</div>)}<div className="memory-group"><h3>近期剧情<span>{memory.recent.length}</span></h3>{memory.recent.slice(-8).map((item,index)=><p key={index} className="recent-memory">{item.speaker&&<strong>{item.speaker}：</strong>}{item.dialogue||item.understanding}</p>)}</div></div>;
}

function ModelDialog({model,onClose,onSaved,onDeleted,notify}:{model:ModelConfig|null;busy:boolean;onClose:()=>void;onSaved:(id:string)=>Promise<void>;onDeleted:()=>Promise<void>;notify:(text:string)=>void}){
  // 已保存的明文 Key 不回读前端，输入留空保留原值，移除须显式勾选。
  // 测试连接会先保存配置；保存返回 ID 留在本弹窗内，后续继续编辑而不是重复新建。
  const [form,setForm]=useState({name:model?.name||'',url:model?.url||'',model:model?.model||'',protocol:model?.protocol||'compatible',api_key:'',temperature:model?.temperature===null||model?.temperature===undefined?'':String(model.temperature),context_tokens:model?.context_tokens||32000,timeout:model?.timeout||60,json_mode:model?.json_mode||false});
  const [working,setWorking]=useState(false);
  const [result,setResult]=useState('');
  const [id,setId]=useState(model?.id);
  const [clearKey,setClearKey]=useState(false);
  const [dirty,setDirty]=useState(!model);
  const change=(key:string,value:unknown)=>{setForm(old=>({...old,[key]:value}));setDirty(true);setResult('');};
  const persist=async()=>{
    // undefined 被 JSON 序列化省略，对应后台保留旧密钥；空字符串表示明确移除。
    const payload={...form,temperature:form.temperature===''?null:Number(form.temperature),api_key:form.api_key||(clearKey?'':undefined)};
    const saved=await api<ModelConfig>(id?'PUT':'POST',id?`/configs/${id}`:'/configs',payload);setId(saved.id);setDirty(false);return saved.id;
  };
  const save=async()=>{setWorking(true);try{const cid=await persist();await onSaved(cid);}catch(error){setResult((error as Error).message);}finally{setWorking(false);}};
  const test=async()=>{setWorking(true);setResult('正在验证连接和图片识别…');try{const cid=dirty||!id?await persist():id;const response=await api<{message:string}>('POST',`/configs/${cid}/test`);setResult(response.message);}catch(error){setResult((error as Error).message);}finally{setWorking(false);}};
  return <Modal title={model?'编辑模型配置':'添加模型配置'} onClose={onClose}><form onSubmit={e=>{e.preventDefault();save();}}><div className="form-grid"><label>配置名称<input autoFocus required maxLength={100} placeholder="例如：我的视觉模型" value={form.name} onChange={e=>change('name',e.target.value)}/></label><label>接口类型<select value={form.protocol} onChange={e=>change('protocol',e.target.value)}><option value="compatible">兼容 Chat Completions</option><option value="anthropic">Anthropic Messages</option><option value="gemini">Gemini GenerateContent</option></select></label></div><label className="form-field">API URL<input required type="url" placeholder={form.protocol==='gemini'?'https://generativelanguage.googleapis.com/v1beta':'https://example.com/v1'} value={form.url} onChange={e=>change('url',e.target.value)}/></label><label className="form-field">API Key<input type="password" autoComplete="off" placeholder={model?.has_key?'已安全保存；留空保留原密钥':'在此填写，本地无认证接口可留空'} value={form.api_key} onChange={e=>change('api_key',e.target.value)}/></label>{model?.has_key&&<label className="checkbox-label"><input type="checkbox" checked={clearKey} onChange={e=>{setClearKey(e.target.checked);setDirty(true);}}/>移除已保存的密钥</label>}<label className="form-field">模型名称<input required maxLength={200} placeholder="填写服务提供的视觉模型标识" value={form.model} onChange={e=>change('model',e.target.value)}/></label><details className="advanced"><summary>请求与上下文设置</summary><div className="form-grid"><label>Temperature（可留空）<input type="number" min={0} max={2} step={0.1} value={form.temperature} onChange={e=>change('temperature',e.target.value)}/></label><label>超时（秒）<input type="number" min={10} max={180} value={form.timeout} onChange={e=>change('timeout',Number(e.target.value))}/></label><label>上下文预算（估算 token）<input type="number" min={12000} max={1000000} value={form.context_tokens} onChange={e=>change('context_tokens',Number(e.target.value))}/></label></div><label className="checkbox-label"><input type="checkbox" checked={form.json_mode} onChange={e=>change('json_mode',e.target.checked)}/>服务支持时启用 JSON 模式</label><p className="form-hint">可选参数按接口实际支持情况配置；预算采用保守估算并预留图片与输出空间。</p></details>{result&&<div className="test-result" role="status">{working&&<LoaderCircle size={15} className="spin"/>}{result}</div>}<p className="form-hint">密钥保存在系统凭据管理器中。测试会先保存配置，再发送一张随机数字图片。</p><div className="modal-actions model-actions">{id&&<button type="button" className="text-button danger-text" disabled={working} onClick={async()=>{setWorking(true);try{await api('DELETE',`/configs/${id}`);await onDeleted();}catch(error){notify((error as Error).message);}finally{setWorking(false);}}}><Trash2 size={14}/>删除</button>}<button type="button" className="secondary" disabled={working||!form.name.trim()||!form.url||!form.model.trim()} onClick={test}><Cable size={15}/>测试连接</button><button className="primary" disabled={working}>{working?<LoaderCircle size={15} className="spin"/>:<Check size={15}/>}保存配置</button></div></form></Modal>;
}

function WindowDialog({onClose,onSelect,busy}:{onClose:()=>void;onSelect:(window:GameWindow)=>void;busy:boolean}){
  // 标题与进程名称供人辨认，绑定时提交句柄、PID 和创建时间，不只依赖标题匹配。
  const [windows,setWindows]=useState<GameWindow[]>([]);const [error,setError]=useState('');const [loading,setLoading]=useState(false);
  const refresh=async()=>{setLoading(true);try{setWindows(await api<GameWindow[]>('GET','/windows'));setError('');}catch(exc){setError((exc as Error).message);}finally{setLoading(false);}};
  useEffect(()=>{refresh();},[]);
  return <Modal title="选择游戏窗口" onClose={onClose} wide><div className="window-intro"><p>只观察和操作你选定的窗口。建议使用游戏的窗口模式。</p><button className="secondary compact" disabled={loading} onClick={refresh}><RotateCcw size={14}/>刷新</button></div><div className="window-list">{loading?<div className="empty-state"><LoaderCircle className="spin" size={20}/>正在查找窗口…</div>:error?<p className="test-result">{error}</p>:windows.length?windows.map(window=><button className="window-row" disabled={busy||window.minimized} key={window.hwnd} onClick={()=>onSelect(window)}><span className="window-icon"><Monitor size={21}/></span><span><strong>{window.title}</strong><small>{window.process_name} · {window.client[2]} × {window.client[3]}{window.minimized?' · 已最小化':''}</small></span><ArrowUpRight size={17}/></button>):<div className="empty-state"><Monitor size={25}/><p>还没有可选择的游戏窗口。<br/>打开游戏后点击刷新。</p></div>}</div></Modal>;
}

function ProfileDialog({profile,preview,onClose,onSave}:{profile:Profile;preview:string|null;onClose:()=>void;onSave:(profile:Profile)=>void}){
  // 编辑本地副本，保存才更新会话；在预览中标记只改变比例坐标，不向游戏发送鼠标。
  // 比例按图像容器实际显示尺寸换算，后台再映射到游戏的物理客户区。
  // 旧版允许保存3次，新版保留配置读取兼容，但界面与执行统一限制最多2次。
  const [value,setValue]=useState({...profile,max_action_retries:Math.min(profile.max_action_retries,2)});
  const pointControl=value.advance_key==='CLICK'&&<div className="form-field"><span>推进点击位置</span><p className="form-hint">在预览上点选你手动点击能推进的位置。标记只改变设置，不会操作游戏。</p>{preview&&<div className="advance-preview" role="button" tabIndex={0} aria-label="在游戏预览中标记推进位置" onClick={event=>{const box=event.currentTarget.getBoundingClientRect();setValue({...value,advance_point:[Math.max(0,Math.min(.999,(event.clientX-box.left)/box.width)),Math.max(0,Math.min(.999,(event.clientY-box.top)/box.height))]});}}><img src={preview} alt="标记推进位置的游戏预览"/><span style={{left:`${value.advance_point[0]*100}%`,top:`${value.advance_point[1]*100}%`}}>＋</span></div>}<div className="form-grid">{['横向位置','纵向位置'].map((label,index)=><label key={label}>{label}<input type="number" min={0} max={.999} step={.01} value={Number(value.advance_point[index].toFixed(3))} onChange={event=>{const point=[...value.advance_point];point[index]=Number(event.target.value);setValue({...value,advance_point:point});}}/></label>)}</div></div>;
  return <Modal title="游戏操作设置" onClose={onClose}><p className="form-hint">模型判断当前画面应等待、推进或选择。这里配置实际输入方式和操作节奏。</p><label className="form-field">推进方式<select value={value.advance_key} onChange={e=>setValue({...value,advance_key:e.target.value})}><option value="SPACE">空格</option><option value="ENTER">回车</option><option value="CLICK">点击对话区域</option></select></label>{pointControl}<div className="form-grid"><label>操作间隔（毫秒）<input type="number" min={500} max={15000} value={value.action_interval_ms} onChange={e=>setValue({...value,action_interval_ms:Number(e.target.value)})}/></label><label>原剧情最多重试次数<input type="number" min={0} max={2} value={value.max_action_retries} onChange={e=>setValue({...value,max_action_retries:Number(e.target.value)})}/></label></div><div className="form-grid"><label>移动后等待（毫秒）<input type="number" min={20} max={1000} value={value.move_settle_ms} onChange={e=>setValue({...value,move_settle_ms:Number(e.target.value)})}/></label><label>按住时间（毫秒）<input type="number" min={30} max={1000} value={value.input_hold_ms} onChange={e=>setValue({...value,input_hold_ms:Number(e.target.value)})}/></label></div><p className="form-hint">每轮由模型决定动作，选择等待时不会自动补点。转场、文字显示或加载中等待2秒后重读，最多额外2次；模型决定重复原操作时，最多额外2次。设为0可关闭操作重试。</p><div className="form-field">允许的按键<div className="key-options">{['SPACE','ENTER','UP','DOWN','LEFT','RIGHT','ESCAPE'].map(key=><label className="checkbox-label" key={key}><input type="checkbox" checked={value.allowed_keys.includes(key)} onChange={e=>setValue({...value,allowed_keys:e.target.checked?[...value.allowed_keys,key]:value.allowed_keys.filter(k=>k!==key)})}/>{key}</label>)}</div></div><div className="modal-actions"><button className="secondary" onClick={onClose}>取消</button><button className="primary" onClick={()=>onSave(value)}><Check size={15}/>保存设置</button></div></Modal>;
}
