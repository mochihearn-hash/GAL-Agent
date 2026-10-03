import asyncio
import pytest

from app.agent.runtime import Runtime
from app.storage.store import Store


@pytest.mark.asyncio
async def test_backend_closes_when_parent_disappears_even_while_paused(tmp_path,monkeypatch):
    class Capture:
        target=None
        stopped=False
        def close(self):self.stopped=True
    class Executor:
        def release(self):pass
    store=Store(tmp_path)
    capture=Capture()
    runtime=Runtime(store,None,None,capture,Executor())
    runtime.parent_pid=12345
    monkeypatch.setattr('app.agent.runtime.psutil.pid_exists',lambda pid:False)
    await runtime.launch()
    await asyncio.sleep(.5)
    assert runtime.closed
    assert capture.stopped
    store.close()
