"""Speech segment -> PCM: Opus decoder with loss concealment, limiter, int16.

Decoding is float (decode_float), never int16: a loud player drives the Opus
output past full scale, and int16 decode saturates there. The limiter then
bends only the excess, so speech under the knee is untouched and nothing
reaches full scale.
"""

import numpy as np
from opuslib_next.classes import Decoder

FRAME_S = 0.020
STREAM_RATE = 24000


class StreamDecoder:
    """Incremental decoder for one channel: feed (seq, Frame) in arrival order,
    get float32 PCM back. Seq gaps up to plc_max frames are concealed by the
    decoder, longer ones become silence; the seq timeline is preserved."""

    def __init__(self, rate=16000, plc_max=3):
        self.dec = Decoder(rate, 1)
        self.rate = rate
        self.plc_max = plc_max
        self.per_frame = int(rate * FRAME_S)
        self.last = None
        self.stats = {"frames": 0, "plc": 0, "gap_silence_frames": 0, "resets": 0, "fail": 0}

    def feed(self, seq, f):
        st, out = self.stats, []
        if f.kind == "reset":
            self.dec.reset_state()
            st["resets"] += 1
            self.last = None
            return out
        if f.kind == "silence":
            out.append(np.zeros(f.value * self.rate // STREAM_RATE, np.float32))
            return out
        if f.kind != "opus":
            return out
        if self.last is not None:
            gap = ((seq - self.last) & 0xFFFF) - 1
            if 0 < gap <= self.plc_max:
                for _ in range(gap):
                    out.append(
                        np.frombuffer(self.dec.decode_float(b"", self.per_frame), np.float32)
                    )
                    st["plc"] += 1
            elif gap > self.plc_max:
                out.append(np.zeros(gap * self.per_frame, np.float32))
                st["gap_silence_frames"] += gap
        try:
            out.append(np.frombuffer(self.dec.decode_float(f.data, 5760), np.float32))
            st["frames"] += 1
        except Exception:  # noqa: BLE001
            out.append(np.zeros(self.per_frame, np.float32))
            st["fail"] += 1
        self.last = seq
        return out


def limit(pcm, knee=0.7):
    """Soft limiter: identity below `knee`, tanh-bent above, asymptote 1.0."""
    a = np.abs(pcm)
    over = a > knee
    if not over.any():
        return pcm
    y = pcm.copy()
    span = 1.0 - knee
    y[over] = np.sign(pcm[over]) * (knee + span * np.tanh((a[over] - knee) / span))
    return y


def to_int16(pcm):
    return (np.clip(pcm, -1.0, 32767 / 32768) * 32768).astype(np.int16)
