# 后端由 Electron 启动，通过标准输出报告随机本地端口，不要求用户自己管理常驻服务。
import asyncio
import json
import os
import socket
from pathlib import Path

import uvicorn

from app.api.server import create_app


async def run():
    # 控制 token 必须由桌面传入，裸启动不会获得游戏控制接口。
    token = os.environ.get("GAL_AGENT_TOKEN")
    if not token or len(token)<32:
        raise SystemExit("A desktop-generated access token is required.")
    default = Path(os.environ.get("LOCALAPPDATA",Path.home()))/"GAL Agent"/"data"
    root = Path(os.environ.get("GAL_AGENT_DATA_DIR",str(default)))
    app = create_app(root,token)
    sock = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    sock.bind(("127.0.0.1",0))
    # 系统分配空闲端口；直接把已绑定 socket 交给服务器，避免先探测再绑定的竞态。
    server = uvicorn.Server(uvicorn.Config(app,log_level="warning",access_log=False,host="127.0.0.1"))
    async def watch():
        # 真正启动后报告 ready；桌面请求关闭或 Runtime 退出后结束服务。
        while not server.started:
            await asyncio.sleep(.05)
        print(json.dumps({"ready":True,"port":sock.getsockname()[1]}),flush=True)
        while not app.state.shutdown_requested and not app.state.runtime.closed:
            await asyncio.sleep(.2)
        server.should_exit = True
    monitor = asyncio.create_task(watch())
    try:
        await server.serve(sockets=[sock])
    finally:
        monitor.cancel()
        sock.close()


if __name__ == "__main__":
    asyncio.run(run())
