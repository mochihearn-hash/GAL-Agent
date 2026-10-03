# LangChain Agent 的游戏流程中间件：观察 → 一次视觉决策 → 校验 → 执行/等待/暂停。
# 每个会话使用独立的状态；PNG 只存在于当前调用，长期记忆继续保存到现有 SQLite。
from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from contextvars import ContextVar
from typing import Any

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import HumanMessage
from langsmith import tracing_context
from pydantic import ValidationError

from app.capture.windows import WindowError
from app.capture.wgc import ResizeRequired
from app.providers.client import ProviderError, SYSTEM_PROMPT, decode_json
from app.providers.langchain_model import GameChatModel
from app.schemas import Decision, GameProfile
from app.storage.store import uid
from .memory import context, update_memory, needs_summary, compress

# LangChain 会为节点创建子任务。内部请求暂停时不能把所属的顶层任务再次取消；
# 外部按钮的暂停不携带该上下文，仍然立即取消整条执行流程。
AGENT_TASK_OWNER = ContextVar("gal_agent_task_owner", default=None)


class GameAgentState(AgentState):
    # pending 只描述已发送的输入；等待重读和输入重试始终分别计数。
    pending: dict | None
    wait_retries: int
    turn: int
    outcome: str
    observation: dict


@dataclass
class GameAgentContext:
    # 进程对象通过 context 注入，不序列化到状态；凭据只留在模型适配器私有属性。
    control: Any
    sid: str
    version: int
    rid: str
    config: dict
    profile: GameProfile
    model: GameChatModel


