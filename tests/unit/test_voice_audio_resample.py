"""Unit (feature 060): the scipy-guarded resample path.

Only a mic that refuses 16 kHz reaches ``_resample_to_16k``, so a scipy-less install can run a long
time before hitting it. This asserts that when scipy is missing the path raises the actionable
:class:`VoiceUnavailable` (like every other missing-extra path), not a raw ``ImportError`` mid-turn.
"""

from __future__ import annotations

import builtins

import numpy as np
import pytest

from obi.voice.audio import SAMPLE_RATE, _resample_to_16k
from obi.voice.errors import VoiceUnavailable


def test_resample_downsamples_to_16k() -> None:
    pytest.importorskip("scipy.signal")  # skip when the voice extra isn't installed
    one_second_at_48k = np.zeros(48_000, dtype=np.float32)
    out = _resample_to_16k(one_second_at_48k, 48_000)
    assert out.dtype == np.float32
    # 48k -> 16k is a 1/3 decimation; ~one second stays ~one second at the new rate.
    assert abs(len(out) - SAMPLE_RATE) < SAMPLE_RATE * 0.05


def test_missing_scipy_raises_voice_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "scipy" or name.startswith("scipy."):
            raise ModuleNotFoundError("No module named 'scipy'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(VoiceUnavailable, match="scipy"):
        _resample_to_16k(np.zeros(48_000, dtype=np.float32), 48_000)
