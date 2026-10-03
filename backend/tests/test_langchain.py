# 框架接入回归：通过真正的 LangChain 调用和模拟服务验证，不连接用户的模型账号。
import base64
import asyncio
import json

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from app.agent.engine import GameAgent
from app.providers.client import Provider
from app.providers.langchain_model import GameChatModel
from app.schemas import GameProfile
from test_retry import fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["compatible", "anthropic", "gemini"])
async def test_langchain_model_keeps_protocol_images_keys_and_usage(protocol):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert "private-test-key" not in json.dumps(body)
        if protocol == "compatible":
            assert request.headers["authorization"] == "Bearer private-test-key"
            assert body["messages"][0]["content"] == "只读当前游戏"
            image = body["messages"][1]["content"][1]["image_url"]["url"].split(",")[1]
            result = {"choices": [{"message": {"content": '{"status":"ready"}'}, "finish_reason": "stop"}], "usage": {"total_tokens": 17}}
        elif protocol == "anthropic":
            assert request.headers["x-api-key"] == "private-test-key"
            assert body["system"] == "只读当前游戏"
            image = body["messages"][0]["content"][0]["source"]["data"]
            result = {"content": [{"type": "text", "text": '{"status":"ready"}'}], "usage": {"output_tokens": 17}, "stop_reason": "end_turn"}
        else:
            assert request.headers["x-goog-api-key"] == "private-test-key"
            assert body["systemInstruction"]["parts"][0]["text"] == "只读当前游戏"
            image = body["contents"][0]["parts"][0]["inlineData"]["data"]
            result = {"candidates": [{"content": {"parts": [{"text": '{"status":"ready"}'}]}, "finishReason": "STOP"}], "usageMetadata": {"totalTokenCount": 17}}
        assert base64.b64decode(image) == b"current-window-png"
        return httpx.Response(200, json=result)
    config = {"name": "mock", "model": "mock-vision", "url": "https://example.com/v1", "protocol": protocol}
    model = GameChatModel(Provider(config, "private-test-key", httpx.MockTransport(handler)))
    agent = create_agent(model=model, tools=[], system_prompt="只读当前游戏")
    result = await agent.ainvoke({"messages": [HumanMessage(content=[
        {"type": "text", "text": "当前剧情"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b"current-window-png").decode()}},
    ])]})
    assert len(calls) == 1
    assert result["messages"][-1].text == '{"status":"ready"}'
    assert 17 in result["messages"][-1].response_metadata["usage"].values()
    assert "private-test-key" not in model.model_dump_json()
    assert "private-test-key" not in repr(model)


@pytest.mark.asyncio
async def test_real_agent_nodes_keep_only_current_image_and_hide_turn_label(tmp_path):
    store, sid, runtime, windows, capture, executor, provider = fixture(tmp_path, change_on=1)
    config = store.get_config(store.get_session(sid)["settings"]["config_id"])
    rid = store.create_run(sid, config)
    engine = GameAgent(runtime, sid, runtime.generation, rid, config, GameProfile(action_interval_ms=500), provider())
    nodes = engine.agent.get_graph().nodes
    assert "GameTurnMiddleware.before_model" in nodes
    assert "model" in nodes
    assert "GameTurnMiddleware.after_model" in nodes
    statuses = []
    queue = asyncio.Queue()
    runtime.clients.add(queue)
    first = await engine.run_turn(None, 0, 0)
    second = await engine.run_turn(first["pending"], first["wait_retries"], first["turn"])
    while not queue.empty():
        event = queue.get_nowait()
        if event["type"] == "status":
            statuses.append(event["data"]["message"])
    assert first["turn"] == 1 and second["turn"] == 2
    assert len(first["messages"]) == len(second["messages"]) == 2
    assert first["messages"][0].id != second["messages"][0].id
    assert first["observation"] == second["observation"] == {}
    assert second["outcome"] == "stop" and runtime.state["status"] == "paused"
    assert provider.calls == 2 and executor.calls == ["advance"]
    thinking = [s for s in statuses if s.startswith("模型正在判断")]
    assert thinking == ["模型正在判断画面、上一操作和下一动作"] * 2
    assert all(e["payload"]["engine"] == "langchain" for e in store.events(sid) if e["kind"] == "request")
    await runtime.close()
    store.close()


@pytest.mark.asyncio
async def test_summary_completion_also_uses_langchain_without_image():
    seen = []
    class MockProvider:
        config = {"model": "mock", "context_tokens": 32000}
        async def complete(self, system, text, image=None):
            seen.append((system, text, image))
            return {"text": '{"summary":"已读剧情"}', "usage": {"total_tokens": 9}, "elapsed_ms": 1}
    model = GameChatModel(MockProvider())
    result = await model.complete("剧情记录员", "只总结已读内容")
    assert seen == [("剧情记录员", "只总结已读内容", None)]
    assert result["usage"] == {"total_tokens": 9} and result["text"] == '{"summary":"已读剧情"}'
