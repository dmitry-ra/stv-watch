"""The closed-utterance recipe (asr.parakeet.ParakeetUtterance): one gain for
the whole utterance, the VAD as a gate, then one call. Model and VAD are
stand-ins, so this runs without the weights; the test with the real ones is
marked `model`."""

import os

import numpy as np
import pytest

from stvwatch import asr
from stvwatch.asr import parakeet, weights
from stvwatch.asr.vad import VAD_WINDOW, speech_seconds

SR = parakeet.SR


class Model:
    def __init__(self, text="  w "):
        self.calls, self.text = [], text

    def recognize(self, pcm, sample_rate):
        assert sample_rate == SR and pcm.dtype == np.float32
        self.calls.append(np.array(pcm))
        return self.text


class Vad:
    """Speech wherever the leveled signal is loud; pauses are silence."""

    def __init__(self):
        self.seen = []

    def probs(self, pcm):
        self.seen.append(np.array(pcm))
        n = len(pcm) // VAD_WINDOW
        w = pcm[: n * VAD_WINDOW].reshape(n, VAD_WINDOW)
        return (np.abs(w).max(axis=1) > 0.02).astype(np.float32)


def engine(m):
    return parakeet.ParakeetUtterance(model=m, vad=Vad())


def tone(seconds, amp):
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


@pytest.mark.parametrize(
    "amp, peak",
    [
        (0.01, 0.3),  # a quiet player: lifted, its peak to 0.3
        (0.001, 0.0316),  # at most +30 dB
        (0.9, 0.3),  # a loud one: brought down
    ],
)
def test_one_gain_for_the_whole_utterance(amp, peak):
    m = Model()
    clip = tone(1.0, amp)
    clip[: SR // 2] *= 0.5  # a softer start keeps its share: one gain, not a control
    assert engine(m).transcribe(clip) == "w"
    (seen,) = m.calls
    assert np.abs(seen).max() == pytest.approx(peak, rel=0.01)
    ratio = np.abs(seen[: SR // 2]).max() / np.abs(seen[SR // 2 :]).max()
    assert ratio == pytest.approx(0.5, rel=0.01)


def test_a_long_utterance_is_one_call_and_no_text_is_no_final():
    m = Model()
    st = engine(m).open()
    for _ in range(40):
        assert st.push(tone(1.0, 0.3)) == []
    assert st.finish() == [("final", "w")]
    assert [len(c) for c in m.calls] == [40 * SR]
    assert st.finish() == []
    m.text = " "
    st.push(tone(0.5, 0.3))
    assert st.finish() == []


def test_under_the_minimum_of_speech_the_model_is_not_called():
    """The gate counts VAD windows above 0.5: the fewest windows that reach
    MIN_SPEECH_S pass, one window less does not; noise too quiet for the VAD
    even after leveling never reaches the model either."""
    windows = int(np.ceil(parakeet.MIN_SPEECH_S * SR / VAD_WINDOW))
    m = Model()
    e = engine(m)
    loud = tone(windows * VAD_WINDOW / SR, 0.3)
    assert e.transcribe(loud[: (windows - 1) * VAD_WINDOW]) == "" and m.calls == []
    assert e.transcribe(loud) == "w" and len(m.calls) == 1
    # a quiet player passes: the VAD hears the leveled clip, not the raw one
    assert e.transcribe(tone(1.0, 0.02)) == "w" and len(m.calls) == 2
    assert np.abs(e.vad.seen[-1]).max() == pytest.approx(0.3, rel=0.01)
    hum = np.concatenate([tone(0.1, 0.3), np.zeros(SR, np.float32)])
    assert e.transcribe(hum) == "" and len(m.calls) == 2
    assert speech_seconds(np.array([0.5, 0.51, 0.9], np.float32)) == 2 * VAD_WINDOW / SR


MODELS = os.environ.get("STV_WATCH_MODELS") or weights.default_dir()


@pytest.mark.model
def test_the_real_model_and_vad_load_from_their_directories():
    for name in asr.WEIGHTS["parakeet"]:
        path = weights.model_dir(weights.PINS[name], MODELS)
        if weights.missing(weights.PINS[name], path):
            pytest.skip(f"no {name} weights in {path} (run stv-watch --asr parakeet once)")
    e = asr.build("parakeet", 2, MODELS)
    assert speech_seconds(e.vad.probs(np.zeros(SR, np.float32))) == 0.0
    assert e.transcribe(np.zeros(SR, np.float32)) == ""
    assert isinstance(e.transcribe(tone(1.0, 0.3)), str)
