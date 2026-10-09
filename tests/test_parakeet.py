"""The closed-utterance recipe (asr.parakeet.ParakeetUtterance): one gain for
the whole utterance, the VAD as a gate, then one call. Model and VAD are
stand-ins, so this runs without the weights; the test with the real ones is
marked `model`."""

import os

import numpy as np
import pytest

from stvwatch import asr
from stvwatch.asr import parakeet, vad, weights
from stvwatch.asr.vad import VAD_WINDOW, speech_ms

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


def engine(m, min_speech_ms=parakeet.MIN_SPEECH_MS):
    return parakeet.ParakeetUtterance(model=m, vad=Vad(), min_speech_ms=min_speech_ms)


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
    assert engine(m).transcribe(clip)[0] == "w"
    (seen,) = m.calls
    assert np.abs(seen).max() == pytest.approx(peak, rel=0.01)
    ratio = np.abs(seen[: SR // 2]).max() / np.abs(seen[SR // 2 :]).max()
    assert ratio == pytest.approx(0.5, rel=0.01)


def test_a_long_utterance_is_one_call_and_no_text_is_no_final():
    m = Model()
    st = engine(m).open()
    for _ in range(40):
        assert st.push(tone(1.0, 0.3)) == []
    assert st.finish() == [("speech_ms", 40_000), ("final", "w")]
    assert [len(c) for c in m.calls] == [40 * SR]
    assert st.finish() == []
    m.text = " "
    st.push(tone(0.5, 0.3))
    assert st.finish() == [("speech_ms", 480)]


@pytest.mark.parametrize("min_ms", [250, 100, 1000])
def test_under_the_minimum_of_speech_the_model_is_not_called(min_ms):
    """The gate counts VAD windows above 0.5, 32 ms each: the fewest windows
    that reach --min-speech-ms pass, one window less does not; the speech
    found is reported either way."""
    windows = -(-min_ms // vad.WINDOW_MS)
    m = Model()
    e = engine(m, min_ms)
    loud = tone(windows * VAD_WINDOW / SR, 0.3)
    short = (windows - 1) * vad.WINDOW_MS
    assert e.transcribe(loud[: (windows - 1) * VAD_WINDOW]) == ("", short) and m.calls == []
    assert e.transcribe(loud) == ("w", windows * vad.WINDOW_MS) and len(m.calls) == 1
    # a quiet player passes: the VAD hears the leveled clip, not the raw one
    assert e.transcribe(tone(2.0, 0.02))[0] == "w" and len(m.calls) == 2
    assert np.abs(e.vad.seen[-1]).max() == pytest.approx(0.3, rel=0.01)
    hum = np.concatenate([tone(0.03, 0.3), np.zeros(SR, np.float32)])
    assert e.transcribe(hum) == ("", 32) and len(m.calls) == 2
    assert speech_ms(np.array([0.5, 0.51, 0.9], np.float32)) == 64


def test_min_speech_ms_0_is_no_gate():
    m = Model()
    assert engine(m, 0).transcribe(np.zeros(SR, np.float32)) == ("w", 0) and len(m.calls) == 1


def test_pauses_come_from_smoothed_decisions():
    """Speech runs under 3 windows are dropped, speech is held 4 windows: a
    one-window blip inside a pause does not split it, a 3-window gap inside
    speech is no pause. The longest qualifying pause wins, the later on a tie;
    pauses at the ends of the clip never count."""
    raw = np.array(
        [0] * 5
        + [1] * 10
        + [0] * 3
        + [1] * 10
        + [0] * 6
        + [1]
        + [0] * 6
        + [1] * 10
        + [0] * 9
        + [1] * 5
        + [0] * 4,
        bool,
    )
    dec = vad.smooth(raw)
    assert dec.tolist() == (
        [False] * 5
        + [True] * 10
        + [True] * 3
        + [True] * 14
        + [False] * 9
        + [True] * 14
        + [False] * 5
        + [True] * 9
    )
    w = VAD_WINDOW
    first, second = (32 + 41) * w // 2, (55 + 60) * w // 2
    assert vad.longest_pause(dec, 0, len(raw) * w) == first
    assert vad.longest_pause(dec, first + 1, len(raw) * w) == second
    assert vad.longest_pause(dec, 0, first - 1) is None
    tie = np.array([1] * 5 + [0] * 4 + [1] * 5 + [0] * 4 + [1] * 5, bool)
    assert vad.longest_pause(tie, 0, len(tie) * w) == (14 + 18) * w // 2


MODELS = os.environ.get("STV_WATCH_MODELS") or weights.default_dir()


@pytest.mark.model
def test_the_real_model_and_vad_load_from_their_directories():
    for name in asr.WEIGHTS["parakeet"]:
        path = weights.model_dir(weights.PINS[name], MODELS)
        if weights.missing(weights.PINS[name], path):
            pytest.skip(f"no {name} weights in {path} (run stv-watch --asr parakeet once)")
    e = asr.build("parakeet", 2, MODELS, parakeet.MIN_SPEECH_MS)
    assert speech_ms(e.vad.probs(np.zeros(SR, np.float32))) == 0
    assert e.transcribe(np.zeros(SR, np.float32)) == ("", 0)
    assert isinstance(e.transcribe(tone(1.0, 0.3))[0], str)
