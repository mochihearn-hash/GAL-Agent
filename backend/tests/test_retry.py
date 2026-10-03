import asyncio
import json
import time

from PIL import Image, ImageDraw
import pytest

from app.agent.runtime import Runtime
from app.capture.windows import WindowInfo, FocusLost, WindowError
from app.capture.wgc import Snapshot
from app.storage.store import Store


async def until(predicate,timeout=6):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.02)


def fixture(tmp_path,change_on=2):
    class Windows:
        focus=True
        def verify(self,target):return target
        def foreground(self,hwnd):return self.focus
    class Capture:
        stage=0
        same_text=False
        target=WindowInfo(10,20,1.,'fixture','fixture.exe',(0,0,800,600),(0,0,800,600),96,False)
        def snapshot(self):return Snapshot(Image.new('RGB',(800,600),'white' if not self.stage else 'gray'),self.target,time.monotonic(),self.stage+1)
        def close(self):self.target=None
    class Provider:
        calls=0
        feedback_calls=0
        requests=[]
        request_times=[]
        scenes=None
        on_request=None
        hold=asyncio.Event()
        def __init__(self,*args):pass
        async def complete(self,system,text,image=None):
            data=json.loads(text)
            # 模拟模型阅读请求时的截图，不能在请求结束后偷读游戏的新状态。
            story_changed=capture.stage and not capture.same_text
            Provider.requests.append(data)
            Provider.request_times.append(time.monotonic())
            if data.get('purpose')=='verify_feedback':Provider.feedback_calls+=1
            else:Provider.calls+=1
            if Provider.on_request:Provider.on_request(data)
            await Provider.hold.wait()
            result={'observation_id':data['observation_id'],'run_version':data['run_version'],
                'screen_type':'dialogue','dialogue':'新的剧情' if story_changed else '原来的剧情',
                'action':{'type':'request_help' if story_changed else 'advance'}}
            if Provider.scenes:result.update(Provider.scenes[min(Provider.calls-1,len(Provider.scenes)-1)])
            if 'previous_action_result' not in result:
                previous=data.get('previous_action')
                if not previous:result['previous_action_result']=None
                elif result['screen_type'] in {'typing','loading','transition','unknown','menu'}:result['previous_action_result']='pending'
                else:
                    before=previous['scene']
                    same=(before['screen_type'],before.get('speaker',''),before.get('dialogue',''),[x['text'] for x in before.get('options',[])]) == (result['screen_type'],result.get('speaker',''),result.get('dialogue',''),[x['text'] for x in result.get('options',[])])
                    result['previous_action_result']='same' if same else 'changed'
            return {'text':json.dumps(result),'usage':{},'elapsed_ms':1}
    class Executor:
        calls=[]
        on_send=None
        fail_focus_once=False
        def release(self):pass
        def check(self,*args):pass
        def execute(self,snapshot,action,options,profile,valid):
            assert valid() and windows.focus
            if self.fail_focus_once:
                self.fail_focus_once=False;windows.focus=False
                raise FocusLost('focus race before any input')
            self.calls.append(action.type)
            if change_on and len(self.calls)==change_on:capture.stage=1
            if self.on_send:self.on_send()
            return {'sent':True}
    windows,capture,executor=Windows(),Capture(),Executor()
    Provider.hold.set()
    store=Store(tmp_path)
    sid=store.create_session('重试测试')['id']
    config=store.save_config({'model':'fixture','name':'fixture','protocol':'compatible','url':'https://example.com/v1','context_tokens':32000})
    store.update_session(sid,settings={'config_id':config['id'],'profile':{'settle_ms':300,'action_interval_ms':500,'feedback_timeout_ms':500,'max_action_retries':2}})
    runtime=Runtime(store,None,windows,capture,executor,Provider)
    # This fixture has no desktop shell heartbeat; lease expiry is tested separately.
    runtime.last_heartbeat=float('inf')
    runtime.active_id=runtime.bound_id=sid;runtime.state['session_id']=sid
    return store,sid,runtime,windows,capture,executor,Provider


