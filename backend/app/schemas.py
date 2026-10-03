# 统一数据契约：配置、窗口绑定及包含上一操作反馈的模型决策在入口处校验。
# 状态与动作关系不能仅依赖提示词，违规数据必须在发送游戏输入前拒绝。
from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    # 拒绝未声明字段，避免拼错配置名被默默接受或模型夹带额外操作参数。
    model_config = ConfigDict(extra="forbid")


class SessionName(StrictModel):
    # 名称只用于展示；重名会话仍由存储层生成不同 ID。
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请填写会话名称")
        return value


class ModelConfig(StrictModel):
    # API Key 仅用于接收配置，接口层把它移入系统凭据库，不直接持久化该模型对象。
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    protocol: Literal["compatible", "anthropic", "gemini"] = "compatible"
    api_key: str | None = Field(default=None, max_length=4096)
    temperature: float | None = Field(default=None, ge=0, le=2)
    context_tokens: int = Field(default=32000, ge=12000, le=1000000)
    timeout: int = Field(default=60, ge=10, le=180)
    json_mode: bool = False

    @field_validator("name", "model", "url")
    @classmethod
    def trim(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("此字段不能为空")
        return value.strip()

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("请填写不包含密钥、查询参数或用户名的 HTTP(S) 接口地址")
        return value.rstrip("/")


class GameProfile(StrictModel):
    # 每个会话独立适配游戏。位置使用客户区归一化坐标，时间参数统一为毫秒。
    advance_key: Literal["SPACE", "ENTER", "CLICK"] = "SPACE"
    # 默认点击客户区下部中央；用户可在预览中改为实际可以推进的点。
    advance_point: tuple[float, float] = (0.5, 0.85)
    move_settle_ms: int = Field(default=50, ge=20, le=1000)
    # 按住时间与操作间隔不同，前者让游戏识别按下，后者等待操作后的反馈。
    input_hold_ms: int = Field(default=100, ge=30, le=1000)
    allowed_keys: list[Literal["SPACE", "ENTER", "UP", "DOWN", "LEFT", "RIGHT", "ESCAPE"]] = Field(default_factory=lambda: ["SPACE", "ENTER", "UP", "DOWN", "LEFT", "RIGHT"])
    dialogue_region: tuple[float, float, float, float] = (0.05, 0.58, 0.9, 0.38)
    settle_ms: int = Field(default=900, ge=300, le=10000)
    action_interval_ms: int = Field(default=1000, ge=500, le=15000)
    # 以下稳定/像素字段兼容旧会话；当前由模型判断状态，运行不再使用它们阻断动作。
    feedback_timeout_ms: int = Field(default=5000, ge=500, le=30000)
    max_action_retries: int = Field(default=2, ge=0, le=3)
    text_change_threshold: float = Field(default=2.0, ge=0.1, le=20)

    @field_validator("advance_point")
    @classmethod
    def valid_point(cls, point):
        if any(not 0 <= coordinate < 1 for coordinate in point):
            raise ValueError("推进点击位置必须在游戏窗口内")
        return point

    @field_validator("dialogue_region")
    @classmethod
    def valid_region(cls, box: tuple[float, float, float, float]):
        x, y, w, h = box
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > 1.00001 or y + h > 1.00001:
            raise ValueError("对话区域必须在窗口范围内")
        return box


class SessionSettings(StrictModel):
    # config_id 指向共享模型连接，profile 独立保存；旧会话缺省字段采用默认值。
    config_id: str | None = None
    profile: GameProfile = Field(default_factory=GameProfile)


class Binding(StrictModel):
    # 包含进程创建时间，防止游戏关闭后句柄或 PID 被其他进程复用。
    hwnd: int = Field(gt=0)
    pid: int = Field(gt=0)
    process_started: float


class GuidanceInput(StrictModel):
    # next 在下一次首次成功发送后消费，persistent 保留到用户取消。
    text: str = Field(min_length=1, max_length=4000)
    scope: Literal["next", "persistent"] = "next"

    @field_validator("text")
    @classmethod
    def clean(cls, value):
        if not value.strip():
            raise ValueError("引导内容不能为空")
        return value.strip()


class CheckpointInput(StrictModel):
    note: str = Field(default="", max_length=1000)


class Action(StrictModel):
    # 动作是受限结构化提议，不是任意工具调用；各动作必须有对应参数。
    type: Literal["wait", "advance", "choose_option", "click", "press_key", "request_help"]
    x: float | None = Field(default=None, ge=0, lt=1)
    y: float | None = Field(default=None, ge=0, lt=1)
    option_index: int | None = Field(default=None, ge=1, le=12)
    key: Literal["SPACE", "ENTER", "UP", "DOWN", "LEFT", "RIGHT", "ESCAPE"] | None = None

    @model_validator(mode="after")
    def valid_action(self):
        if self.type == "click" and (self.x is None or self.y is None):
            raise ValueError("点击动作缺少位置")
        if self.type == "choose_option" and self.option_index is None:
            raise ValueError("选择动作缺少选项编号")
        if self.type == "press_key" and self.key is None:
            raise ValueError("按键动作缺少按键")
        return self


class Choice(StrictModel):
    # 选项按可见顺序从1编号，坐标为当前截图中对应按钮的中心。
    text: str = Field(min_length=1, max_length=1000)
    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)


