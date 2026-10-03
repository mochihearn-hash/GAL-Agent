# 图像变化只用于判断何时值得重新读取，不负责理解台词，也不直接认定点击成功。
# 稳定等待和局部变化检测采用不同规则：小闪烁不应无限阻止阅读，短句变化也不能漏掉。
import time
import numpy as np


def difference(a,b):
    # 灰度指纹的平均绝对差；无上一帧或尺寸不一致时视为完全变化。
    if a is None or b is None or a.shape != b.shape:
        return float("inf")
    return float(np.mean(np.abs(a-b)))


def change_evidence(a,b,threshold=2.0):
    # mean 反映整体变化，local 反映最明显局部块，pixel_fraction 排除极少数像素噪声。
    # 仅整体平均会把文字变化稀释到大片静止背景中，所以同时检查 6×16 个局部块。
    if a is None or b is None or a.shape != b.shape:
        return {"changed":True,"mean":None,"local":None,"pixel_fraction":None}
    delta = np.abs(a-b)
    mean = float(delta.mean())
    # A short sentence must not disappear into the average of its static background.
    tiles = [part for row in np.array_split(delta,6,axis=0) for part in np.array_split(row,16,axis=1)]
    local = max(float(part.mean()) for part in tiles)
    fraction = float(np.mean(delta > 20))
    return {"changed":mean > threshold or (local > threshold*2 and fraction > .001),
            "mean":round(mean,4),"local":round(local,4),"pixel_fraction":round(fraction,6)}


def changed(a,b,threshold=2.0):
    # 给调度层的便利接口；详细数值仍保存在 evidence 中，方便回查漏判或误判。
    return change_evidence(a,b,threshold)["changed"]


class Stability:
    def __init__(self, profile):
        self.profile = profile
        self.previous = None
        self.changed_at = time.monotonic()

    def observe(self,snapshot):
        # 使用单调时钟累计稳定时间；系统时间调整不会改变等待长度。
        # 图像稳定不等于文字一定显示完成，模型仍需区分 typing 和 dialogue。
        current = snapshot.fingerprint(self.profile.dialogue_region)
        # Tiny blinking indicators may trigger readback, but must not keep the reader waiting forever.
        if difference(current,self.previous) > self.profile.text_change_threshold:
            self.changed_at = time.monotonic()
        self.previous = current
        return (time.monotonic()-self.changed_at)*1000 >= self.profile.settle_ms
