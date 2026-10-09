"""Parakeet TDT 0.6B v3 (onnx-asr) on closed push-to-talk utterances.

The whole utterance is leveled (one gain, its peak to 0.3 full scale, at most
+30 dB) and recognized in one call. Pieces are cut upstream: the viewer ends
a monologue's piece at a pause, at most --max-utt long, so no cut lands here.

One instance per process; concurrent callers are serialized.
"""

import threading

import numpy as np

SR = 16000
MODEL = "nemo-parakeet-tdt-0.6b-v3"


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

    def __init__(self, path=None, threads=2, model=None):
        self.path, self.threads = path, threads
        self._model = model
        self.lock = threading.Lock()

    @property
    def model(self):
        if self._model is None:
            self._model = load(self.path, self.threads)
        return self._model

    def load(self):
        """Load the model now (callers that must not stall later)."""
        return self.model

    def transcribe(self, pcm):
        """16 kHz float32 utterance -> text ("" when the model hears none)."""
        lev = level_peak(np.asarray(pcm, np.float32))
        with self.lock:
            return " ".join(str(self.model.recognize(lev, sample_rate=SR)).split())

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
        text = self.e.transcribe(pcm)
        return [("final", text)] if text else []
