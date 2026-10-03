# 供 Electron 主进程调用的本地 FastAPI 接口；渲染页面仅通过受限 IPC 间接访问。
# 接口层负责认证、参数和控制命令，长期托管任务由 Runtime 管理。
from __future__ import annotations

import asyncio
import hmac
import io
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image, ImageDraw, ImageFont

from app.agent.runtime import Runtime
from app.capture.wgc import Capture, ResizeRequired
from app.capture.windows import Windows
from app.execution.executor import Executor
from app.providers.client import Provider, ProviderError, decode_json
from app.schemas import Binding, CheckpointInput, GuidanceInput, ModelConfig, SessionName, SessionSettings
from app.storage.credentials import Credentials
from app.storage.store import Store, uid


def create_app(root: Path, token: str, *, store=None, credentials=None, windows=None, capture=None, executor=None, provider_factory=Provider):
    # 可注入模拟存储/窗口/模型，方便用独立测试数据验证，而不必操作商业游戏。
    store = store or Store(root)
    credentials = credentials or Credentials()
    windows = windows or Windows()
    capture = capture or Capture(windows)
    executor = executor or Executor(windows)
    runtime = Runtime(store,credentials,windows,capture,executor,provider_factory)

    @asynccontextmanager
    async def lifespan(app):
        # 启动看门狗；退出先停止 Agent 和捕获，再关闭数据库。
        await runtime.launch()
        yield
        await runtime.close()
        store.close()

    app = FastAPI(title="GAL Agent local backend",docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.store,app.state.runtime = store,runtime
    app.state.shutdown_requested = False

    @app.middleware("http")
    async def authentication(request: Request,call_next):
        # 控制 token 由桌面为本次进程随机生成，和模型 API Key 是两种不同凭据。
        authorization = request.headers.get("authorization","")
        if not hmac.compare_digest(authorization,"Bearer "+token):
            return JSONResponse({"detail":"本地控制连接未授权"},status_code=401)
        # The desktop main process is the only HTTP client; reject browser origins.
        if request.headers.get("origin"):
            return JSONResponse({"detail":"不接受网页直接控制请求"},status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(KeyError)
    async def missing(request,exc):
        return JSONResponse({"detail":str(exc.args[0])},status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request,exc):
        return JSONResponse({"detail":str(exc),"code":getattr(exc,"code","validation")},status_code=400)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request,exc):
        # 参数错误中可能带用户输入，不能直接回显完整 Pydantic 错误和 API Key。
        # Pydantic errors include rejected values: never echo the request/API key.
        return JSONResponse({"detail":"填写内容或动作格式不正确，请检查必填项和数值范围"},status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(request,exc):
        return JSONResponse({"detail":"操作未完成，后台遇到异常；当前会话记录会保留"},status_code=500)

    @app.get("/health")
    async def health():
        return {"ok":True,"version":"0.2.0","agent_framework":"langchain"}

    @app.get("/state")
    async def state():
        return runtime.public_state()

    @app.post("/control/heartbeat")
    async def heartbeat():
        # 桌面定期续约；超时后看门狗停止托管并保存。
        runtime.last_heartbeat = time.monotonic()
        return {"ok":True}

    @app.post("/control/windows")
    async def own_windows(request: Request):
        # 登记桌面 PID/HWND，使选择器排除应用主窗口与悬浮窗。
        payload = await request.json()
        pids,hwnds = payload.get("pids",[]),payload.get("hwnds",[])
        if len(pids)>100 or len(hwnds)>100 or not all(isinstance(x,int) and x>0 for x in pids+hwnds):
            raise ValueError("窗口标识无效")
        windows.excluded_pids.update(pids)
        windows.excluded_hwnds.update(hwnds)
        return {"ok":True}

    @app.post("/shutdown")
    async def shutdown():
        await runtime.close()
        app.state.shutdown_requested = True
        return {"ok":True}

    @app.get("/sessions")
    async def sessions():
        return store.list_sessions()

    @app.post("/sessions")
    async def create_session(body: SessionName):
        return store.create_session(body.name)

    @app.get("/sessions/{sid}")
    async def session(sid: str):
        return store.get_session(sid)

    @app.patch("/sessions/{sid}")
    async def rename(sid: str,body: SessionName):
        result = store.update_session(sid,name=body.name)
        runtime.broadcast({"type":"sessions_changed"})
        return result

    @app.delete("/sessions/{sid}")
    async def delete_session(sid: str):
        # 先停止正在运行的任务和捕获，再删除；迟到响应不能恢复已删会话。
        async with runtime.command_lock:
            if runtime.active_id == sid:
                await runtime.pause("会话已停止")
                capture.close()
                runtime.active_id,runtime.bound_id = None,None
                runtime.state.update(session_id=None,window=None,understanding="",reason="")
                runtime.status("paused","请选择或新建会话")
            store.delete_session(sid)
            runtime.broadcast({"type":"sessions_changed"})
            return {"ok":True}

    @app.post("/sessions/{sid}/select")
    async def select(sid: str):
        async with runtime.command_lock:
            await runtime.select(sid)
            return store.get_session(sid)

    @app.put("/sessions/{sid}/settings")
    async def settings(sid: str,body: SessionSettings):
        # 配置改变先暂停，新推进方式和时间参数在继续后的新任务中生效。
        async with runtime.command_lock:
            if body.config_id:
                store.get_config(body.config_id)
            if runtime.active_id == sid:
                await runtime.pause("配置已更新，请继续运行",checkpoint=False)
            return store.update_session(sid,settings=body.model_dump())

    @app.post("/sessions/{sid}/bind")
    async def bind(sid: str,body: Binding):
        async with runtime.command_lock:
            await runtime.bind(sid,body)
            return runtime.public_state()

    @app.post("/sessions/{sid}/start")
    async def start(sid: str):
        async with runtime.command_lock:
            await runtime.start(sid)
            return runtime.public_state()

    @app.post("/sessions/{sid}/pause")
    async def pause(sid: str):
        # 暂停不等待控制锁，避免慢速绑定阻碍人工接管。
        store.get_session(sid)
        # Do not wait behind slow capture startup or model calls.
        if runtime.active_id == sid:
            await runtime.pause()
        return runtime.public_state()

    @app.post("/sessions/{sid}/guidance")
    async def guidance(sid: str,body: GuidanceInput):
        return await runtime.guide(sid,body.text,body.scope)

    @app.delete("/sessions/{sid}/guidance/{gid}")
    async def cancel_guidance(sid: str,gid: str):
        if runtime.active_id == sid:
            await runtime.pause("引导已更新，请继续")
        session = store.get_session(sid)
        items = [item for item in session["guidance"] if item["id"]!=gid]
        store.update_session(sid,guidance=items)
        runtime.log(sid,"guidance_cancelled",{"guidance_id":gid})
        return {"ok":True}

    @app.get("/sessions/{sid}/events")
    async def events(sid: str,before: int | None=None,limit: int=100):
        return store.events(sid,before,max(1,min(limit,200)))

    @app.get("/sessions/{sid}/checkpoints")
    async def checkpoints(sid: str):
        return store.checkpoints(sid)

    @app.post("/sessions/{sid}/checkpoints")
    async def checkpoint(sid: str,body: CheckpointInput):
        if runtime.bound_id == sid:
            shot = capture.snapshot()
            store.save_image(sid,uid(),shot.png())
        result = store.checkpoint(sid,body.note)
        runtime.log(sid,"checkpoint",{"checkpoint_id":result["id"],"note":body.note})
        return result

    @app.post("/sessions/{sid}/restore/{cid}")
    async def restore(sid: str,cid: str):
        # 只恢复 Agent 检查点，游戏存档由用户载入；解除旧捕获后重新绑定。
        async with runtime.command_lock:
            if runtime.active_id == sid:
                await runtime.pause("正在恢复检查点",checkpoint=False)
                capture.close()
                runtime.bound_id = None
                runtime.state["window"] = None
            result = store.restore(sid,cid)
            if runtime.active_id == sid:
                runtime.status("paused","记忆已恢复，请载入对应游戏存档并重新选择窗口")
            return result

    @app.post("/sessions/{sid}/reconcile")
    async def reconcile(sid: str):
        # 主窗口和悬浮窗共用此接口，必须暂停且绑定当前会话才能确认。
        # 保存当前截图并使旧响应失效，只更新对齐状态，不自动开始托管。
        async with runtime.command_lock:
            if runtime.bound_id != sid or runtime.active_id != sid:
                raise ValueError("请先选择当前游戏窗口")
            if runtime.task and not runtime.task.done():
                raise ValueError("请先暂停再核对进度")
            # Invalidate any delayed response before accepting a user's current progress.
            runtime.generation += 1
            shot = capture.snapshot()
            relative = store.save_image(sid,uid(),shot.png())
            store.update_session(sid,needs_reconcile=0)
            runtime.log(sid,"reconcile",{"message":"用户已确认当前游戏画面与 Agent 进度对齐","image":relative})
            store.checkpoint(sid,"进度对齐后保存")
            runtime.status("paused","进度已对齐，点击继续后会重新观察游戏",execution=None)
            return store.get_session(sid)

    @app.get("/sessions/{sid}/export")
    async def export(sid: str):
        return store.export(sid)

    @app.get("/windows")
    async def window_list():
        return windows.enumerate()

    @app.get("/sessions/{sid}/preview")
    async def preview(sid: str):
        # 返回缩小的客户区图像，不提供整个桌面或其他会话窗口的截图。
        store.get_session(sid)
        if runtime.bound_id != sid:
            raise ValueError("请先选择此会话的游戏窗口")
        try:
            shot = capture.snapshot()
        except ResizeRequired as exc:
            await asyncio.to_thread(capture.rebind_if_current,exc.target)
            raise ValueError("窗口尺寸已变化，正在等待新的画面") from None
        image = shot.image.copy()
        image.thumbnail((1280,800))
        stream = io.BytesIO()
        image.save(stream,format="PNG")
        return Response(stream.getvalue(),media_type="image/png")

    @app.get("/images/{sid}/{filename}")
    async def image(sid: str,filename: str):
        store.get_session(sid)
        return FileResponse(store.image_path(f"{sid}/{filename}"),media_type="image/png")

    @app.get("/configs")
    async def configs():
        # 只展示连接信息及 has_key 标记，系统凭据中的明文密钥不发给前端。
        return [{**item,"has_key":bool(item.get("credential_ref"))} for item in store.configs()]

    def persist_config(body,cid=None):
        # 密钥三态：None 保留旧值，空字符串移除，非空字符串写入/替换。
        # 数据库只持久化引用 ID，重新进入会话不需要再填写或确认已保存的密钥。
        previous = store.get_config(cid) if cid else None
        cid = cid or uid()
        payload = body.model_dump(exclude={"api_key"})
        ref = previous.get("credential_ref") if previous else None
        if body.api_key is not None:
            if body.api_key:
                ref = cid
                credentials.put(ref,body.api_key)
            else:
                if ref:
                    credentials.delete(ref)
                ref = None
        payload["credential_ref"] = ref
        item = store.save_config(payload,cid)
        return {**item,"has_key":bool(ref)}

    @app.post("/configs")
    async def create_config(body: ModelConfig):
        return persist_config(body)

    @app.put("/configs/{cid}")
    async def update_config(cid: str,body: ModelConfig):
        if runtime.active_id:
            session = store.get_session(runtime.active_id)
            if session["settings"].get("config_id") == cid:
                await runtime.pause("模型配置已修改，请继续")
        return persist_config(body,cid)

    @app.delete("/configs/{cid}")
    async def delete_config(cid: str):
        item = store.get_config(cid)
        if runtime.active_id and store.get_session(runtime.active_id)["settings"].get("config_id") == cid:
            await runtime.pause("模型配置已删除")
        if item.get("credential_ref"):
            credentials.delete(item["credential_ref"])
        store.delete_config(cid)
        return {"ok":True}

    @app.post("/configs/{cid}/test")
    async def test_config(cid: str):
        # 先验证文字接口，再生成随机数字图片检查视觉输入；仅连通不能证明模型会看图。
        # 此接口会请求用户配置的真实服务，自动化测试通过 provider_factory 注入模拟服务。
        item = store.get_config(cid)
        secret = credentials.get(item["credential_ref"]) if item.get("credential_ref") else ""
        provider = provider_factory(item,secret)
        await provider.complete("You are testing an API connection.","Reply with OK.",json_output=False)
        code = str(secrets.randbelow(9000)+1000)
        canvas = Image.new("RGB",(400,180),"#ffffff")
        font = ImageFont.truetype("arial.ttf",80) if __import__("os").name == "nt" else ImageFont.load_default(size=64)
        ImageDraw.Draw(canvas).text((75,35),code,font=font,fill="#111111")
        buffer = io.BytesIO()
        canvas.save(buffer,format="PNG")
        result = await provider.complete("Read the supplied image. Return JSON only.","Return {\"digits\":\"the four digits visible in the image\"}.",buffer.getvalue())
        try:
            answer = decode_json(result["text"])
            vision = str(answer.get("digits")) == code
        except ProviderError:
            vision = False
        return {"connection":True,"vision":vision,"message":"连接和图片识别测试通过" if vision else "接口连接成功，但图片识别未通过，请确认模型支持视觉输入"}

    @app.websocket("/events")
    async def stream(websocket: WebSocket):
        # WebSocket 用子协议携带本地 token，并拒绝浏览器 Origin；和 HTTP 认证使用同一凭据。
        # 每个连接独立队列，监听断开并清理订阅；实时控制连接失效时停止托管。
        protocols = [x.strip() for x in websocket.headers.get("sec-websocket-protocol","").split(",")]
        if len(protocols)!=2 or protocols[0]!="gal-agent" or not hmac.compare_digest(protocols[1],token) or websocket.headers.get("origin"):
            await websocket.close(code=1008)
            return
        await websocket.accept(subprotocol="gal-agent")
        queue = asyncio.Queue(maxsize=128)
        runtime.clients.add(queue)
        await websocket.send_json({"type":"status","data":runtime.public_state()})
        async def receive():
            while True:
                await websocket.receive_text()
        receiver = asyncio.create_task(receive())
        try:
            while True:
                getter = asyncio.create_task(queue.get())
                done,_ = await asyncio.wait({getter,receiver},return_when=asyncio.FIRST_COMPLETED)
                if receiver in done:
                    getter.cancel()
                    receiver.result()
                    break
                await websocket.send_json(getter.result())
        except (WebSocketDisconnect,RuntimeError):
            pass
        finally:
            receiver.cancel()
            runtime.clients.discard(queue)
            if not runtime.closed:
                await runtime.pause("实时控制连接已断开，已停止操作")

    return app
