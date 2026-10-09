"""Synthetic Steam voice: PCM -> Opus -> svc_VoiceData payloads, the inverse of
stvwatch.voice.steamvoice.parse + stvwatch.voice.audio.StreamDecoder, and a
synthetic relay recording with voice made of tones and noise (no speech).

Layout, as measured on live relay traffic:
  u64 SteamID64 | 0x0B u16 24000 | 0x06 u16 bloblen, frames (u16 len | u16 seq | opus) | u32 CRC32
  CRC32 = zlib.crc32(payload[:-4]); Opus 24 kHz mono, 20 ms frames, usually 3
  frames per payload (one payload per ~60 ms), seq +1 per frame, a reset mark
  (len 0xFFFF) in the payload that ends a talk spurt.

    uv run python tests/voicegen.py OUT.tvd
"""

import os
import sys

import numpy as np
from opuslib_next.classes import Encoder

from stvwatch.voice import audio, steamvoice

RATE = 24000
FRAME = 480  # 20 ms at 24 kHz
FRAMES_PER_PAYLOAD = 3
BITRATE = 32000


def build_payload(steamid64: int, frames, reset=False) -> bytes:
    """frames: [(seq, opus_bytes)]."""
    fs = [steamvoice.Frame("opus", seq=seq & 0xFFFF, data=f) for seq, f in frames]
    if reset:
        fs.append(steamvoice.Frame("reset"))
    return steamvoice.build(steamid64, fs, rate=RATE)


def decode(payloads):
    """Payloads of one speaker -> (int16 bytes at RATE, decoder stats)."""
    dec = audio.StreamDecoder(RATE)
    pcm = []
    for p in payloads:
        for f in steamvoice.parse(p).frames:
            pcm += dec.feed(f.seq, f)
    out = np.concatenate(pcm) if pcm else np.zeros(0, np.float32)
    return audio.to_int16(out).tobytes(), dec.stats


class SpurtEncoder:
    """One encoder per speaker; seq runs on until the caller restarts it."""

    def __init__(self, steamid64: int, bitrate=BITRATE):
        self.enc = Encoder(RATE, 1, "voip")
        self.enc.bitrate = bitrate
        self.steamid64 = steamid64
        self.seq = 0

    def spurt(self, pcm16: bytes, t0: float):
        """pcm16: mono int16 at 24 kHz. Returns [(t, payload)], one per 3 frames,
        timed at the moment the last frame of the payload was captured."""
        n = len(pcm16) // 2
        n_frames = (n + FRAME - 1) // FRAME
        pcm16 = pcm16 + b"\x00\x00" * (n_frames * FRAME - n)
        out, pending = [], []
        for k in range(n_frames):
            pending.append(
                (self.seq, self.enc.encode(pcm16[2 * FRAME * k : 2 * FRAME * (k + 1)], FRAME))
            )
            self.seq += 1
            last = k == n_frames - 1
            if len(pending) == FRAMES_PER_PAYLOAD or last:
                out.append(
                    (
                        round(t0 + (k + 1) * FRAME / RATE, 4),
                        build_payload(self.steamid64, pending, reset=last),
                    )
                )
                pending = []
        self.enc.reset_state()
        return out


def payload(steamid64, seq0=0, n=3, reset=False):
    """One payload of `n` Opus frames of a tone, seq from `seq0`."""
    enc = Encoder(RATE, 1, "voip")
    pcm = tone(n * FRAME / RATE)
    frames = [
        (seq0 + k, enc.encode(pcm[2 * FRAME * k : 2 * FRAME * (k + 1)], FRAME)) for k in range(n)
    ]
    return build_payload(steamid64, frames, reset=reset)


def tone(seconds, freq=220.0, amp=0.3, pauses=()):
    """int16 bytes at RATE: a tone with a 3 Hz swell, silent in the (start, end)
    second ranges of `pauses`."""
    t = np.arange(int(seconds * RATE)) / RATE
    x = amp * np.sin(2 * np.pi * freq * t) * (0.7 + 0.3 * np.sin(2 * np.pi * 3 * t))
    for a, b in pauses:
        x[(t >= a) & (t < b)] = 0.0
    return (x * 32767).astype(np.int16).tobytes()


def noise(seconds, amp=0.1, seed=1):
    x = np.random.default_rng(seed).uniform(-amp, amp, int(seconds * RATE))
    return (x * 32767).astype(np.int16).tobytes()


def voice_plan(spurts):
    """spurts: [(t0_s, steamid64, pcm16)] -> [(t_s, steamid64, payload)] in time
    order. Each spurt is a key press: its seq restarts at 0."""
    out = []
    encoders = {}
    for t0, sid, pcm in spurts:
        enc = encoders.setdefault(sid, SpurtEncoder(sid))
        enc.seq = 0
        out += [(t, sid, p) for t, p in enc.spurt(pcm, t0)]
    return sorted(out, key=lambda e: e[0])


def demo(path):
    """Two players on a synthetic relay: alice says a tone with two pauses,
    bob talks over her with noise, then a chat line and alice once more."""
    from helpers import (
        T0,
        account,
        chat_bytes,
        packet,
        reliable_packet,
        table_update,
        write_recording,
    )

    from stvwatch.net import netchan
    from stvwatch.stream.userinfo import STEAMID64_BASE

    alice, bob = STEAMID64_BASE + account(1), STEAMID64_BASE + account(2)
    plan = voice_plan(
        [
            (1.0, alice, tone(4.0, pauses=((1.5, 1.9), (2.8, 3.1)))),
            (2.0, bob, noise(1.5)),
            (9.0, alice, tone(1.0, freq=330.0)),
        ]
    )
    timed = [
        (
            0.0,
            lambda seq: netchan.build_packet(
                seq,
                1,
                0x11223344,
                0,
                unreliable=table_update(
                    ("alice", f"[U:1:{account(1)}]", account(1), 2),
                    ("bob", f"[U:1:{account(2)}]", account(2), 3),
                ),
            ),
        ),
        (7.0, lambda seq: reliable_packet(seq, chat_bytes("bob", "synthetic voice only"))),
    ]
    timed += [(t, lambda seq, p=p: packet(seq, [(1, p)])) for t, _sid, p in plan]
    timed += [(k * 0.05, lambda seq: packet(seq)) for k in range(1, 240)]
    timed.sort(key=lambda e: e[0])
    write_recording(path, [(T0 + int(t * 1e9), make(n + 1)) for n, (t, make) in enumerate(timed)])


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    demo(sys.argv[1])
