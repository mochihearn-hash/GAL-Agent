import asyncio
import json
import time
from types import SimpleNamespace

from PIL import Image
import pytest

from app.agent.runtime import Runtime
from app.capture.wgc import Snapshot
from app.capture.windows import WindowInfo
from app.storage.store import Store


class Credentials:
    def get(self,ref):return "secret"


class Windows:
    def foreground(self,hwnd):return True
    def verify(self,target):return target


class Capture:
    def __init__(self):
        self.target=WindowInfo(10,20,1.0,"fixture","game.exe",(0,0,800,600),(0,0,800,600),96,False)
    def snapshot(self):return Snapshot(Image.new("RGB",(800,600),"white"),self.target,time.monotonic(),1)
    def close(self):self.target=None


class Executor:
    def __init__(self):self.inputs=[]
    def release(self):pass
    def check(self,*args):pass
    def execute(self,*args):self.inputs.append(args);return {"sent":True}


async def setup_runtime(tmp_path):
    store=Store(tmp_path);sid=store.create_session("旅程")["id"]
    config=store.save_config({"name":"test","model":"vision","protocol":"compatible","url":"https://example.com/v1","timeout":30,"context_tokens":32000,"credential_ref":"ref"})
    store.update_session(sid,settings={"config_id":config["id"],"profile":{"settle_ms":300}})
    started,release=asyncio.Event(),asyncio.Event()
    class Delayed:
        def __init__(self,*args):pass
        async def complete(self,system,text,image=None):
            request=json.loads(text);started.set()
            try:await release.wait()
            except asyncio.CancelledError:await release.wait()
            return {"text":json.dumps({"observation_id":request["observation_id"],"run_version":request["run_version"],"screen_type":"dialogue","dialogue":"旧响应的剧情","action":{"type":"advance"}}),"usage":{},"elapsed_ms":1}
    executor=Executor()
    runtime=Runtime(store,Credentials(),Windows(),Capture(),executor,Delayed)
    runtime.active_id=sid;runtime.bound_id=sid;runtime.state["session_id"]=sid
    await runtime.start(sid)
    await asyncio.wait_for(started.wait(),3)
    return store,sid,runtime,executor,release


@pytest.mark.asyncio
@pytest.mark.parametrize("operation",["pause","guide","delete","switch"])
async def test_late_model_response_cannot_execute_or_leak_memory(tmp_path,operation):
    store,sid,runtime,executor,release=await setup_runtime(tmp_path)
    old_task=runtime.task
    if operation=='guide':await runtime.guide(sid,"新的引导","next")
    elif operation=='delete':await runtime.pause();store.delete_session(sid)
    elif operation=='switch':await runtime.select(store.create_session("另一个游戏")["id"])
    else:await runtime.pause()
    release.set();await asyncio.wait_for(old_task,2)
    assert executor.inputs==[]
    if operation!='delete':assert store.get_session(sid)["memory"]["recent"]==[]
    if operation=='switch':assert store.get_session(runtime.active_id)["memory"]["recent"]==[]
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_control_lease_expiry_stops_input_and_saves(tmp_path):
    store,sid,runtime,executor,release=await setup_runtime(tmp_path)
    task=runtime.task
    await runtime.launch();runtime.last_heartbeat=time.monotonic()-20
    await asyncio.sleep(.55)
    assert runtime.state["status"]=="paused"
    release.set();await task
    assert not executor.inputs
    assert store.checkpoints(sid)
    await runtime.close();store.close()
