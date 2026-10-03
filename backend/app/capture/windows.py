# Win32 窗口身份、坐标、焦点和遮挡检查；这里不负责抢焦点，也不发送鼠标/键盘事件。
# 坐标采用桌面物理像素，Electron 的界面位置另行转换为 DIP，不能直接混用。
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes as W
from dataclasses import dataclass, asdict

import psutil


class WindowError(ValueError):
    pass


class FocusLost(WindowError):
    # 专用于尚未发送输入的临时失焦；按下之后失焦由执行器转换为不可重放的普通错误。
    """No input has been sent: this transient condition may wait for focus."""
    pass


@dataclass(frozen=True)
class WindowInfo:
    # hwnd/pid/process_started 共同标识目标；client/frame 均为左、上、宽、高。
    # client 是游戏内容区，frame 是窗口边界；冻结对象避免旧截图的几何被原地改写。
    hwnd: int
    pid: int
    process_started: float
    title: str
    process_name: str
    client: tuple[int, int, int, int]
    frame: tuple[int, int, int, int]
    dpi: int
    minimized: bool

    def public(self):
        return asdict(self)


class Windows:
    def __init__(self):
        # 排除自身进程和桌面层注册的窗口，避免把 Agent 主窗口/悬浮窗当成游戏。
        self.excluded_pids = {os.getpid()}
        self.excluded_hwnds: set[int] = set()
        if os.name != "nt":
            self.user = None
            return
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.dwm = ctypes.WinDLL("dwmapi")
        self.user.SetProcessDpiAwarenessContext.argtypes = [W.HANDLE]
        self.user.SetProcessDpiAwarenessContext.restype = W.BOOL
        self.user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        # 显式声明 ctypes 参数和返回类型，尤其句柄不能按默认32位整数处理。
        signatures = {
            "IsWindow": ([W.HWND], W.BOOL), "IsWindowVisible": ([W.HWND], W.BOOL),
            "IsIconic": ([W.HWND], W.BOOL), "GetForegroundWindow": ([],W.HWND),
            "GetWindowThreadProcessId": ([W.HWND,ctypes.POINTER(W.DWORD)],W.DWORD),
            "GetWindowTextLengthW": ([W.HWND],ctypes.c_int),
            "GetWindowTextW": ([W.HWND,W.LPWSTR,ctypes.c_int],ctypes.c_int),
            "GetClientRect": ([W.HWND,ctypes.POINTER(W.RECT)],W.BOOL),
            "GetWindowRect": ([W.HWND,ctypes.POINTER(W.RECT)],W.BOOL),
            "ClientToScreen": ([W.HWND,ctypes.POINTER(W.POINT)],W.BOOL),
            "WindowFromPoint": ([W.POINT],W.HWND),
            "GetAncestor": ([W.HWND,W.UINT],W.HWND),
            "GetDpiForWindow": ([W.HWND],W.UINT),
            "GetAsyncKeyState": ([ctypes.c_int],ctypes.c_short),
            "GetSystemMetrics": ([ctypes.c_int],ctypes.c_int),
        }
        for name,(args,result) in signatures.items():
            func = getattr(self.user,name)
            func.argtypes,func.restype = args,result
        self.dwm.DwmGetWindowAttribute.argtypes = [W.HWND,W.DWORD,ctypes.c_void_p,W.DWORD]
        self.dwm.DwmGetWindowAttribute.restype = ctypes.c_long

    def info(self, hwnd: int) -> WindowInfo:
        # PID 可能被复用，因此同时记录进程创建时间；标题只供界面展示，不用于确认身份。
        if not self.user:
            raise WindowError("窗口捕获仅支持 Windows")
        if not self.user.IsWindow(hwnd) or hwnd in self.excluded_hwnds:
            raise WindowError("目标窗口已关闭或不可选择")
        pid = W.DWORD()
        self.user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        if pid.value in self.excluded_pids:
            raise WindowError("不能选择 Agent 自己的窗口")
        try:
            process = psutil.Process(pid.value)
            started,name = process.create_time(),process.name()
        except (psutil.Error,OSError):
            raise WindowError("无法核验目标窗口所属进程") from None
        length = self.user.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length+1)
        self.user.GetWindowTextW(hwnd,title,length+1)
        client,origin,frame = W.RECT(),W.POINT(0,0),W.RECT()
        if not self.user.GetClientRect(hwnd,ctypes.byref(client)) or not self.user.ClientToScreen(hwnd,ctypes.byref(origin)):
            raise WindowError("无法获取游戏窗口位置")
        if self.dwm.DwmGetWindowAttribute(hwnd,9,ctypes.byref(frame),ctypes.sizeof(frame)) != 0:
            if not self.user.GetWindowRect(hwnd,ctypes.byref(frame)):
                raise WindowError("无法获取窗口边界")
        return WindowInfo(hwnd,pid.value,started,title.value,name,
                          (origin.x,origin.y,client.right-client.left,client.bottom-client.top),
                          (frame.left,frame.top,frame.right-frame.left,frame.bottom-frame.top),
                          self.user.GetDpiForWindow(hwnd) or 96,bool(self.user.IsIconic(hwnd)))

    def enumerate(self):
        # 只列可见、未被系统隐藏且尺寸足够的窗口；不可核验的目标不会加入列表。
        if not self.user:
            return []
        result = []
        callback_type = ctypes.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
        @callback_type
        def callback(hwnd,_):
            if not self.user.IsWindowVisible(hwnd) or not self.user.GetWindowTextLengthW(hwnd):
                return True
            cloak = W.DWORD()
            if self.dwm.DwmGetWindowAttribute(hwnd,14,ctypes.byref(cloak),ctypes.sizeof(cloak)) == 0 and cloak.value:
                return True
            try:
                info = self.info(int(hwnd))
                if info.client[2] > 100 and info.client[3] > 80:
                    result.append(info.public())
            except WindowError:
                pass
            return True
        self.user.EnumWindows.argtypes = [callback_type,W.LPARAM]
        self.user.EnumWindows.restype = W.BOOL
        self.user.EnumWindows(callback,0)
        return sorted(result,key=lambda x:x["title"].casefold())

    def verify(self, target: WindowInfo):
        # 从系统重新获取信息；目标身份改变、最小化或隐藏时拒绝操作。
        current = self.info(target.hwnd)
        if current.pid != target.pid or current.process_started != target.process_started:
            raise WindowError("目标进程已改变，请重新选择窗口")
        if current.minimized or not self.user.IsWindowVisible(current.hwnd):
            raise WindowError("游戏窗口已最小化或隐藏")
        if min(current.client[2:]) <= 0:
            raise WindowError("游戏窗口没有有效画面")
        return current

    def foreground(self, hwnd):
        return bool(self.user and self.user.GetForegroundWindow() == hwnd)

    def unobstructed(self, hwnd, x, y):
        # 查询点击位置实际命中的窗口，并上溯到顶层；游戏子控件允许，其他窗口遮挡拒绝。
        hit = self.user.WindowFromPoint(W.POINT(x,y))
        return bool(hit and self.user.GetAncestor(hit,2) == hwnd)

    def modifiers_down(self):
        # Shift/Ctrl/Alt/Win 若正被用户按住，暂停发送，避免普通按键变成系统快捷键。
        return any(self.user.GetAsyncKeyState(key) & 0x8000 for key in (0x10,0x11,0x12,0x5B,0x5C))