@pytest.mark.asyncio
async def test_no_feedback_reuses_decision_then_continues_new_story(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path)
    await runtime.start(sid)
    await asyncio.wait_for(runtime.task,6)
    assert executor.calls==['advance','advance']
    assert provider.calls==3  # 原决策、下一轮确认原剧情、再下一轮读取新剧情；没有独立核对请求。
    assert provider.feedback_calls==0
    actions=[e['payload'] for e in store.events(sid) if e['kind']=='action']
    assert actions[1]['original_decision_id']==actions[0]['decision_id']
    assert actions[1]['retry_of'] is not None and actions[1]['attempt']==2
    assert all(a['phase']=='observed' for a in actions)
    assert store.get_session(sid)['needs_reconcile']==0
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_short_text_change_is_verified_before_any_retry(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=1)
    def shot():
        image=Image.new('RGB',(800,600),'white')
        ImageDraw.Draw(image).text((140,480),'Original sentence' if not capture.stage else 'Next sentence',fill='black')
        return Snapshot(image,capture.target,time.monotonic(),capture.stage+1)
    capture.snapshot=shot
    await runtime.start(sid);await asyncio.wait_for(runtime.task,6)
    assert executor.calls==['advance'] and provider.calls==2 and provider.feedback_calls==0
    actions=[e['payload'] for e in store.events(sid) if e['kind']=='action']
    assert actions[0]['result']['content_changed'] is True
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_animation_change_does_not_count_as_story_progress(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=1)
    capture.same_text=True
    await runtime.start(sid);await asyncio.wait_for(runtime.task,7)
    assert len(executor.calls)==3 and provider.calls==4 and provider.feedback_calls==0
    assert store.get_session(sid)['needs_reconcile']==1
    assert runtime.state['status']=='paused'
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_no_feedback_has_finite_retry_limit(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=None)
    await runtime.start(sid)
    await asyncio.wait_for(runtime.task,7)
    assert len(executor.calls)==3 and provider.calls==4 and provider.feedback_calls==0
    assert runtime.state['status']=='paused'
    assert '重试 2 次' in runtime.state['message']
    assert store.get_session(sid)['needs_reconcile']==1
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_focus_loss_during_model_rereads_before_any_input(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=1)
    provider.hold.clear()
    await runtime.launch();await runtime.start(sid)
    task=runtime.task
    await until(lambda:provider.calls==1)
    version=runtime.generation;windows.focus=False
    provider.hold.set()
    await until(lambda:runtime.state['status']=='waiting_focus')
    assert not executor.calls and runtime.generation==version
    windows.focus=True
    await asyncio.wait_for(task,5)
    assert len(executor.calls)==1 and provider.calls==3
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_scene_changed_while_unfocused_is_not_clicked_again(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=None)
    executor.on_send=lambda:setattr(windows,'focus',False)
    await runtime.start(sid);task=runtime.task
    await until(lambda:runtime.state['status']=='waiting_focus' and len(executor.calls)==1)
    capture.stage=1;windows.focus=True
    await asyncio.wait_for(task,5)
    assert len(executor.calls)==1 and provider.calls==2
    assert store.get_session(sid)['needs_reconcile']==0
    await runtime.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['pause','guide'])