class Decision(StrictModel):
    # observation_id/run_version 将响应绑定到截图和运行；运行层还要再次检查是否过期。
    observation_id: str
    run_version: int
    screen_type: Literal["typing", "dialogue", "choice", "transition", "loading", "menu", "unknown"]
    # 下一轮决策同时反馈上一操作；首次观察填null，存在待确认操作时必须由模型填写。
    previous_action_result: Literal["changed", "same", "pending", "uncertain"] | None = None
    speaker: str = Field(default="", max_length=120)
    dialogue: str = Field(default="", max_length=8000)
    understanding: str = Field(default="", max_length=1500)
    options: list[Choice] = Field(default_factory=list, max_length=12)
    action: Action
    reason: str = Field(default="", max_length=1500)
    facts: list[str] = Field(default_factory=list, max_length=8)
    hypotheses: list[str] = Field(default_factory=list, max_length=8)
    questions: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("facts", "hypotheses", "questions")
    @classmethod
    def bound_items(cls, items):
        if any(len(item) > 1000 for item in items):
            raise ValueError("记忆条目过长")
        return items

    @model_validator(mode="after")
    def valid_decision(self):
        # 对话统一使用 advance，由配置决定实际输入；不能用 click/press_key 绕过。
        # 未确认界面只允许等待或求助，分支选择必须依据当前可见列表。
        if self.screen_type == "dialogue" and self.action.type not in {"advance", "wait", "request_help"}:
            raise ValueError("对话推进只能使用 advance，由游戏配置决定点击或按键")
        if self.action.type == "advance" and (self.screen_type != "dialogue" or not self.dialogue.strip()):
            raise ValueError("推进前必须完整读取当前对话")
        if self.screen_type in {"typing", "transition", "loading", "unknown", "menu"} and self.action.type not in {"wait", "request_help"}:
            raise ValueError("未确认界面只允许等待或请求帮助")
        if self.action.type == "choose_option":
            if self.screen_type != "choice" or not self.options or self.action.option_index > len(self.options):
                raise ValueError("选择动作与可见选项不匹配")
        if self.screen_type == "choice" and self.action.type not in {"choose_option", "wait", "request_help"}:
            raise ValueError("选项界面只能选择可见选项、等待或请求帮助")
        if self.previous_action_result in {"pending", "uncertain"} and self.action.type not in {"wait", "request_help"}:
            raise ValueError("上一操作未确认时只能等待或请求帮助")
        return self
