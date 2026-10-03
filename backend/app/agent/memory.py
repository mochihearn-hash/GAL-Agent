# 每个会话的剧情记忆：近期原文 + 累积章节摘要 + 事实/推测/问题及来源。
# 修改都在深拷贝上进行；尤其摘要请求失败时，不能污染数据库中仍有效的原记忆。
from __future__ import annotations

import copy
import json
import asyncio
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from app.providers.client import SUMMARY_PROMPT, decode_json
from app.schemas import Decision


SummaryItem = Annotated[StrictStr,Field(min_length=1,max_length=500)]


class SummaryResult(BaseModel):
    # 校验结构与单项长度，同时提供 JSON Schema 给模型；不把对象或数字自动转成字符串。
    model_config = ConfigDict(extra="forbid")
    summary: StrictStr = Field(min_length=1,max_length=2400)
    facts: list[SummaryItem] = Field(max_length=12)
    hypotheses: list[SummaryItem] = Field(max_length=12)
    questions: list[SummaryItem] = Field(max_length=12)

    def check_text(self):
        if not self.summary.strip():
            raise ValueError("summary 必须是非空字符串")
        for key in ("facts","hypotheses","questions"):
            if any(not text.strip() or len(text)>500 for text in getattr(self,key)):
                raise ValueError(f"{key} 每项必须是非空字符串，最多500字")


