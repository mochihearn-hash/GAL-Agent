import asyncio
import base64
import io
import json
import time

import httpx
from PIL import Image
import pytest

from app.agent.runtime import Runtime
from app.capture.wgc import Snapshot
from app.capture.windows import WindowInfo
from app.providers.client import Provider
from app.storage.store import Store


@pytest.mark.asyncio
async def test_full_agent_loop_with_simulated_frames_and_mock_model(tmp_path):
    stage=0
    colors=[(40,60,70),(80,100,110),(140,100,60)]
    target=WindowInfo(10,20,1.0,'mock game','fixture.exe',(0,0,800,600),(0,0,800,600),96,False)
    class Windows:
        def foreground(self,hwnd):return True
        def verify(self,info):return info
    class Capture:
        def __init__(self):self.target=target
        def snapshot(self):return Snapshot(Image.new('RGB',(800,600),colors[stage]),target,time.monotonic(),stage+1)
        def close(self):self.target=None
    class Executor:
        def __init__(self):self.calls=[]
        def release(self):pass
        def check(self,*args):pass
        def execute(self,snapshot,action,options,profile,valid):
            nonlocal stage
            assert valid()
            self.calls.append(action.type);stage+=1
            return {'fixture_stage':stage}
    def handler(request):
        body=json.loads(request.content)
        input=json.loads(body['messages'][1]['content'][0]['text'])
        png=base64.b64decode(body['messages'][1]['content'][1]['image_url']['url'].split(',')[1])
        current=colors.index(Image.open(io.BytesIO(png)).getpixel((0,0)))
        decision={'observation_id':input['observation_id'],'run_version':input['run_version'],'speaker':'小明','dialogue':['我们明天见。','要继续相信他吗？','约定得到了兑现。'][current],'screen_type':['dialogue','choice','dialogue'][current],'action':[{'type':'advance'},{'type':'choose_option','option_index':1},{'type':'request_help'}][current]}
        decision['previous_action_result']='changed' if input.get('previous_action') else None
        if current==1:decision['options']=[{'text':'继续相信','x':.5,'y':.4},{'text':'离开','x':.5,'y':.5}]
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(decision,ensure_ascii=False)},'finish_reason':'stop'}],'usage':{'total_tokens':12}})
    class Credentials:
        def get(self,ref):return 'integration-test-key'
    def factory(config,key):return Provider(config,key,httpx.MockTransport(handler))
    store=Store(tmp_path);sid=store.create_session('模拟剧情闭环')['id']
    config=store.save_config({'name':'mock','model':'mock-vision','protocol':'compatible','url':'https://example.com/v1','context_tokens':32000,'credential_ref':'ref'})
    store.update_session(sid,settings={'config_id':config['id'],'profile':{'settle_ms':300,'action_interval_ms':500}})
    executor=Executor();runtime=Runtime(store,Credentials(),Windows(),Capture(),executor,factory)
    runtime.active_id=runtime.bound_id=sid;runtime.state['session_id']=sid
    await runtime.start(sid);task=runtime.task
    await asyncio.wait_for(task,8)
    assert runtime.state['status']=='paused'
    assert executor.calls==['advance','choose_option']
    events=store.events(sid)
    actions=[event for event in events if event['kind']=='action']
    assert all(event['payload']['phase']=='observed' for event in actions)
    assert store.get_session(sid)['needs_reconcile']==0
    assert len(store.get_session(sid)['memory']['recent'])==3
    assert len(list((store.images/sid).glob('*.png')))==3
    assert len([event for event in events if event['kind']=='request'])==3
    assert not any(event['kind']=='feedback_request' for event in events)
    assert 'integration-test-key' not in json.dumps(store.export(sid))
    await runtime.close();store.close()
