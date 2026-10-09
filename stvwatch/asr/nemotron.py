"""Nemotron 3.5 ASR Streaming 0.6B (sherpa-onnx, the ONNX int8 export with
560 ms chunks) as a live recognizer: partial text while the player talks.

Each stream levels its input on the go (Level) and decodes a chunk as soon as
the model has one. A partial is sent only when the text changes. finish()
decodes nothing more: the final text of an utterance is Parakeet's.

push_all() feeds many streams and decodes the ready ones in one call, the way
a viewer with several speakers at once uses it.
"""

import os
from collections import deque

import numpy as np

SR = 16000
FILES = {
    "tokens": "tokens.txt",
    "encoder": "encoder.int8.onnx",
    "decoder": "decoder.int8.onnx",
    "joiner": "joiner.int8.onnx",
}


class Level:
    """Streaming peak gain toward `target` full scale, at most +30 dB. The gain
    drops at once and rises at most 6 dB per 160 ms; the peak is that of the
    last 2 s."""

    def __init__(self, target=0.3, max_gain=31.6, span=2 * SR, rise=int(0.16 * SR)):
        self.target, self.max_gain, self.span, self.rise = target, max_gain, span, rise
        self.peaks = deque()  # (samples, peak) of recent pieces
        self.n = 0
        self.gain = 1.0

    def __call__(self, pcm):
        if not len(pcm):
            return pcm
        self.peaks.append((len(pcm), float(np.abs(pcm).max())))
        self.n += len(pcm)
        while self.n - self.peaks[0][0] >= self.span:
            self.n -= self.peaks.popleft()[0]
        peak = max(p for _n, p in self.peaks)
        want = min(self.max_gain, self.target / max(peak, 1e-4))
        up = self.gain * 2.0 ** (len(pcm) / self.rise)
        self.gain = want if want < self.gain else min(want, up)
        return np.clip(pcm * self.gain, -1.0, 1.0).astype(np.float32)


def load(path, threads):
    import sherpa_onnx

    return sherpa_onnx.OnlineRecognizer.from_transducer(
        **{k: os.path.join(path, f) for k, f in FILES.items()},
        num_threads=threads,
        # the viewer, not the model, says where an utterance ends
        enable_endpoint_detection=False,
    )


class NemotronStreaming:
    name = "nemotron"

    def __init__(self, path=None, threads=1, language="", recognizer=None):
        self.path, self.threads, self.language = path, threads, language
        self._rec = recognizer

    @property
    def rec(self):
        if self._rec is None:
            self._rec = load(self.path, self.threads)
        return self._rec

    def load(self):
        return self.rec

    def open(self):
        return _Stream(self)

    def push_all(self, pairs):
        """[(stream, pcm)] -> [(stream, events)] for each stream given, events
        as Stream.push returns them."""
        streams = []
        for st, pcm in pairs:
            st.s.accept_waveform(SR, st.level(np.asarray(pcm, np.float32)))
            if st not in streams:
                streams.append(st)
        while True:
            ready = [st.s for st in streams if self.rec.is_ready(st.s)]
            if not ready:
                break
            self.rec.decode_streams(ready)
        return [(st, st.update()) for st in streams]


class _Stream:
    def __init__(self, eng):
        self.e = eng
        self.s = eng.rec.create_stream()
        if eng.language:
            self.s.set_option("language", eng.language)
        self.level = Level()
        self.text = ""

    def update(self):
        # chunks are joined with a double space
        text = " ".join(self.e.rec.get_result(self.s).split())
        if text == self.text:
            return []
        self.text = text
        return [("partial", text)]

    def push(self, pcm):
        return self.e.push_all([(self, pcm)])[0][1]

    def finish(self):
        return []
