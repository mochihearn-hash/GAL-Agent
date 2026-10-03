# 将现有三种协议接入 LangChain 的标准聊天模型接口。
# 请求地址、认证和网络重试仍由 Provider 管理；本模型没有任何窗口操作权限。
from __future__ import annotations

import base64

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langsmith import tracing_context
from pydantic import PrivateAttr

from .client import ProviderError


class GameChatModel(BaseChatModel):
    """异步视觉模型适配器，保留用户现有 URL 和三个接口类型。"""

    model_name: str = "game-vision"
    # 私有属性不会进入模型序列化、运行状态或会话检查点，避免存储网络凭据。
    _provider: object = PrivateAttr()

    def __init__(self, provider):
        super().__init__(model_name=provider.config.get("model", "game-vision") if hasattr(provider, "config") else "game-vision",
                         cache=False, disable_streaming=True)
        self._provider = provider

    @property
    def _llm_type(self):
        return "gal-agent-protocol-adapter"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        # 桌面后台统一使用异步接口，避免同步网络阻塞暂停与心跳。
        raise NotImplementedError("游戏模型请通过异步接口调用")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        # 每轮只接受一条新的观察；历史剧情通过有限预算的 context 提供。
        # 不把此前每轮的 PNG 累积成聊天历史，也不重新上传旧截图。
        systems = [m for m in messages if isinstance(m, SystemMessage)]
        humans = [m for m in messages if isinstance(m, HumanMessage)]
        if len(systems) != 1 or len(humans) != 1 or len(messages) != 2:
            raise ProviderError("format", "Agent 请求必须包含一个系统提示和一次当前观察")
        content = humans[0].content
        image = None
        if isinstance(content, str):
            text = content
        else:
            texts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"]
            images = [b for b in content if isinstance(b, dict) and b.get("type") == "image_url"]
            if len(texts) != 1 or len(images) > 1 or len(texts) + len(images) != len(content):
                raise ProviderError("format", "Agent 观察内容格式无效")
            text = texts[0]
            if images:
                url = images[0]["image_url"]["url"]
                prefix = "data:image/png;base64,"
                if not url.startswith(prefix):
                    raise ProviderError("format", "Agent 只接受当前窗口的 PNG 图像")
                image = base64.b64decode(url[len(prefix):], validate=True)
        response = await self._provider.complete(systems[0].content, text, image)
        message = AIMessage(content=response["text"], response_metadata={
            "usage": response.get("usage", {}), "elapsed_ms": response.get("elapsed_ms"),
            "finish_reason": response.get("finish_reason"),
        })
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def complete(self, system, text, image=None):
        """为剧情摘要复用 LangChain；保留旧摘要逻辑需要的统一返回格式。"""
        content = [{"type": "text", "text": text}]
        if image is not None:
            content.append({"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(image).decode("ascii")}})
        with tracing_context(enabled=False):
            message = await self.ainvoke([SystemMessage(content=system), HumanMessage(content=content)])
        return {"text": message.text, **message.response_metadata}

    @property
    def config(self):
        # 摘要预算继续读取原来的模型配置，不改变长剧情压缩条件。
        return self._provider.config