async def test_manual_stop_cancels_focus_wait_and_never_auto_resumes(tmp_path,operation):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path)
    provider.hold.clear()
    await runtime.start(sid);task=runtime.task
    await until(lambda:provider.calls==1)
    windows.focus=False;provider.hold.set()
    await until(lambda:runtime.state['status']=='waiting_focus')
    if operation=='guide':await runtime.guide(sid,'新引导','persistent')
    else:await runtime.pause()
    windows.focus=True
    await task;await asyncio.sleep(.3)
    assert runtime.state['status']=='paused' and not executor.calls
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_pause_during_retry_prevents_second_input(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=None)
    # 下一轮核对尚未返回时暂停：即便第一次点击失败，也不能提前重复提交输入。
    executor.on_send=provider.hold.clear
    await runtime.start(sid);task=runtime.task
    await until(lambda:provider.calls==2)
    await runtime.pause();await task
    provider.hold.set()
    await asyncio.sleep(.4)
    assert len(executor.calls)==1 and provider.calls==2
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_focus_race_before_send_waits_without_counting_as_retry(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=1)
    executor.fail_focus_once=True
    await runtime.start(sid);task=runtime.task
    await until(lambda:runtime.state['status']=='waiting_focus' and provider.calls==1)
    windows.focus=True
    await asyncio.wait_for(task,5)
    actions=[e['payload'] for e in store.events(sid) if e['kind']=='action']
    assert actions[0]['phase']=='abandoned'
    assert actions[1]['attempt']==1 and actions[1]['retry_of'] is None
    assert len(executor.calls)==1 and provider.calls==3
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_new_scene_before_first_send_discards_old_decision(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path)
    provider.hold.clear()
    await runtime.start(sid);task=runtime.task
    await until(lambda:provider.calls==1)
    windows.focus=False;provider.hold.set()
    await until(lambda:runtime.state['status']=='waiting_focus')
    capture.stage=1;windows.focus=True
    await asyncio.wait_for(task,5)
    assert not executor.calls and provider.calls==2
    assert any(e['kind']=='discarded' for e in store.events(sid))
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_uncertain_os_submission_failure_is_never_retried(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path)
    def fail(*args):
        executor.calls.append('partial')
        raise WindowError('系统未完整接收输入')
    executor.execute=fail
    await runtime.start(sid);await asyncio.wait_for(runtime.task,5)
    assert executor.calls==['partial'] and provider.calls==1
    assert runtime.state['status']=='paused' and store.get_session(sid)['needs_reconcile']==1
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_loading_waits_two_seconds_and_rereads_only_twice(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path)
    provider.scenes=[{'screen_type':'loading','dialogue':'','action':{'type':'wait'}}]
    await runtime.start(sid);await asyncio.wait_for(runtime.task,7)
    assert provider.calls==3 and provider.feedback_calls==0 and not executor.calls
    # 验证真实两秒间隔，而不是只检查提示文案；截图在等待结束后的下一次请求中产生。
    assert all(b-a>=1.95 for a,b in zip(provider.request_times,provider.request_times[1:]))
    assert [r['wait_retry']['used'] for r in provider.requests]==[0,1,2]
    waits=[e for e in store.events(sid) if e['kind']=='wait_retry']
    assert len(waits)==2 and all(e['payload']['delay_seconds']==2 for e in waits)
    assert store.get_session(sid)['memory']['recent']==[]
    assert runtime.state['status']=='paused' and '2 次' in runtime.state['message']
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_pending_typing_then_ready_uses_same_response_for_feedback(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=1)
    runtime.WAIT_SECONDS=.05
    provider.scenes=[{}, {'screen_type':'typing','dialogue':'新','action':{'type':'wait'},'previous_action_result':'pending'},
        {'screen_type':'loading','dialogue':'','action':{'type':'wait'},'previous_action_result':'pending'},
        {'previous_action_result':'changed'}]
    await runtime.start(sid);await asyncio.wait_for(runtime.task,5)
    assert provider.calls==4 and provider.feedback_calls==0 and executor.calls==['advance']
    assert all(r['previous_action']['scene']['dialogue']=='原来的剧情' for r in provider.requests[1:])
    assert len(store.get_session(sid)['memory']['recent'])==2
    actions=[e['payload'] for e in store.events(sid) if e['kind']=='action']
    assert actions[0]['result']['content_changed'] and actions[0]['phase']=='observed'
    assert store.get_session(sid)['needs_reconcile']==0
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_wait_reads_do_not_spend_click_retries_or_duplicate_memory(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=3)
    runtime.WAIT_SECONDS=.05
    provider.scenes=[{}, {'screen_type':'typing','dialogue':'原','action':{'type':'wait'}},
        {'screen_type':'loading','dialogue':'','action':{'type':'wait'}}, {}, {}, {}]
    await runtime.start(sid);await asyncio.wait_for(runtime.task,6)
    assert provider.calls==6 and executor.calls==['advance']*3 and provider.feedback_calls==0
    assert len([e for e in store.events(sid) if e['kind']=='wait_retry'])==2
    actions=[e['payload'] for e in store.events(sid) if e['kind']=='action']
    assert [a['attempt'] for a in actions]==[1,2,3]
    assert all(a.get('original_decision_id')==actions[0]['decision_id'] for a in actions[1:])
    assert len(store.get_session(sid)['memory']['recent'])==2
    assert store.get_session(sid)['needs_reconcile']==0
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_still_typing_after_two_reads_preserves_unconfirmed_input(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=None)
    runtime.WAIT_SECONDS=.05
    provider.scenes=[{}, {'screen_type':'typing','dialogue':'原','action':{'type':'wait'}}]
    await runtime.start(sid);await asyncio.wait_for(runtime.task,5)
    assert provider.calls==4 and executor.calls==['advance']
    action=next(e['payload'] for e in store.events(sid) if e['kind']=='action')
    assert action['phase']=='unconfirmed' and store.get_session(sid)['needs_reconcile']==1
    assert len(store.get_session(sid)['memory']['recent'])==1
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_pause_during_loading_delay_stops_further_reads(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path)
    provider.scenes=[{'screen_type':'loading','dialogue':'','action':{'type':'wait'}}]
    await runtime.start(sid);task=runtime.task
    await until(lambda:any(e['kind']=='wait_retry' for e in store.events(sid)))
    await runtime.pause();await task
    assert provider.calls==1 and not executor.calls
    assert runtime.state['status']=='paused'
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_loading_animation_during_response_cannot_bypass_retry_limit(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path)
    runtime.WAIT_SECONDS=.05
    provider.scenes=[{'screen_type':'loading','dialogue':'','action':{'type':'wait'}}]
    provider.on_request=lambda data:setattr(capture,'stage',1-capture.stage)
    await runtime.start(sid);await asyncio.wait_for(runtime.task,5)
    assert provider.calls==3 and not executor.calls and runtime.state['status']=='paused'
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_model_can_confirm_progress_despite_identical_transcription(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=None)
    provider.scenes=[{}, {'previous_action_result':'changed','action':{'type':'request_help'}}]
    await runtime.start(sid);await asyncio.wait_for(runtime.task,5)
    assert provider.calls==2 and executor.calls==['advance']
    assert store.get_session(sid)['needs_reconcile']==0 and runtime.state['status']=='paused'
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_disabled_retries_still_reads_feedback_without_second_click(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=None)
    session=store.get_session(sid)
    session['settings']['profile']['max_action_retries']=0
    store.update_session(sid,settings=session['settings'])
    await runtime.start(sid);await asyncio.wait_for(runtime.task,5)
    assert provider.calls==2 and executor.calls==['advance']
    assert store.get_session(sid)['needs_reconcile']==1
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_old_three_retry_setting_is_capped_at_two(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=None)
    session=store.get_session(sid)
    session['settings']['profile']['max_action_retries']=3
    store.update_session(sid,settings=session['settings'])
    await runtime.start(sid);await asyncio.wait_for(runtime.task,6)
    assert provider.calls==4 and executor.calls==['advance']*3
    assert all(r['previous_action']['max_retries']==2 for r in provider.requests[1:])
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_same_choice_cannot_switch_unconfirmed_branch(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=2)
    options=[{'text':'相信他','x':.5,'y':.4},{'text':'离开','x':.5,'y':.5}]
    provider.scenes=[{'screen_type':'choice','dialogue':'','options':options,'action':{'type':'choose_option','option_index':1}},
        {'screen_type':'choice','dialogue':'','options':options,'action':{'type':'choose_option','option_index':2},'previous_action_result':'same'},
        {'previous_action_result':'changed'}]
    selections=[]
    execute=executor.execute
    def record(snapshot,action,options,profile,valid):
        selections.append(action.option_index)
        return execute(snapshot,action,options,profile,valid)
    executor.execute=record
    await runtime.start(sid);await asyncio.wait_for(runtime.task,5)
    assert selections==[1] and provider.calls==2 and provider.feedback_calls==0
    assert store.get_session(sid)['needs_reconcile']==1
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_moved_same_choices_use_current_model_coordinates(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=2)
    provider.scenes=[{'screen_type':'choice','dialogue':'','options':[{'text':'继续','x':.5,'y':.4}],
                     'action':{'type':'choose_option','option_index':1}},
                    {'screen_type':'choice','dialogue':'','options':[{'text':'继续','x':.8,'y':.4}],
                     'action':{'type':'choose_option','option_index':1}},
                    {'action':{'type':'request_help'},'previous_action_result':'changed'}]
    points=[];execute=executor.execute
    def record(snapshot,action,options,profile,valid):
        points.append((options[action.option_index-1].x,options[action.option_index-1].y))
        return execute(snapshot,action,options,profile,valid)
    executor.execute=record
    await runtime.start(sid);await asyncio.wait_for(runtime.task,5)
    assert executor.calls==['choose_option']*2 and provider.calls==3
    assert points==[(.5,.4),(.8,.4)] and store.get_session(sid)['needs_reconcile']==0
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_late_combined_feedback_after_pause_cannot_clear_reconcile(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=1)
    started,release=asyncio.Event(),asyncio.Event()
    complete=provider.complete
    async def delayed(self,system,text,image=None):
        result=await complete(self,system,text,image)
        if provider.calls==2:
            started.set()
            try:await release.wait()
            except asyncio.CancelledError:await release.wait()
        return result
    provider.complete=delayed
    await runtime.start(sid);task=runtime.task
    await asyncio.wait_for(started.wait(),4)
    await runtime.pause();release.set();await asyncio.wait_for(task,2)
    assert executor.calls==['advance'] and store.get_session(sid)['needs_reconcile']==1
    assert len(store.get_session(sid)['memory']['recent'])==1
    assert not any(e['kind']=='feedback' for e in store.events(sid))
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_typing_request_help_still_automatically_waits_then_reads(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path)
    runtime.WAIT_SECONDS=.05
    provider.scenes=[{'screen_type':'typing','dialogue':'原','action':{'type':'request_help'}},
        {'action':{'type':'request_help'}}]
    await runtime.start(sid);await asyncio.wait_for(runtime.task,4)
    assert provider.calls==2 and not executor.calls
    assert len([e for e in store.events(sid) if e['kind']=='wait_retry'])==1
    assert store.get_session(sid)['memory']['recent'][0]['dialogue']=='原来的剧情'
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_blinking_indicator_never_discards_model_decision_or_blocks_reading(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=1)
    snapshots=0
    class AnimatedSnapshot(Snapshot):
        def fingerprint(self,*args):
            raise AssertionError('Agent must not use pixel fingerprints to judge story state')
    def shot():
        nonlocal snapshots
        snapshots+=1
        image=Image.new('RGB',(800,600),'white')
        draw=ImageDraw.Draw(image)
        draw.text((140,480),'Original sentence' if not capture.stage else 'Next sentence',fill='black')
        draw.rectangle((710,555,750,590),fill='black' if snapshots%2 else 'white')
        return AnimatedSnapshot(image,capture.target,time.monotonic(),snapshots)
    capture.snapshot=shot
    session=store.get_session(sid)
    session['settings']['profile'].update(settle_ms=10000,feedback_timeout_ms=30000)
    store.update_session(sid,settings=session['settings'])
    await runtime.start(sid);await asyncio.wait_for(runtime.task,3)
    assert executor.calls==['advance'] and provider.calls==2 and provider.feedback_calls==0
    assert snapshots>=4
    assert not any(e['kind']=='discarded' for e in store.events(sid))
    assert [e['payload']['turn'] for e in store.events(sid) if e['kind']=='request']==[1,2]
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_model_same_and_wait_is_respected_without_automatic_click(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=2)
    runtime.WAIT_SECONDS=.05
    provider.scenes=[{}, {'previous_action_result':'same','action':{'type':'wait'}}, {}, {}]
    inputs_when_reading=[]
    provider.on_request=lambda data:inputs_when_reading.append(len(executor.calls))
    await runtime.start(sid);await asyncio.wait_for(runtime.task,5)
    assert provider.calls==4 and executor.calls==['advance','advance']
    assert inputs_when_reading==[0,1,1,2]
    assert provider.request_times[2]-provider.request_times[1]>=.045
    assert len([e for e in store.events(sid) if e['kind']=='wait_retry'])==1
    assert len(store.get_session(sid)['memory']['recent'])==2
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_same_and_wait_hits_read_limit_without_spending_click_retries(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=None)
    runtime.WAIT_SECONDS=.05
    provider.scenes=[{}, {'previous_action_result':'same','action':{'type':'wait'}}]
    await runtime.start(sid);await asyncio.wait_for(runtime.task,4)
    assert provider.calls==4 and executor.calls==['advance']
    assert all(r['previous_action']['attempt']==1 for r in provider.requests[1:])
    assert store.get_session(sid)['needs_reconcile']==1
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_uncertain_and_wait_allows_model_to_read_again(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=1)
    runtime.WAIT_SECONDS=.05
    provider.scenes=[{}, {'screen_type':'unknown','dialogue':'','previous_action_result':'uncertain','action':{'type':'wait'}},
        {'previous_action_result':'changed'}]
    await runtime.start(sid);await asyncio.wait_for(runtime.task,4)
    assert provider.calls==3 and executor.calls==['advance']
    assert store.get_session(sid)['needs_reconcile']==0
    await runtime.close();store.close()


