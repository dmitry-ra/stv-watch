"""Parakeet TDT 0.6B v3 (onnx-asr) on closed push-to-talk utterances.

The whole utterance is leveled (one gain, its peak to 0.3 full scale, at most
+30 dB). Silero VAD then gates it: under `min_speech_ms` of speech there is
no text, Parakeet otherwise invents an interjection on noise. Past the gate
the utterance is recognized in one call. Pieces are cut upstream: the viewer
ends a monologue's piece at a pause, at most --max-utt-ms long, so no cut
lands here.

One instance per process; concurrent callers are serialized.
"""

import threading

import numpy as np

from .vad import Vad, speech_ms

SR = 16000
MODEL = "nemo-parakeet-tdt-0.6b-v3"
MIN_SPEECH_MS = 250


def level_peak(pcm, target=0.3, max_gain=31.6):
    """One gain for a whole closed clip: its peak to `target`, at most +30 dB."""
    peak = float(np.abs(pcm).max()) if len(pcm) else 0.0
    return np.clip(pcm * min(max_gain, target / max(peak, 1e-4)), -1.0, 1.0)


def load(path, threads):
    """The model from the local directory `path`: onnx-asr reads it there and
    never downloads by itself."""
    import onnx_asr
    import onnxruntime

    o = onnxruntime.SessionOptions()
    o.intra_op_num_threads = threads
    o.inter_op_num_threads = 1
    # idle worker threads sleep instead of spinning on a shared host
    o.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return onnx_asr.load_model(MODEL, path, sess_options=o)


class ParakeetUtterance:
    name = "parakeet"

    def __init__(
        self, path=None, threads=2, model=None, vad_path=None, vad=None, min_speech_ms=MIN_SPEECH_MS
    ):
        self.path, self.threads, self.vad_path = path, threads, vad_path
        self.min_speech_ms = min_speech_ms
        self._model, self._vad = model, vad
        self.lock = threading.Lock()

    @property
    def vad(self):
        if self._vad is None:
            self._vad = Vad(self.vad_path)
        return self._vad

    @property
    def model(self):
        if self._model is None:
            self._model = load(self.path, self.threads)
        return self._model

    def load(self):
        """Load VAD and model now (callers that must not stall later)."""
        return self.vad, self.model

    def transcribe(self, pcm):
        """16 kHz float32 utterance -> (text, speech_ms); text "" when the
        gate or the model hears no speech. min_speech_ms 0: no gate."""
        lev = level_peak(np.asarray(pcm, np.float32))
        with self.lock:
            speech = speech_ms(self.vad.probs(lev))
            if speech < self.min_speech_ms:
                return "", speech
            return " ".join(str(self.model.recognize(lev, sample_rate=SR)).split()), speech

    def open(self):
        return _UtteranceStream(self)


class _UtteranceStream:
    def __init__(self, eng):
        self.e, self.parts = eng, []

    def push(self, pcm):
        self.parts.append(np.asarray(pcm, np.float32))
        return []

    def finish(self):
        if not self.parts:
            return []
        pcm, self.parts = np.concatenate(self.parts), []
        text, speech = self.e.transcribe(pcm)
        return [("speech_ms", speech)] + ([("final", text)] if text else [])