class GameTurnMiddleware(AgentMiddleware):
    state_schema = GameAgentState

    @staticmethod
    def finish(pending, wait_retries, turn, outcome):
        return {"pending": pending, "wait_retries": wait_retries, "turn": turn,
                "outcome": outcome, "observation": {}, "jump_to": "end"}

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        # 观察节点还负责摘要和焦点等待；不就绪时直接结束本次调用，不消耗视觉请求。
        ctx = runtime.context
        control, sid, version, rid = ctx.control, ctx.sid, ctx.version, ctx.rid
        config, profile, provider = ctx.config, ctx.profile, ctx.model
        pending, wait_retries, turn = state["pending"], state["wait_retries"], state["turn"]
        if not control.valid(sid, version):
            return self.finish(pending, wait_retries, turn, "stop")
        if not await control._wait_for_focus(sid,version):
            return self.finish(pending, wait_retries, turn, "stop")
        try:
            shot = control.capture.snapshot()
        except ResizeRequired as exc:
            control.status("reading",str(exc))
            await asyncio.to_thread(control.capture.rebind_if_current,exc.target)
            return self.finish(pending, wait_retries, turn, "continue")
        except WindowError as exc:
            if "等待" in str(exc):
                control.status("reading",str(exc))
                await asyncio.sleep(.4)
                return self.finish(pending, wait_retries, turn, "continue")
            raise
        if pending and (shot.window.client,shot.window.frame,shot.window.dpi) != (pending["baseline"].window.client,pending["baseline"].window.frame,pending["baseline"].window.dpi):
            raise WindowError("操作后窗口位置或尺寸变化，请核对游戏进度后继续")
        session = control.store.get_session(sid)
        memory = session["memory"]
        if needs_summary(memory,config):
            control.status("thinking","正在整理此前剧情，原始记录会完整保留")
            compact,response = await compress(memory,provider,
                on_attempt=lambda data:control.log(sid,"summary_attempt",data,rid),
                valid=lambda:control.valid(sid,version))
            if not control.valid(sid,version):
                return self.finish(pending, wait_retries, turn, "stop")
            control.store.update_session(sid,memory=compact)
            control.log(sid,"summary",{"summary":compact["chapters"][-1],"usage":response["usage"],"elapsed_ms":response["elapsed_ms"],"input":response.get("summary_input"),"raw_text":response["text"]},rid)
            control.store.checkpoint(sid,"剧情摘要检查点")
            return self.finish(pending, wait_retries, turn, "continue")
        oid = uid()
        image = control.store.save_image(sid,oid,shot.png())
        control.log(sid,"observation",{"observation_id":oid,"image":image,"window":shot.window.public()},rid)
        story_context = context(memory,session["guidance"],config)
        previous_action = None
        if pending:
            previous = pending["decision"]
            previous_action = {"scene":{"screen_type":previous.screen_type,"speaker":previous.speaker,
                                       "dialogue":previous.dialogue,"options":[o.model_dump() for o in previous.options]},
                "action":previous.action.model_dump(),"execution":pending["action"]["payload"].get("execution"),
                "attempt":pending["attempt"]+1,"max_retries":min(profile.max_action_retries,2)}
        turn += 1
        request_text = json.dumps({"observation_id":oid,"run_version":version,"context":json.loads(story_context),
            "advance_method":profile.advance_key,"advance_point":profile.advance_point,"allowed_keys":profile.allowed_keys,
            "previous_action":previous_action,"wait_retry":{"used":wait_retries,"limit":control.MAX_WAIT_RETRIES,"delay_seconds":control.WAIT_SECONDS},
            "response_schema":Decision.model_json_schema()},ensure_ascii=False)
        control.log(sid,"request",{"observation_id":oid,"image":image,"system_prompt":SYSTEM_PROMPT,"input":request_text,"prompt_version":"4","turn":turn,"engine":"langchain"},rid)
        control.status("thinking","模型正在判断画面、上一操作和下一动作",next_action=None)
        focus_version = control.focus_generation
        return {"messages": [HumanMessage(content=[
            {"type": "text", "text": request_text},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(shot.png()).decode("ascii")}},
        ])], "pending": pending, "wait_retries": wait_retries, "turn": turn,
            "observation": {"shot": shot, "image": image, "memory": memory, "oid": oid, "focus_version": focus_version},
            "outcome": "observed"}

    async def awrap_model_call(self, request, handler):
        # 一个调用只运行一次模型节点。JSON 格式错误不启用框架自动修复，避免增加请求。
        ctx = request.runtime.context
        if not ctx.control.valid(ctx.sid, ctx.version):
            raise asyncio.CancelledError()
        return await handler(request)

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state, runtime):
        # 所有真实输入都位于模型返回后的校验节点；迟到响应不能触发鼠标或键盘。
        ctx = runtime.context
        control, sid, version, rid, profile = ctx.control, ctx.sid, ctx.version, ctx.rid, ctx.profile
        pending, wait_retries, turn = state["pending"], state["wait_retries"], state["turn"]
        observation = state["observation"]
        shot, image, memory, oid, focus_version = (observation[k] for k in ("shot", "image", "memory", "oid", "focus_version"))
        message = state["messages"][-1]
        response = {"text": message.text, **message.response_metadata}
        if not control.valid(sid,version):
            return self.finish(pending, wait_retries, turn, "stop")
        # 保存格式失败的原始回复和具体字段，避免只留一句无法定位的统一错误。
        # 不增加格式修复请求；每轮仍只有一个视觉请求，失败后没有任何输入。
        try:
            decision = Decision.model_validate(decode_json(response["text"]))
            if pending and decision.previous_action_result is None:
                raise ProviderError("format","缺少 previous_action_result，无法判断上一操作结果")
        except (ValidationError,ProviderError) as exc:
            errors = [{"field":".".join(str(x) for x in item["loc"]),"message":item["msg"]} for item in exc.errors()] if isinstance(exc,ValidationError) else [{"field":"previous_action_result" if "previous_action_result" in str(exc) else "JSON","message":str(exc)}]
            control.log(sid,"decision_error",{"image":image,"observation_id":oid,"raw_text":response["text"],"errors":errors,
                                          "elapsed_ms":response.get("elapsed_ms"),"usage":response.get("usage",{})},rid)
            fields = ", ".join(dict.fromkeys(item["field"] or "action/screen_type" for item in errors))
            raise ProviderError("format",f"模型动作格式不符合约定（{fields[:160]}），原始回复已保存，未执行输入") from None
        if decision.observation_id != oid or decision.run_version != version:
            raise ProviderError("stale","模型返回了过期的观察或运行版本，未执行动作")
        event = control.log(sid,"decision",{**decision.model_dump(),"usage":response["usage"],"elapsed_ms":response["elapsed_ms"],"raw_text":response["text"]},rid)
        if not await control._wait_for_focus(sid,version):
            return self.finish(pending, wait_retries, turn, "stop")
        if focus_version != control.focus_generation:
            # 失焦期间可能有人接管，必须用恢复后的新截图决策；普通动画不走此分支。
            control.log(sid,"discarded",{"message":"模型请求期间游戏失焦，恢复后重新截图决策","decision_id":event["id"]},rid)
            return self.finish(pending, wait_retries, turn, "continue")
        fresh = control.capture.snapshot()
        if (fresh.window.client,fresh.window.frame,fresh.window.dpi) != (shot.window.client,shot.window.frame,shot.window.dpi):
            raise WindowError("窗口位置或尺寸变化，请重新观察后继续")
        verdict = control._record_feedback(sid,rid,pending,decision,event,image) if pending else None
        control.status("reading","模型已判断当前画面",understanding=decision.understanding,reason=decision.reason,next_action=decision.action.model_dump())
        if verdict == "changed":
            control.store.update_session(sid,needs_reconcile=0)
            pending = None
            wait_retries = 0
        # 显示/转场/加载不完整时自动等待；完整对白也尊重模型的 wait，绝不补点。
        # 等待与重复操作独立计数，期间不写入未完整或重复的剧情记忆。
        waiting = decision.screen_type in {"typing","transition","loading"} or decision.action.type == "wait"
        if waiting:
            if wait_retries >= control.MAX_WAIT_RETRIES:
                if pending:
                    payload = pending["action"]["payload"]
                    payload.update(phase="unconfirmed",result={"message":"等待重读2次后仍未就绪，未追加输入"})
                    control.store.replace_event_payload(sid,pending["action"]["id"],payload)
                await control.pause("等待并重新读取 2 次后模型仍判断应等待，请接管或给出引导")
                return self.finish(pending, wait_retries, turn, "stop")
            wait_retries += 1
            control.log(sid,"wait_retry",{"message":f"模型判断应等待，2秒后重新读取（{wait_retries}/2）","attempt":wait_retries,"delay_seconds":control.WAIT_SECONDS,"image":image,"screen_type":decision.screen_type},rid)
            control.status("reading",f"模型判断应等待，2秒后重新读取（{wait_retries}/2）",next_action={"type":"wait"})
            await asyncio.sleep(control.WAIT_SECONDS)
            return self.finish(pending, wait_retries, turn, "continue")
        wait_retries = 0
        if decision.action.type == "request_help":
            if pending is None and decision.screen_type in {"dialogue","choice"}:
                control.store.update_session(sid,memory=update_memory(memory,decision,event))
            if pending and verdict in {"pending","uncertain"}:
                payload = pending["action"]["payload"]
                payload.update(phase="unconfirmed",result={"message":"模型请求帮助，未追加输入"})
                control.store.replace_event_payload(sid,pending["action"]["id"],payload)
            await control.pause(decision.reason or "模型需要你的引导")
            return self.finish(pending, wait_retries, turn, "stop")
        if verdict == "same":
            if pending["attempt"] >= min(profile.max_action_retries,2):
                await control.pause(f"重试 {min(profile.max_action_retries,2)} 次后仍未观察到剧情变化，请接管或给出引导")
                return self.finish(pending, wait_retries, turn, "stop")
            if not control._repeat_matches(pending["original_decision"],decision):
                await control.pause("模型对未完成的原操作提出了不同动作或分支，请核对后继续")
                return self.finish(pending, wait_retries, turn, "stop")
            # 执行当前模型提出的重复动作，使用当前选项坐标，保留原动作链及累计次数。
            action_event = await control._execute_decision(sid,version,rid,decision,event,fresh,profile,
                attempt=pending["attempt"]+1,root_id=pending["root_id"],original_decision_id=pending["original_event_id"])
            if action_event:
                pending.update(action=action_event,decision=decision,attempt=pending["attempt"]+1)
        else:
            if decision.screen_type in {"dialogue","choice"}:
                control.store.update_session(sid,memory=update_memory(memory,decision,event))
            action_event = await control._execute_decision(sid,version,rid,decision,event,fresh,profile)
            if action_event:
                pending = {"decision":decision,"original_decision":decision,"original_event_id":event["id"],
                           "action":action_event,"root_id":action_event["id"],"baseline":fresh,"attempt":0}
        if not control.valid(sid,version):
            return self.finish(pending, wait_retries, turn, "stop")
        if action_event:
            # 仅保留用户设置的操作间隔；结束后立即取最新截图，不再等像素稳定。
            await asyncio.sleep(profile.action_interval_ms/1000)
        return self.finish(pending, wait_retries, turn, "continue")


