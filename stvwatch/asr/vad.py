"""Silero VAD v4 (ONNX, 16 kHz, 32 ms windows): how much of a clip is speech.

The recognizer uses it as a gate only: it never cuts a clip.
"""

import numpy as np

VAD_WINDOW = 512  # samples at 16 kHz, what the model takes


class Vad:
    """Silero VAD (ONNX, 16 kHz, 32 ms windows)."""

    def __init__(self, path):
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])

    def probs(self, pcm16k):
        h = np.zeros((2, 1, 64), np.float32)
        c = np.zeros((2, 1, 64), np.float32)
        n = len(pcm16k) // VAD_WINDOW
        out = np.zeros(n, np.float32)
        for i in range(n):
            x = pcm16k[i * VAD_WINDOW : (i + 1) * VAD_WINDOW][None, :].astype(np.float32)
            p, h, c = self.sess.run(None, {"x": x, "h": h, "c": c})
            out[i] = p[0, 0]
        return out


def speech_seconds(probs, thr=0.5):
    return float(np.count_nonzero(probs > thr)) * VAD_WINDOW / 16000
