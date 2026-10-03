# Agent 的运行调度中心：截图 → 模型判断状态/反馈/动作 → 校验权限 → 执行 → 下一轮截图。
# 本模块不直接实现鼠标或截屏；窗口、捕获、执行、模型和存储都通过注入的对象协作。
# 最重要的约束是：任何异步等待之后，都不能把旧会话或已暂停任务的响应继续用于操作。
from __future__ import annotations

import asyncio
import os
import time

import psutil

from app.capture.windows import WindowError, FocusLost
from app.providers.client import Provider, ProviderError
from app.schemas import GameProfile
from app.storage.store import uid, now
from .engine import GameAgent, AGENT_TASK_OWNER


class Runtime:
    # 初次读取不计入重读次数；加载/逐字显示最多再等两次，每次两秒，期间不发送输入。
    WAIT_SECONDS = 2
    MAX_WAIT_RETRIES = 2
    # 一个应用进程只运行一个托管任务；不同游戏的记忆仍分别保存在各自会话中。
    def __init__(self,store,credentials,windows,capture,executor,provider_factory=Provider):
        self.store,self.credentials,self.windows = store,credentials,windows
        self.capture,self.executor,self.provider_factory = capture,executor,provider_factory
        # active_id 是界面当前选择的会话，bound_id 是当前截屏窗口所属的会话。
        # 分开保存可以防止切换会话后，错误地继续操作上一款游戏。
        self.active_id = None
        self.bound_id = None
        self.task = None
        # 运行版本是异步任务的失效标记。暂停、切换、重新开始都会递增版本；
        # 即使模型请求无法立即取消，返回结果也必须通过版本检查才能生效。
        self.generation = 0
        # 失焦不结束运行，但失焦期间游戏可能被人操作，恢复后重新观察而不执行旧截图的动作。
        self.focus_generation = 0
        self.state = {"session_id":None,"status":"paused","message":"选择或新建会话，开始一段剧情","understanding":"","reason":"","next_action":None,"window":None,"execution":None}
        self.clients = set()
        self.last_heartbeat = time.monotonic()
        self.watchdog_task = None
        self.closed = False
        # 串行化开始、绑定、配置等控制命令；紧急暂停不等待该锁，以便尽快释放输入。
        self.command_lock = asyncio.Lock()
        self.parent_pid = int(os.environ.get("GAL_AGENT_PARENT_PID","0"))

    async def launch(self):
        self.watchdog_task = asyncio.create_task(self._watchdog())

    def broadcast(self,event):
        # 每个 WebSocket 订阅者有独立队列；队列满时丢弃最早通知，避免慢界面阻塞 Agent。
        # 完整记录以 SQLite 为准，通知队列只负责提醒界面刷新。
        for queue in list(self.clients):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    def status(self,status,message,**fields):
        self.state.update(status=status,message=message,**fields)
        self.broadcast({"type":"status","data":self.public_state()})

    def public_state(self):
        return {**self.state,"run_version":self.generation,"bound_session_id":self.bound_id}

    def log(self,sid,kind,payload,rid=None):
        # 先持久化，再通知前端；界面断开不会导致已经生成的剧情记录丢失。
        event = self.store.event(sid,kind,payload,rid)
        self.broadcast({"type":"event","session_id":sid,"data":event})
        return event

    def valid(self,sid,version):
        # 同时检查任务版本、会话身份和桌面心跳。会话被删除时，迟到任务也必须停止。
        if self.closed or self.active_id != sid or self.generation != version or time.monotonic()-self.last_heartbeat > 8:
            return False
        try:
            self.store.get_session(sid)
        except KeyError:
            return False
        return True

    async def pause(self,message="已暂停，后续动作已停止",checkpoint=True):
        # 先使旧任务失效并释放程序按下的键，再取消协程；不等待模型服务主动退出。
        # 暂停只停止 Agent 并保存它的记忆，不会替游戏执行存档操作。
        sid = self.active_id
        self.generation += 1
        task,self.task = self.task,None
        self.executor.release()
        if task and task is not asyncio.current_task() and task is not AGENT_TASK_OWNER.get():
            task.cancel()
            # Tasks have version gates even if a provider ignores cancellation.
            task.add_done_callback(self._consume_task)
        self.status("paused",message,next_action=None)
        if sid:
            try:
                if checkpoint:
                    self.store.checkpoint(sid,"暂停时自动保存")
                self.log(sid,"pause",{"message":message})
            except KeyError:
                pass

    @staticmethod
    def _consume_task(task):
        if not task.cancelled():
            task.exception()

    async def select(self,sid):
        # 选中同一会话不会重复清空绑定；真正切换时必须停旧任务、关旧捕获。
        self.store.get_session(sid)
        if self.active_id != sid:
            await self.pause("已切换会话")
            self.capture.close()
            self.bound_id = None
            self.active_id = sid
            self.state.update(session_id=sid,window=None,understanding="",reason="",execution=None)
            self.status("paused","已加载记忆，请选择游戏窗口并核对进度")

    async def bind(self,sid,binding):
        # 不仅比对 HWND，还核对 PID 和进程创建时间，避免系统复用窗口句柄后误操作。
        # 图形捕获启动可能阻塞，放在线程中；完成后再次核对版本，防止绑定期间发生切换。
        await self.select(sid)
        await self.pause("正在连接游戏窗口",checkpoint=False)
        window = self.windows.info(binding.hwnd)
        if window.pid != binding.pid or window.process_started != binding.process_started:
            raise WindowError("窗口身份已变化，请刷新后重新选择")
        version = self.generation
        await asyncio.to_thread(self.capture.bind,window)
        if self.generation != version or self.active_id != sid:
            self.capture.close()
            raise WindowError("窗口绑定已取消")
        self.bound_id = sid
        self.store.update_session(sid,last_window=window.public())
        self.state["window"] = window.public()
        self.status("paused","窗口已连接，请核对当前画面后开始")
        self.log(sid,"bind",{"window":window.public()})

    async def start(self,sid):
        # 开始前只验证本地条件，不自动清除进度对齐标记，也不主动抢游戏焦点。
        # 密钥在这里通过凭据引用读取；运行记录不会包含密钥或凭据引用。
        if self.task and not self.task.done() and self.active_id == sid:
            raise ValueError("此会话已经运行")
        await self.select(sid)
        session = self.store.get_session(sid)
        if self.bound_id != sid or not self.capture.target:
            raise ValueError("请重新选择此会话的游戏窗口")
        if session["needs_reconcile"]:
            raise ValueError("请先核对游戏存档与恢复位置，并点击“进度已对齐”")
        cid = session["settings"].get("config_id")
        if not cid:
            raise ValueError("请选择模型配置")
        config = self.store.get_config(cid)
        secret = self.credentials.get(config["credential_ref"]) if config.get("credential_ref") else ""
        profile = GameProfile.model_validate(session["settings"].get("profile",{}))
        self.windows.verify(self.capture.target)
        if time.monotonic()-self.last_heartbeat > 8:
            raise ValueError("桌面控制连接已断开，请重新打开应用")
        self.generation += 1
        version = self.generation
        rid = self.store.create_run(sid,config)
        self.log(sid,"run",{"run_id":rid,"model":config["model"],"protocol":config["protocol"],"prompt_version":"4","engine":"langchain","message":"开始运行；请切回游戏窗口"},rid)
        self.status("waiting_focus","请切回游戏窗口，Agent 将开始阅读")
        self.task = asyncio.create_task(self._loop(sid,version,rid,config,secret,profile))
        self.task.add_done_callback(self._consume_task)

    async def _watchdog(self):
        # 独立于模型请求的看门狗：桌面退出、心跳超时、窗口失效都能及时停止输入。
        # 普通失焦只进入等待；主动暂停会使任务版本失效，两者不能混为一谈。
        while not self.closed:
            await asyncio.sleep(.4)
            if self.parent_pid and not psutil.pid_exists(self.parent_pid):
                await self.close()
                return
            if self.task and not self.task.done():
                if time.monotonic()-self.last_heartbeat > 8:
                    await self.pause("控制连接已断开，已停止操作并保存")
                elif self.capture.target:
                    try:
                        self.windows.verify(self.capture.target)
                        if not self.windows.foreground(self.capture.target.hwnd):
                            if self.state["status"] != "waiting_focus":
                                self.focus_generation += 1
                                self.executor.release()
                                self.status("waiting_focus","游戏失去焦点，已停止输入；切回游戏后会核对画面并继续")
                    except WindowError as exc:
                        await self.pause(str(exc))

    async def guide(self,sid,text,scope):
        # 人的引导会先暂停当前会话，避免模型在引导写入前继续执行旧判断。
        # next 引导只作用于下一次实际操作，persistent 引导保留到用户取消。
        if self.active_id == sid:
            await self.pause("已收到引导，请核对后继续")
        session = self.store.get_session(sid)
        item = {"id":uid(),"text":text,"scope":scope,"created_at":now()}
        guidance = session["guidance"]+[item]
        if sum(len(x["text"]) for x in guidance) > 8000:
            raise ValueError("有效引导过长，请先取消不再需要的引导")
        self.store.update_session(sid,guidance=guidance)
        self.log(sid,"guidance",item)
        self.store.checkpoint(sid,"引导后自动保存")
        return item

    async def _wait_for_focus(self,sid,version):
        # 只等待用户把游戏切回前台，不调用强制激活窗口的接口。
        # 每次等待仍检查版本和窗口身份，保证暂停后不会因重新获得焦点自动恢复。
        interrupted = False
        while self.valid(sid,version):
            self.windows.verify(self.capture.target)
            if self.windows.foreground(self.capture.target.hwnd):
                if self.state["status"] == "waiting_focus":
                    self.status("reading","游戏已回到前台，正在核对画面与待完成动作")
                return True
            if self.state["status"] != "waiting_focus":
                self.executor.release()
                self.status("waiting_focus","游戏失去焦点，已停止输入；切回游戏后会核对画面并继续")
            if not interrupted:
                self.focus_generation += 1
                interrupted = True
            await asyncio.sleep(.2)
        return False

    def _record_feedback(self,sid,rid,pending,decision,event,image):
        # 剧情状态由模型在本轮响应中给出，不再计算像素差或按台词字符串否定判断。
        # 这里只持久化反馈，标记上一输入结果；实际新动作仍经过结构和窗口权限检查。
        verdict = decision.previous_action_result
        self.log(sid,"feedback",{"image":image,"verdict":verdict,"confirmation":"model_decision",
                                 "decision_id":event["id"],"action_id":pending["action"]["id"],
                                 "observed":{"screen_type":decision.screen_type,"speaker":decision.speaker,
                                             "dialogue":decision.dialogue,"options":[x.model_dump() for x in decision.options]}},rid)
        if verdict in {"same","changed"}:
            payload = pending["action"]["payload"]
            payload.update(phase="observed",result={"content_changed":verdict=="changed","confirmation":"model_decision",
                "observation_id":decision.observation_id,"decision_id":event["id"]})
            self.store.replace_event_payload(sid,pending["action"]["id"],payload)
        return verdict

    @staticmethod
    def _repeat_matches(previous,current):
        # 同一选择尚未确认成功时，重复动作不能改投其他分支；当前按钮坐标可以更新。
        # 这约束动作身份，不判断剧情是否推进，不检查像素，也不覆盖模型的 wait 决策。
        if previous.action.type != current.action.type:
            return False
        if current.action.type == "choose_option":
            old = previous.options[previous.action.option_index-1].text
            new = current.options[current.action.option_index-1].text
            return "".join(old.split()) == "".join(new.split())
        return previous.action.type == "advance"

    async def _execute_decision(self,sid,version,rid,decision,event,baseline,profile,attempt=0,root_id=None,original_decision_id=None):
        # 这里只提交一次输入。是否重试由下一轮决策核对结果决定，绝不由像素差直接触发。
        # attempt 为已进行的额外重试次数；失焦且未发送不占次数，部分发送失败不重试。
        valid = lambda:self.valid(sid,version)
        while valid():
            focus_version = self.focus_generation
            if not await self._wait_for_focus(sid,version):
                return None
            if focus_version != self.focus_generation:
                self.log(sid,"discarded",{"message":"发送前失焦，恢复后重新截图决策"},rid)
                return None
            fresh = self.capture.snapshot()
            if (fresh.window.client,fresh.window.frame,fresh.window.dpi) != (baseline.window.client,baseline.window.frame,baseline.window.dpi):
                raise WindowError("窗口位置或尺寸变化，待完成动作已停止，请重新观察后继续")
            try:
                self.executor.check(fresh,decision.action,decision.options,profile,valid)
            except FocusLost:
                if not await self._wait_for_focus(sid,version):
                    return None
                self.log(sid,"discarded",{"message":"输入检查时失焦，恢复后重新截图决策"},rid)
                return None
            image = self.store.save_image(sid,uid(),fresh.png()) if attempt else None
            # 每次输入都有独立持久记录；保留首次决策及动作引用，便于分辨读取与实际重试。
            payload = {"phase":"proposed","action":decision.action.model_dump(),"observation_id":decision.observation_id,
                       "decision_id":event["id"],"run_version":version,"attempt":attempt+1,"retry_of":root_id}
            if image:
                payload.update(retry_image=image,original_decision_id=original_decision_id)
                self.log(sid,"retry",{"message":f"下一轮确认仍是原剧情，重试上一动作（{attempt}/{min(profile.max_action_retries,2)}）",
                                      "decision_id":event["id"],"attempt":attempt+1,"image":image},rid)
            action_event = self.log(sid,"action",payload,rid)
            for phase in ("validated","prepared"):
                payload["phase"] = phase
                self.store.replace_event_payload(sid,action_event["id"],payload)
            previous_reconcile = self.store.get_session(sid)["needs_reconcile"]
            # 输入和游戏反馈不能作为同一数据库事务提交，发送前先标记进度待核对。
            self.store.update_session(sid,needs_reconcile=1)
            try:
                if hasattr(self.executor,"execute_async"):
                    result = await self.executor.execute_async(fresh,decision.action,decision.options,profile,valid)
                else:
                    result = self.executor.execute(fresh,decision.action,decision.options,profile,valid)
            except FocusLost:
                payload.update(phase="abandoned",result={"sent":False,"message":"发送前失焦，保留决策等待恢复"})
                self.store.replace_event_payload(sid,action_event["id"],payload)
                self.store.update_session(sid,needs_reconcile=previous_reconcile)
                if not await self._wait_for_focus(sid,version):
                    return None
                self.log(sid,"discarded",{"message":"尚未发送即失焦，恢复后重新截图决策"},rid)
                return None
            payload.update(phase="sent",execution=result)
            self.store.replace_event_payload(sid,action_event["id"],payload)
            action_event["payload"] = payload
            if not attempt:
                session = self.store.get_session(sid)
                guidance = [g for g in session["guidance"] if g["scope"] != "next"]
                memory = session["memory"]
                if memory["recent"] and memory["recent"][-1].get("source_id") == event["id"]:
                    memory["recent"][-1]["executed_action"] = decision.action.model_dump()
                self.store.update_session(sid,guidance=guidance,memory=memory)
            self.store.checkpoint(sid,"操作后自动保存")
            self.broadcast({"type":"refresh","session_id":sid})
            self.status("acting","系统已接收输入，下一轮读取将同时核对剧情反馈",next_action=decision.action.model_dump(),execution=result)
            return action_event
        return None

    async def _loop(self,sid,version,rid,config,secret,profile):
        # 生命周期仍由桌面控制；每次观察、决策和动作分支由 LangChain 中间件调度。
        # 不恢复旧框架调用或重放输入。跨重启恢复继续使用 SQLite 检查点并人工确认对齐。
        pending, wait_retries, turn = None, 0, 0
        try:
            engine = GameAgent(self,sid,version,rid,config,profile,self.provider_factory(config,secret))
            while self.valid(sid,version):
                state = await engine.run_turn(pending,wait_retries,turn)
                pending, wait_retries, turn = state["pending"],state["wait_retries"],state["turn"]
                if state["outcome"] == "stop":
                    return
        # 取消属于正常的暂停流程；其他错误写入历史并停止任务，不在异常路径补发输入。
        except asyncio.CancelledError:
            return
        except (ProviderError,WindowError,ValueError) as exc:
            if self.valid(sid,version):
                self.log(sid,"error",{"message":str(exc),"code":getattr(exc,"code","runtime")},rid)
                await self.pause(str(exc))
        except Exception:
            if self.valid(sid,version):
                self.log(sid,"error",{"message":"运行遇到异常，已停止输入。请重新绑定窗口后继续。","code":"internal"},rid)
                await self.pause("运行遇到异常，已停止输入并保存")


    async def close(self):
        # 退出顺序为暂停保存、关闭捕获、停止看门狗、再次释放输入；允许重复调用。
        if self.closed:
            return
        await self.pause("应用已停止运行并保存")
        self.closed = True
        self.capture.close()
        if self.watchdog_task:
            self.watchdog_task.cancel()
        self.executor.release()
