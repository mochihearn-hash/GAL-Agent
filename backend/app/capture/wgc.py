# 使用 Windows Graphics Capture 精确捕获一个窗口，再裁剪为游戏客户区。
# 图形回调运行在线程中，Agent 在事件循环中读取；两者只通过受锁保护的最新帧交换数据。
from __future__ import annotations

import hashlib
import io
import threading
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image

from .windows import WindowError, WindowInfo, Windows


class ResizeRequired(WindowError):
    # 帧尺寸与当前窗口边界不匹配时请求重建捕获，不能继续使用旧尺寸坐标。
    def __init__(self,target):
        super().__init__("窗口尺寸已变化，正在重建捕获并等待新画面")
        self.target = target


@dataclass
class Snapshot:
    # 一次快照包含图像与对应的窗口几何；acquired_at 是读取时的单调时间，非游戏原始帧时间。
    image: Image.Image
    window: WindowInfo
    acquired_at: float
    frame_number: int

    def png(self):
        stream = io.BytesIO()
        self.image.save(stream,format="PNG")
        return stream.getvalue()

    def fingerprint(self, region=(0.05,0.58,0.9,0.38)):
        # 从归一化对话区域提取固定512×192灰度矩阵，降低比较成本且保留局部文字变化。
        # 矩阵用浮点类型，避免像素相减时无符号整数下溢。
        x,y,w,h = region
        iw,ih = self.image.size
        crop = self.image.crop((int(x*iw),int(y*ih),int((x+w)*iw),int((y+h)*ih)))
        return np.asarray(crop.convert("L").resize((512,192)),dtype=np.float32)

    @property
    def digest(self):
        return hashlib.sha256(self.image.tobytes()).hexdigest()


class Capture:
    def __init__(self, windows: Windows):
        self.windows = windows
        self.target: WindowInfo | None = None
        self._control = None
        self._capture = None
        # _lock 保护帧数据；_lifecycle 串行化绑定/关闭并允许 _bind 内部再次调用 close。
        self._lock = threading.Lock()
        self._lifecycle = threading.RLock()
        self._generation = 0
        self._buffer = None
        self._frame_number = 0
        self._error = None

    def bind(self,target: WindowInfo):
        with self._lifecycle:
            self._bind(target)

    def rebind_if_current(self,target: WindowInfo):
        # 重建尺寸之前仍须核对目标身份；切换游戏后不能让旧窗口的恢复任务抢回捕获。
        with self._lifecycle:
            current = self.target
            if current is None or (current.hwnd,current.pid,current.process_started)!=(target.hwnd,target.pid,target.process_started):
                raise WindowError("捕获目标已改变，取消尺寸恢复")
            if current.client==target.client and current.frame==target.frame:
                return
            self._bind(target)

    def _bind(self,target: WindowInfo):
        # 先关闭旧捕获。依赖必须支持 HWND 精确绑定，不能退化为按标题匹配或截取全屏。
        self.close()
        from windows_capture import WindowsCapture
        import inspect
        if "window_hwnd" not in inspect.signature(WindowsCapture).parameters:
            raise WindowError("捕获依赖不支持精确窗口绑定，请升级 windows-capture；不会捕获整个桌面")
        self.windows.verify(target)
        self.target = target
        generation = self._generation
        with self._lock:
            self._buffer,self._error,self._frame_number = None,None,0
        # 不捕获系统鼠标光标，避免其移动触发图像变化；因此预览不显示光标不代表没有点击。
        capture = WindowsCapture(window_hwnd=target.hwnd,monitor_index=None,cursor_capture=False,draw_border=True)
        @capture.event
        def on_frame_arrived(frame,control):
            # 捕获版本不同说明已经解绑；停止旧回调，防止晚到帧覆盖新游戏的图像。
            if self._generation != generation or self.target != target:
                control.stop()
                return
            with self._lock:
                self._buffer = frame.frame_buffer.copy()
                self._frame_number += 1
        @capture.event
        def on_closed():
            with self._lock:
                if self._generation == generation:
                    self._error = "游戏窗口已关闭，请重新选择"
        try:
            self._control = capture.start_free_threaded()
            self._capture = capture
        except Exception:
            self.target = None
            raise WindowError("无法启动此窗口的图形捕获，请检查 Windows 版本和窗口状态") from None

    def snapshot(self):
        # 每次重新获取窗口信息，然后拷贝最新缓冲区，避免分析过程中被捕获线程修改。
        if not self.target:
            raise WindowError("请先选择游戏窗口")
        info = self.windows.verify(self.target)
        with self._lock:
            if self._error:
                raise WindowError(self._error)
            if self._buffer is None:
                raise WindowError("正在等待第一张游戏画面")
            raw = self._buffer.copy()
            number = self._frame_number
        height,width = raw.shape[:2]
        fx,fy,fw,fh = info.frame
        cx,cy,cw,ch = info.client
        if abs(width-fw) > 4 or abs(height-fh) > 4:
            raise ResizeRequired(info)
        # 原始帧以窗口边界为基准；客户区在桌面的偏移换算到帧内坐标后再裁剪。
        sx,sy = width/fw,height/fh
        left,top = round((cx-fx)*sx),round((cy-fy)*sy)
        right,bottom = left+round(cw*sx),top+round(ch*sy)
        if left<0 or top<0 or right>width+2 or bottom>height+2:
            raise WindowError("捕获画面与游戏客户区无法准确对齐")
        # 取前三个颜色通道并从BGR换为RGB，丢弃边框、标题栏和可能存在的额外通道。
        rgb = raw[max(0,top):min(height,bottom),max(0,left):min(width,right),:3][:,:,::-1]
        if rgb.size == 0:
            raise WindowError("未取得有效游戏画面")
        image = Image.fromarray(rgb)
        if np.max(rgb) < 5:
            raise WindowError("捕获到黑屏，请切回游戏或调整显示模式")
        return Snapshot(image,info,time.monotonic(),number)

    def close(self):
        with self._lifecycle:
            self._close()

    def _close(self):
        # 先清空目标并递增捕获版本，使回调失效，再停止底层控制器和释放缓冲区。
        self.target = None
        self._generation += 1
        control,self._control = self._control,None
        if control:
            try:
                control.stop()
            except Exception:
                pass
        self._capture = None
        with self._lock:
            self._buffer = None
