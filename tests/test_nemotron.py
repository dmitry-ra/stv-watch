"""The live engine (asr.nemotron): streaming level, partials only on a change
of text, ready streams decoded in one call. sherpa-onnx is a stand-in here;
the test with the real weights is marked `model`."""

import os
import tempfile
import threading
import wave

import numpy as np
import pytest

from stvwatch import asr, cli
from stvwatch.asr import nemotron, weights

SR = nemotron.SR
CHUNK = int(0.56 * SR)


class Recognizer:
    """sherpa-onnx OnlineRecognizer: a chunk of 560 ms makes a stream ready,
    each decode adds a word; results come joined with double spaces."""

    def __init__(self):
        self.batches, self.streams = [], []

    def create_stream(self):
        rec = self

        class Stream:
            def __init__(self):
                self.got, self.used, self.words, self.options = [], 0, [], {}
                rec.streams.append(self)

            def accept_waveform(self, sr, pcm):
                assert sr == SR and pcm.dtype == np.float32
                self.got.append(np.array(pcm))

            def set_option(self, key, value):
                self.options[key] = value

        return Stream()

    def is_ready(self, s):
        return sum(len(p) for p in s.got) - s.used >= CHUNK

    def decode_streams(self, streams):
        self.batches.append(len(streams))
        for s in streams:
            s.used += CHUNK
            s.words.append(f"w{len(s.words)}")

    def get_result(self, s):
        return " " + "  ".join(s.words) + "  "


def engine(language=""):
    return nemotron.NemotronStreaming(language=language, recognizer=Recognizer())


def tone(seconds, amp):
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_partials_come_on_a_change_of_text_and_ready_streams_decode_together():
    e = engine()
    a, b = e.open(), e.open()
    assert a.push(tone(0.5, 0.1)) == []  # under a chunk: nothing decoded
    out = e.push_all([(a, tone(0.1, 0.1)), (b, tone(0.3, 0.1)), (b, tone(0.3, 0.1))])
    assert out == [(a, [("partial", "w0")]), (b, [("partial", "w0")])]
    assert e.rec.batches == [2]
    # two chunks queued in one stream: decoded until it is not ready
    assert a.push(tone(1.2, 0.1)) == [("partial", "w0 w1 w2")] and e.rec.batches == [2, 1, 1]
    assert a.push(tone(0.1, 0.1)) == [] and a.finish() == []


def test_the_language_is_set_per_stream_only_when_given():
    assert engine().open().s.options == {}
    assert engine("ru").open().s.options == {"language": "ru"}


def peaks(level, clip, piece=320):
    return [float(np.abs(level(clip[i : i + piece])).max()) for i in range(0, len(clip), piece)]


def test_the_level_lifts_at_most_30_db_rising_6_db_per_160_ms_and_drops_at_once():
    lv = nemotron.Level()
    quiet = peaks(lv, tone(3.0, 0.001))
    assert quiet[7] == pytest.approx(0.002, rel=0.01)  # 160 ms in: +6 dB
    assert max(quiet) == pytest.approx(0.0316, rel=0.01) == quiet[-1]
    # a loud piece is brought down in the same piece, not clipped
    assert max(peaks(lv, tone(0.02, 0.9))) == pytest.approx(0.3, rel=0.01)
    # the gain stays down while the loud peak is in the last 2 s
    after = peaks(lv, tone(3.5, 0.01))
    assert max(after[:97]) == pytest.approx(0.01 * 0.3 / 0.9, rel=0.01)
    assert after[-1] == pytest.approx(0.3, rel=0.01)
    assert lv(np.zeros(0, np.float32)).size == 0


