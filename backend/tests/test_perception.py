import time
from types import SimpleNamespace

import numpy as np

from app.perception.stability import Stability, change_evidence
from app.schemas import GameProfile


def test_small_local_change_triggers_readback_without_resetting_stability_forever():
    before=np.zeros((192,512),dtype=np.float32)
    after=before.copy();after[165:180,440:455]=150
    evidence=change_evidence(before,after)
    assert evidence['mean']<2 and evidence['changed']
    stability=Stability(GameProfile())
    stability.previous=before
    stability.changed_at=time.monotonic()-2
    assert stability.observe(SimpleNamespace(fingerprint=lambda region:after))