class GameAgent:
    """LangChain 创建的单次观察 Agent；桌面负责持续调度与紧急暂停。"""

    def __init__(self, control, sid, version, rid, config, profile, provider):
        model = GameChatModel(provider)
        self.context = GameAgentContext(control, sid, version, rid, config, profile, model)
        # 不启用可重放的图检查点：真实游戏输入不可事务回滚，恢复必须重新观察和核对。
        # 现有 SQLite 已保存剧情/动作/检查点；不累积框架 messages，防止长游戏重复上传图片。
        self.agent = create_agent(model=model, tools=[], system_prompt=SYSTEM_PROMPT,
                                  middleware=[GameTurnMiddleware()], state_schema=GameAgentState,
                                  context_schema=GameAgentContext, name="gal_game_agent")

    async def run_turn(self, pending, wait_retries, turn):
        # 禁止环境中的跟踪开关把游戏截图或剧情发送到额外服务；本地事件日志继续完整保留。
        token = AGENT_TASK_OWNER.set(asyncio.current_task())
        try:
            with tracing_context(enabled=False):
                return await self.agent.ainvoke({"messages": [], "pending": pending,
                    "wait_retries": wait_retries, "turn": turn, "outcome": "start", "observation": {}, "jump_to": None},
                    context=self.context, config={"recursion_limit": 12})
        finally:
            AGENT_TASK_OWNER.reset(token)
