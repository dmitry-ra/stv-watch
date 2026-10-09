"""The closed-utterance recipe (asr.parakeet.ParakeetUtterance): one gain for
the whole utterance, then one call. The model is a stand-in, so this runs
without the weights; the test with the real model is marked `model`."""

import os

import numpy as np
import pytest

from stvwatch.asr import parakeet, weights

SR = parakeet.SR


class Model:
    def __init__(self, text="  w "):
        self.calls, self.text = [], text

    def recognize(self, pcm, sample_rate):
        assert sample_rate == SR and pcm.dtype == np.float32
        self.calls.append(np.array(pcm))
        return self.text


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
    assert parakeet.ParakeetUtterance(model=m).transcribe(clip) == "w"
    (seen,) = m.calls
    assert np.abs(seen).max() == pytest.approx(peak, rel=0.01)
    ratio = np.abs(seen[: SR // 2]).max() / np.abs(seen[SR // 2 :]).max()
    assert ratio == pytest.approx(0.5, rel=0.01)


def test_a_long_utterance_is_one_call_and_no_text_is_no_final():
    m = Model()
    st = parakeet.ParakeetUtterance(model=m).open()
    for _ in range(40):
        assert st.push(tone(1.0, 0.3)) == []
    assert st.finish() == [("final", "w")]
    assert [len(c) for c in m.calls] == [40 * SR]
    assert st.finish() == []
    m.text = " "
    st.push(tone(0.5, 0.3))
    assert st.finish() == []


MODELS = os.environ.get("STV_WATCH_MODELS") or weights.default_dir()


@pytest.mark.model
def test_the_real_model_loads_from_its_directory_and_hears_nothing_in_silence():
    pin = weights.PINS["parakeet"]
    path = weights.model_dir(pin, MODELS)
    if weights.missing(pin, path):
        pytest.skip(f"no Parakeet weights in {path} (run stv-watch --asr parakeet once)")
    e = parakeet.ParakeetUtterance(path, threads=2)
    e.load()
    assert e.transcribe(np.zeros(SR, np.float32)) == ""
    assert isinstance(e.transcribe(tone(1.0, 0.3)), str)