def test_the_level_works_in_160_ms_slices_whatever_the_size_of_a_push():
    """A push of 3 s after a loud piece: its first 2 s stay down, the gain
    rises only after the loud peak leaves the window, as with short pushes."""
    lv = nemotron.Level()
    lv(tone(0.02, 0.9))
    out = lv(tone(3.0, 0.01))
    pieces = [float(np.abs(out[i : i + 320]).max()) for i in range(0, len(out), 320)]
    # the loud peak leaves the window within one slice of 2 s
    assert max(pieces[: (2 * SR - 2560) // 320]) == pytest.approx(0.01 * 0.3 / 0.9, rel=0.01)
    assert pieces[-1] == pytest.approx(0.3, rel=0.01)


def test_nemotron_is_a_live_engine_only():
    assert asr.LIVE_ENGINES == ("nemotron",) and "nemotron" not in asr.ENGINES
    with pytest.raises(SystemExit):
        cli.parse_args(["--replay", "x.tvd", "--asr", "nemotron"])
    with pytest.raises(ValueError):
        asr.build_live("parakeet", 1, "models")


def test_the_nemotron_pin_is_the_measured_archive():
    p = weights.PINS["nemotron"]
    assert p.source == "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
    assert (p.archive.size, p.archive.sha256[:12]) == (475_271_763, "c6bf5e0df765")
    assert sorted(f.name for f in p.files) == sorted(nemotron.FILES.values())
    assert p.size == 682_215_356
    assert weights.model_dir(p, "/m") == "/m/nemotron-560ms-int8-2026-06-11"


MODELS = os.environ.get("STV_WATCH_MODELS") or weights.default_dir()
# The k2-fsa archive ships no English sample; this one comes with the same
# export on Hugging Face, at a pinned revision.
SAMPLE_SOURCE = (
    "https://huggingface.co/csukuangfj2/"
    "sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-560ms-int8-2026-06-11/resolve/"
    "ab43d895f5985b1bbab8b6eac8607fcdc05343f3"
)
SAMPLE = weights.WeightFile(
    "test_wavs/en.wav", 228908, "eb1eb008904465b74c304aad8342e8c7d3c6e61ffe9f66adcaca9cf0f76a93f4"
)


def sample(*engines):
    """The sample as float32, or a skip when weights or network are missing."""
    for name in engines:
        pin = weights.PINS[name]
        if weights.missing(pin, weights.model_dir(pin, MODELS)):
            pytest.skip(f"no {name} weights in {MODELS}")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "en.wav")
        try:
            weights.fetch(f"{SAMPLE_SOURCE}/{SAMPLE.name}", path, SAMPLE)
        except weights.WeightsError as e:
            pytest.skip(f"no sample: {e}")
        with wave.open(path) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16) / 32768
    return np.concatenate([pcm, np.zeros(SR)]).astype(np.float32)


def stream(engine, clip):
    st = engine.open()
    return [t for i in range(0, len(clip), 320) for _k, t in st.push(clip[i : i + 320])]


@pytest.mark.model
def test_the_real_model_grows_its_text_while_the_speech_goes_on():
    texts = stream(asr.build_live("nemotron", 1, MODELS), sample("nemotron"))
    assert len(texts) >= 3 and len(texts[-1].split()) > len(texts[0].split())
    assert "gold" in texts[-1].lower()


@pytest.mark.model
def test_parakeet_and_nemotron_run_at_once_in_one_process():
    """Each brings its own onnxruntime (the onnxruntime wheel; the library
    inside sherpa-onnx-core): both load, and running together changes
    neither result."""
    clip = sample("nemotron", *asr.WEIGHTS["parakeet"])
    p = asr.build("parakeet", 2, MODELS, 0)
    alone = p.transcribe(clip)
    n = asr.build_live("nemotron", 1, MODELS)
    live_alone = stream(n, clip)
    got = {}
    t = threading.Thread(target=lambda: got.update(live=stream(n, clip)))
    t.start()
    together = [p.transcribe(clip) for _ in range(3)]
    t.join(60)
    assert "gold" in alone[0].lower() and together == [alone] * 3
    assert got["live"] == live_alone