@pytest.mark.asyncio
async def test_transition_is_read_then_waited_without_input(tmp_path):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path)
    runtime.WAIT_SECONDS=.05
    provider.scenes=[{'screen_type':'transition','dialogue':'','action':{'type':'wait'}},
        {'action':{'type':'request_help'}}]
    await runtime.start(sid);await asyncio.wait_for(runtime.task,3)
    assert provider.calls==2 and not executor.calls
    wait=next(e['payload'] for e in store.events(sid) if e['kind']=='wait_retry')
    assert wait['screen_type']=='transition'
    await runtime.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_reply',[{'previous_action_result':None}, {'screen_type':'loading','action':{'type':'advance'}}])
async def test_bad_agent_response_saves_raw_fields_without_retry_or_input(tmp_path,bad_reply):
    store,sid,runtime,windows,capture,executor,provider=fixture(tmp_path,change_on=None)
    provider.scenes=[{},bad_reply]
    await runtime.start(sid);await asyncio.wait_for(runtime.task,3)
    assert provider.calls==2 and executor.calls==['advance']
    error=next(e['payload'] for e in store.events(sid) if e['kind']=='decision_error')
    assert error['raw_text'] and error['errors'] and error['image']
    assert '原始回复已保存' in runtime.state['message']
    assert store.get_session(sid)['needs_reconcile']==1
    await runtime.close();store.close()
