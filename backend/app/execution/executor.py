# 受限输入执行器：把经过校验的单个动作转换成 Windows SendInput 事件。
# SendInput 本身是系统输入流，窗口限制依靠每阶段检查焦点、坐标和任务版本实现。
# “系统接受输入”和“游戏推进成功”是不同结果，后者由运行层重新读取剧情确认。
from __future__ import annotations

import ctypes
import asyncio
import time
from ctypes import wintypes as W

from app.capture.windows import WindowError, FocusLost


class MouseInput(ctypes.Structure):
    # 结构字段和对齐方式必须保持 Win32 ABI；额外信息使用指针宽度，兼容64位进程。
    _fields_ = [("dx",W.LONG),("dy",W.LONG),("mouseData",W.DWORD),("dwFlags",W.DWORD),("time",W.DWORD),("dwExtraInfo",ctypes.c_size_t)]


class KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk",W.WORD),("wScan",W.WORD),("dwFlags",W.DWORD),("time",W.DWORD),("dwExtraInfo",ctypes.c_size_t)]


class HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg",W.DWORD),("wParamL",W.WORD),("wParamH",W.WORD)]


class InputUnion(ctypes.Union):
    _fields_ = [("mi",MouseInput),("ki",KeyboardInput),("hi",HardwareInput)]


class Input(ctypes.Structure):
    # type=0 使用鼠标结构，type=1 使用键盘结构；匿名联合体允许直接访问 mi/ki。
    _anonymous_ = ("data",)
    _fields_ = [("type",W.DWORD),("data",InputUnion)]


KEYS = {"SPACE":0x20,"ENTER":0x0D,"UP":0x26,"DOWN":0x28,"LEFT":0x25,"RIGHT":0x27,"ESCAPE":0x1B}


def client_point(window,x,y):
    # 模型坐标相对游戏客户区，范围 [0,1)；映射后是桌面物理像素，可包含负坐标。
    # 客户区不含标题栏和边框，不使用整张桌面尺寸计算游戏内点击位置。
    left,top,width,height = window.client
    if not 0 <= x < 1 or not 0 <= y < 1:
        raise WindowError("点击位置超出游戏范围")
    return left+min(width-1,int(x*width)),top+min(height-1,int(y*height))