def memory_budget(config):
    # 按字符做保守预算，预留图片、输出、提示词和动作 Schema；不是供应商精确 token 计数。
    schema_size = len(json.dumps(Decision.model_json_schema(),ensure_ascii=False))
    return max(1000,(config.get("context_tokens",32000)-6000)//2-schema_size-900)


def for_context(memory):
    # 来源 ID 可持续增长，保留在本地用于追溯，不把完整历史来源列表重复发给模型。
    result = copy.deepcopy(memory)
    for chapter in result["chapters"]:
        chapter.pop("source_ids",None)
    return result


def update_memory(memory,decision,event):
    # 同一段对白连续观察时不重复加入 recent；事实条目按文本去重并保存首次来源。
    # 选项还会记录模型提议的编号及理由，实际执行结果由运行层另外登记。
    result = copy.deepcopy(memory)
    recent = result["recent"]
    entry = {"source_seq":event["seq"],"source_id":event["id"],"speaker":decision.speaker,"dialogue":decision.dialogue,"understanding":decision.understanding}
    if decision.screen_type == "choice":
        entry["options"] = [x.text for x in decision.options]
        entry["proposed_choice"] = decision.action.option_index
        entry["reason"] = decision.reason
    if decision.dialogue or decision.screen_type == "choice":
        previous = recent[-1] if recent else None
        if not previous or (previous.get("dialogue"),previous.get("speaker"),previous.get("options")) != (entry.get("dialogue"),entry.get("speaker"),entry.get("options")):
            recent.append(entry)
    for key in ("facts","hypotheses","questions"):
        existing = {item["text"] for item in result[key]}
        for text in getattr(decision,key):
            if text not in existing:
                result[key].append({"text":text,"source_seq":event["seq"],"source_id":event["id"]})
                existing.add(text)
    return result


def context(memory,guidance,config):
    # 有效引导与剧情记忆一起发送；超出预算时要求压缩，不能默默截掉最新剧情。
    # Conservative estimate; images/output/schema have their own reserved space.
    maximum_chars = memory_budget(config)
    result = for_context(memory)
    result["guidance"] = guidance
    encoded = json.dumps(result,ensure_ascii=False)
    if len(encoded) > maximum_chars:
        raise ValueError("剧情上下文需要压缩，暂不发送超出预算的请求")
    return encoded


def needs_summary(memory,config):
    # 两个触发条件：近期记录达到16条，或估算上下文接近预算上限。
    size = len(json.dumps(for_context(memory),ensure_ascii=False))
    threshold = max(800,memory_budget(config)-800)
    return len(memory["recent"]) >= 16 or size > threshold


async def compress(memory,provider,on_attempt=None,valid=lambda:True):
    # 每批最多压缩12条，留下较新的原文；on_attempt 负责记录失败响应，不依赖函数写数据库。
    # valid 在请求前后检查暂停/切换，防止迟到摘要覆盖用户恢复后的记忆。
    result = copy.deepcopy(memory)
    if not result["recent"]:
        raise ValueError("长期记忆已达到预算，请提高上下文预算后继续")
    # Bounded batches keep summarization requests within the configured budget.
    budget = max(2000,(provider.config.get("context_tokens",32000)-6000)//2-600)
    repair_reserve = min(1500,max(200,budget//10))
    # 预留少量修正空间，并将带来源的对象转成文本数组，避免模型照抄对象格式。
    previous = for_context(result)
    previous.pop("recent",None)
    for key in ("facts","hypotheses","questions"):
        previous[key] = [item["text"] for item in previous[key]]
    previous_size = len(json.dumps(previous,ensure_ascii=False))+len(json.dumps(SummaryResult.model_json_schema(),ensure_ascii=False))
    batch = []
    for item in result["recent"]:
        if len(json.dumps(batch+[item],ensure_ascii=False)) > budget-previous_size-500-repair_reserve:
            break
        batch.append(item)
        if len(batch) >= 12:
            break
    if not batch:
        raise ValueError("单段剧情过长，请提高上下文预算")
    data = {"previous_memory":previous,"dialogue":batch,"response_schema":SummaryResult.model_json_schema()}
    summary_input = json.dumps(data,ensure_ascii=False)
    repair = ""
    for attempt in range(1,4):
        # 共3次：首次生成 + 最多2次格式修正；网络错误由 Provider 自己处理，不在此无限重试。
        if not valid():
            raise asyncio.CancelledError()
        request_text = summary_input+repair
        response = await provider.complete(SUMMARY_PROMPT,request_text)
        if not valid():
            raise asyncio.CancelledError()
        response["summary_input"] = request_text
        try:
            checked = SummaryResult.model_validate(decode_json(response["text"]))
            checked.check_text()
            parsed = checked.model_dump()
        except (ValueError,ValidationError) as exc:
            # 日志保留完整响应和字段错误；修正请求只放受预算限制的错误信息及部分原输出。
            error = str(exc)[:1800]
            if on_attempt:
                on_attempt({"attempt":attempt,"valid":False,"error":error,"raw_text":response["text"],
                            "input":request_text,"usage":response.get("usage",{}),"elapsed_ms":response.get("elapsed_ms")})
            if attempt == 3:
                raise ValueError("剧情摘要连续3次格式校验失败，原始记录和失败响应已保留；请调整模型后继续") from None
            # Repair instructions stay bounded; full failed output remains in the event log.
            allowance = max(200,budget-len(summary_input)-100)
            repair = "\n"+json.dumps({"repair":{"errors":error[:min(600,allowance//3)],"previous_output":response["text"][:max(0,min(2500,allowance//2-300))],
                "instruction":"重新生成整个摘要 JSON；四个字段都必须存在，列表只能包含字符串，最多12项，每项最多500字。"}},ensure_ascii=False)
            continue
        break
    summary = parsed["summary"]
    # 只有通过全部校验后才构造新的累积摘要；未压缩部分和更晚产生的事实仍然保留。
    through = batch[-1]["source_seq"]
    # Cumulative chapter summary retains provenance for its entire ancestry.
    provenance = result["chapters"][-1].get("source_ids",[]) if result["chapters"] else []
    provenance += [x["source_id"] for x in batch]
    result["chapters"] = [{"summary":summary,"from_seq":result["chapters"][0]["from_seq"] if result["chapters"] else batch[0]["source_seq"],"through_seq":through,"source_ids":provenance,"version":len(provenance)}]
    result["recent"] = result["recent"][len(batch):]
    result["summarized_through"] = through
    # Preserve newer facts; older ones are represented in the cumulative summary.
    for key in ("facts","hypotheses","questions"):
        result[key] = [{"text":x,"source_seq":through,"summary_version":len(provenance)} for x in parsed.get(key,[])]+[x for x in result[key] if x.get("source_seq",0)>through]
    return result,response
