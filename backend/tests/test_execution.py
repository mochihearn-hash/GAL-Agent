from dataclasses import replace
from types import SimpleNamespace
import ctypes
import asyncio
import time

import pytest

from app.capture.windows import WindowInfo, WindowError
from app.execution.executor import Executor, Input, client_point
from app.schemas import Action, GameProfile


class FakeUser:
    def __init__(self):
        self.calls=[]
        self.SendInput=self.send
    def send(self,count,items,size):
        self.calls.append((count,[(x.type,x.ki.wVk if x.type==1 else x.mi.dwFlags) for x in items],size))
        return count
    def GetSystemMetrics(self,index):
        return {76:-1920,77:-200,78:3840,79:1400}[index]


class FakeWindows:
    def __init__(self):
        self.current=WindowInfo(10,20,1.0,"游戏","game.exe",(-1800,-100,1200,800),(-1808,-130,1216,838),144,False)
        self.focus=True;self.clear=True;self.modifiers=False
        self.user=None
    def verify(self,target):
        if self.current.pid!=target.pid:raise WindowError("进程改变")
        return self.current
    def foreground(self,hwnd):return self.focus
    def unobstructed(self,hwnd,x,y):return self.clear
    def modifiers_down(self):return self.modifiers


def test_coordinate_mapping_supports_negative_monitors_and_physical_dimensions():
    windows=FakeWindows()
    assert client_point(windows.current,.5,.5)==(-1200,300)
    assert client_point(windows.current,0,0)==(-1800,-100)
    with pytest.raises(WindowError):client_point(windows.current,1,.5)
    if __import__('os').name=='nt':assert ctypes.sizeof(Input)==40


@pytest.mark.parametrize("condition",["focus","clear","geometry","version","modifiers","key"])
def test_executor_rejects_invalid_context_before_sending_input(condition):
    windows=FakeWindows();executor=Executor(windows)
    snapshot=SimpleNamespace(window=windows.current)
    action=Action(type="click",x=.5,y=.5)
    valid=True
    if condition=='focus':windows.focus=False
    if condition=='clear':windows.clear=False
    if condition=='geometry':windows.current=replace(windows.current,client=(0,0,1200,800))
    if condition=='version':valid=False
    if condition=='modifiers':windows.modifiers=True
    if condition=='key':action=Action(type="press_key",key="ESCAPE")
    with pytest.raises(WindowError):executor.check(snapshot,action,[],GameProfile(),lambda:valid)


def test_allowed_input_holds_between_down_and_up():
    windows=FakeWindows();executor=Executor(windows);windows.user=FakeUser()
    result=executor.execute(SimpleNamespace(window=windows.current),Action(type="advance"),[],GameProfile(),lambda:True)
    assert result["key"]=="SPACE"
    assert windows.user.calls==[(1,[(1,32)],ctypes.sizeof(Input)),(1,[(1,32)],ctypes.sizeof(Input))]
    assert executor.owned_keys==set()


def test_partial_input_failure_releases_owned_key_without_retrying_down():
    windows=FakeWindows();executor=Executor(windows)
    user=FakeUser();original=user.send;calls=0
    def partial(count,items,size):
        nonlocal calls
        calls+=1
        original(count,items,size)
        return 0 if calls==1 else count
    user.SendInput=partial;windows.user=user
    with pytest.raises(WindowError):executor.execute(SimpleNamespace(window=windows.current),Action(type="advance"),[],GameProfile(),lambda:True)
    assert [call[0] for call in user.calls]==[1,1]
    assert not executor.owned_keys


@pytest.mark.asyncio
async def test_click_stages_have_time_to_settle_and_hold():
    windows=FakeWindows();executor=Executor(windows);user=FakeUser();windows.user=user
    times=[];send=user.SendInput
    def record(*args):times.append(time.monotonic());return send(*args)
    user.SendInput=record
    result=await executor.execute_async(SimpleNamespace(window=windows.current),Action(type="advance"),[],GameProfile(advance_key="CLICK",advance_point=(.3,.7)),lambda:True)
    assert [call[1][0] for call in user.calls]==[(0,0xC001),(0,2),(0,4)]
    assert times[1]-times[0]>=.045 and times[2]-times[1]>=.095
    assert result['key'] is None and result['screen_point']==[-1440,460]


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupt',['cancel','focus','valid'])
async def test_interrupt_during_hold_releases_button_without_another_down(interrupt):
    windows=FakeWindows();executor=Executor(windows);windows.user=FakeUser();valid=True
    task=asyncio.create_task(executor.execute_async(SimpleNamespace(window=windows.current),Action(type="advance"),[],GameProfile(advance_key="CLICK",input_hold_ms=200),lambda:valid))
    while not executor.mouse_owned:await asyncio.sleep(.005)
    if interrupt=='cancel':task.cancel()
    elif interrupt=='focus':windows.focus=False
    else:valid=False
    with pytest.raises((WindowError,asyncio.CancelledError)):await task
    flags=[call[1][0][1] for call in windows.user.calls]
    assert flags.count(2)==1 and flags[-1]==4
    assert not executor.mouse_owned