class Executor:
    def __init__(self,windows):
        self.windows = windows
        self.owned_keys: set[int] = set()
        self.mouse_owned = False
        if windows.user:
            windows.user.SendInput.argtypes = [W.UINT,ctypes.POINTER(Input),ctypes.c_int]
            windows.user.SendInput.restype = W.UINT

    def check(self,snapshot,action,options,profile,valid):
        # 此方法仅校验，不发送输入。尺寸、位置或 DPI 改变时，旧截图坐标必须作废。
        # 修饰键和遮挡检查避免组合键误触，以及点击到悬浮窗或其他应用。
        if not valid():
            raise WindowError("动作已因暂停或引导失效")
        current = self.windows.verify(snapshot.window)
        if (current.client,current.frame,current.dpi) != (snapshot.window.client,snapshot.window.frame,snapshot.window.dpi):
            raise WindowError("窗口位置或尺寸已变化，需要重新观察")
        if not self.windows.foreground(current.hwnd):
            raise FocusLost("请切回游戏窗口，Agent 正在等待焦点")
        if self.windows.modifiers_down():
            raise WindowError("检测到修饰键按下，暂不发送输入")
        key,x,y = None,None,None
        if action.type == "advance":
            # advance 不采纳模型额外填写的 key/x/y，始终按用户配置选择点击或按键。
            if profile.advance_key == "CLICK":
                x,y = profile.advance_point
            else:
                key = profile.advance_key
        elif action.type == "press_key":
            key = action.key
        elif action.type == "click":
            x,y = action.x,action.y
        elif action.type == "choose_option":
            if not action.option_index or action.option_index > len(options):
                raise WindowError("选项编号无效")
            option = options[action.option_index-1]
            x,y = option.x,option.y
        else:
            raise WindowError("此动作不需要模拟输入")
        if key and key not in profile.allowed_keys:
            raise WindowError("此游戏配置未允许该按键")
        px,py = client_point(current,x if x is not None else .5,y if y is not None else .75)
        if not self.windows.unobstructed(current.hwnd,px,py):
            raise WindowError("目标位置被其他窗口遮挡，请移开遮挡后继续")
        return current,key,px,py

    def _send(self,items):
        # 返回数目只证明 Windows 接收了事件；不完整发送属于结果不确定，禁止自动补发。
        array = (Input*len(items))(*items)
        count = self.windows.user.SendInput(len(items),array,ctypes.sizeof(Input))
        if count != len(items):
            raise WindowError("系统未完整接收输入，可能存在权限限制；不会自动重试此动作")

    def _steps(self,snapshot,action,options,profile,valid):
        # 生成器统一描述发送步骤，同步和异步入口共用，避免两套输入行为发生偏差。
        # yield 的等待间隔允许游戏处理移动/按下，模拟真实点击的持续过程。
        current,key,x,y = self.check(snapshot,action,options,profile,valid)
        if not valid():
            raise WindowError("动作已失效")
        try:
            if key:
                vk = KEYS[key]
                self.owned_keys.add(vk)
                # 先登记程序拥有的键，即使发送抛错，finally 仍会尝试释放。
                self._send([Input(type=1,ki=KeyboardInput(vk,0,0,0,0))])
                yield "wait",profile.input_hold_ms/1000
                self._check_after_down(snapshot,action,options,profile,valid)
                self._send([Input(type=1,ki=KeyboardInput(vk,0,2,0,0))])
                self.owned_keys.discard(vk)
            else:
                user = self.windows.user
                vx,vy,vw,vh = [user.GetSystemMetrics(i) for i in (76,77,78,79)]
                # 虚拟桌面覆盖全部显示器；绝对移动值换算为 0—65535。
                # 0xC001 = MOVE | ABSOLUTE | VIRTUALDESK，支持副屏位于主屏左侧的情况。
                if vw <= 1 or vh <= 1:
                    raise WindowError("无法获取显示器坐标")
                nx,ny = round((x-vx)*65535/(vw-1)),round((y-vy)*65535/(vh-1))
                self._send([Input(type=0,mi=MouseInput(nx,ny,0,0xC001,0,0))])
                yield "wait",profile.move_settle_ms/1000
                # 移动后再次检查前台和遮挡；鼠标移动期间可能出现菜单或其他窗口。
                self.check(snapshot,action,options,profile,valid)
                self.mouse_owned = True
                self._send([Input(type=0,mi=MouseInput(0,0,0,2,0,0))])
                yield "wait",profile.input_hold_ms/1000
                self._check_after_down(snapshot,action,options,profile,valid)
                self._send([Input(type=0,mi=MouseInput(0,0,0,4,0,0))])
                self.mouse_owned = False
            yield "result",{"key":key,"screen_point":[x,y],"hwnd":current.hwnd,
                            "input_type":"keyboard" if key else "mouse",
                            "hold_ms":profile.input_hold_ms,"move_settle_ms":profile.move_settle_ms if not key else 0,
                            "effective_action":f"按 {key}" if key else f"点击游戏位置 ({x}, {y})",
                            "system_accepted":True}
        finally:
            # 正常返回、异常、协程取消、生成器关闭，都只释放本执行器登记的输入状态。
            if self.owned_keys or self.mouse_owned:
                self.release()

    def _check_after_down(self,snapshot,action,options,profile,valid):
        # 按下后失焦已经可能影响游戏，必须转换成不可自动重放的错误。
        # 与“尚未发送就失焦”的 FocusLost 区分，防止恢复焦点后多点一次。
        try:
            self.check(snapshot,action,options,profile,valid)
        except WindowError as exc:
            # A down event may already have affected the game: never replay it as an unsent action.
            raise WindowError("输入已开始但被中断，已释放按键；请核对进度后继续") from exc

    def execute(self,snapshot,action,options,profile,valid):
        # 同步入口供独立原生检查使用；Agent 主循环使用 execute_async，避免阻塞控制命令。
        steps = self._steps(snapshot,action,options,profile,valid)
        try:
            for kind,value in steps:
                if kind == "wait":
                    time.sleep(value)
                else:
                    return value
        finally:
            steps.close()

    async def execute_async(self,snapshot,action,options,profile,valid):
        # asyncio.sleep 让出事件循环；等待期间用户可以暂停，finally 会关闭步骤并释放输入。
        steps = self._steps(snapshot,action,options,profile,valid)
        try:
            for kind,value in steps:
                if kind == "wait":
                    await asyncio.sleep(value)
                else:
                    return value
        finally:
            steps.close()

    def release(self):
        # 不释放用户所有按键，只处理本程序记录的 owned_keys 和鼠标左键状态。
        # 释放失败不再次发送按下事件；上层保留未确认状态供人工核对。
        if not self.windows.user:
            return
        items = [Input(type=1,ki=KeyboardInput(vk,0,2,0,0)) for vk in self.owned_keys]
        if self.mouse_owned:
            items.append(Input(type=0,mi=MouseInput(0,0,0,4,0,0)))
        if items:
            try:
                self._send(items)
            except Exception:
                pass
        self.owned_keys.clear()
        self.mouse_owned = False
