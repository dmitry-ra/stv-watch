"""Silero VAD v4 (ONNX, 16 kHz, 32 ms windows): how much of a clip is speech
(the recognizer's gate) and where its pauses are (where a monologue is cut).

Pauses are read from smoothed decisions: a speech run shorter than MIN_RUN
windows is dropped, then each speech window is held for HANGOVER more. Raw
decisions flicker inside words and would put a cut there.
"""

import numpy as np

VAD_WINDOW = 512  # samples at 16 kHz, what the model takes
WINDOW_MS = VAD_WINDOW * 1000 // 16000
THRESHOLD = 0.5
MIN_RUN = 3
HANGOVER = 4


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


def speech_ms(probs):
    """Milliseconds of windows above THRESHOLD."""
    return int(np.count_nonzero(np.asarray(probs) > THRESHOLD)) * WINDOW_MS


def smooth(raw):
    """raw bool (..., n) -> drop runs < MIN_RUN, then hold HANGOVER windows."""
    raw = np.asarray(raw, bool)
    n = raw.shape[-1]
    if n == 0:
        return raw
    pad = np.zeros(raw.shape[:-1] + (1,), np.int32)
    cs = np.concatenate([pad, np.cumsum(raw, -1, dtype=np.int32)], -1)
    m = MIN_RUN
    full = np.zeros(raw.shape, bool)
    if n >= m:
        full[..., : n - m + 1] = (cs[..., m:] - cs[..., : n - m + 1]) == m
    cf = np.concatenate([pad, np.cumsum(full, -1, dtype=np.int32)], -1)
    idx = np.arange(n)
    lo = np.maximum(idx - m + 1, 0)
    kept = (cf[..., idx + 1] - cf[..., lo]) > 0
    ck = np.concatenate([pad, np.cumsum(kept, -1, dtype=np.int32)], -1)
    lo = np.maximum(idx - HANGOVER, 0)
    return (ck[..., idx + 1] - ck[..., lo]) > 0


def longest_pause(dec, lo, hi):
    """Sample at the middle of the longest non-speech run of `dec` (smoothed
    decisions, one per window) whose middle lies in [lo, hi] samples; runs at
    the ends of the clip are not pauses. Equal runs: the later. None if no
    run qualifies."""
    best = None
    i, n = 0, len(dec)
    while i < n:
        if dec[i]:
            i += 1
            continue
        j = i
        while j < n and not dec[j]:
            j += 1
        mid = (i + j) * VAD_WINDOW // 2
        if i > 0 and j < n and lo <= mid <= hi and (best is None or j - i >= best[1]):
            best = (mid, j - i)
        i = j
    return None if best is None else best[0]
