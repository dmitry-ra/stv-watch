"""Steam voice payloads and the Opus decoder: round trip through our own
encoder, parser and decoder on synthetic signals."""

import struct
import zlib

import numpy as np
from voicegen import FRAME, RATE, SpurtEncoder, build_payload, decode, tone

from stvwatch.voice import audio, steamvoice


def voiced(seconds=1.0):
    t = np.arange(int(seconds * RATE)) / RATE
    f0 = 140 + 30 * np.sin(2 * np.pi * 1.5 * t)
    phase = 2 * np.pi * np.cumsum(f0) / RATE
    x = sum(np.sin(k * phase) / k for k in range(1, 8)) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
    return (x / np.max(np.abs(x)) * 12000).astype(np.int16)


def test_payloads_match_live_layout_and_decode_back():
    pcm = voiced()
    sid = 76561197960265728 + 2 * 4_000_004_242
    enc = SpurtEncoder(sid)
    first = enc.spurt(pcm.tobytes(), 10.0)
    second = enc.spurt(pcm[: FRAME * 4].tobytes(), 20.0)
    payloads = [p for _, p in first + second]
    seqs, resets = [], 0
    for p in payloads:
        assert struct.unpack("<Q", p[:8])[0] == sid
        assert struct.unpack("<I", p[-4:])[0] == zlib.crc32(p[:-4])
        parsed = steamvoice.parse(p)
        assert parsed.crc_ok and not parsed.error
        assert parsed.frames[0] == steamvoice.Frame("rate", value=RATE)
        kinds = [f.kind for f in parsed.frames]
        seqs += [f.seq for f in parsed.frames if f.kind == "opus"]
        resets += kinds.count("reset")
    n_frames = len(pcm) // FRAME + 4
    assert seqs == list(range(n_frames))
    assert resets == 2
    assert [round(t - 10.0, 3) for t, _ in first[:2]] == [0.06, 0.12]

    out, st = decode(payloads)
    assert (st["frames"], st["fail"], st["plc"], st["resets"]) == (n_frames, 0, 0, 2)
    dec = np.frombuffer(out, np.int16).astype(np.float64)
    assert len(dec) == n_frames * FRAME
    ref = pcm.astype(np.float64)[2400:21600]
    lag = max(range(400), key=lambda L: float(np.dot(ref, dec[2400 + L : 21600 + L])))
    assert np.corrcoef(ref, dec[2400 + lag : 21600 + lag])[0, 1] > 0.8


def test_steam_voice_roundtrip_keeps_frames_and_crc():
    frames = (
        steamvoice.Frame("reset"),
        steamvoice.Frame("opus", seq=7, data=b"\x01\x02"),
        steamvoice.Frame("silence", value=480),
        steamvoice.Frame("opus", seq=8, data=b"\x03"),
    )
    p = steamvoice.parse(steamvoice.build(42, frames))
    assert p.crc_ok and not p.error and p.steamid64 == 42
    assert p.frames[1:] == frames
    bad = bytearray(steamvoice.build(42, frames))
    bad[-1] ^= 1
    assert not steamvoice.parse(bytes(bad)).crc_ok
    assert steamvoice.parse(b"\x00" * 11).error == "short"


def test_a_byte_left_over_in_an_opus_blob_is_a_framing_error():
    """Too short for a frame header: with a valid CRC it still means the
    frames were cut wrong."""
    body = struct.pack("<QBH", 42, steamvoice.CHUNK_SAMPLERATE, 24000)
    for blob in (struct.pack("<HH", 2, 7) + b"\x01\x02", struct.pack("<H", 0xFFFF)):
        for tail, error in ((b"", ""), (b"\xaa", "opus blob trailing byte")):
            b = body + struct.pack("<BH", steamvoice.CHUNK_OPUS, len(blob + tail)) + blob + tail
            p = steamvoice.parse(b + struct.pack("<I", zlib.crc32(b)))
            assert (p.crc_ok, p.error) == (True, error)


def test_lost_frames_are_concealed_short_and_silenced_long_keeping_the_timeline():
    """seq gaps of up to 3 frames are concealed by the decoder, longer ones
    filled with silence: the decoded length always equals the seq span."""
    enc = SpurtEncoder(1)
    sent = [f for _t, p in enc.spurt(tone(0.6), 0.0) for f in steamvoice.parse(p).frames]
    opus = [f for f in sent if f.kind == "opus"]
    kept = [f for f in opus if f.seq not in (5, 6) and not 12 <= f.seq < 22]
    out, st = decode([build_payload(1, [(f.seq, f.data) for f in kept])])
    assert (st["frames"], st["plc"], st["gap_silence_frames"]) == (len(kept), 2, 10)
    assert len(out) // 2 == len(opus) * FRAME
    silent = np.frombuffer(out, np.int16)[13 * FRAME : 21 * FRAME]
    assert not silent.any()


def test_limiter_bends_only_the_excess_and_int16_does_not_wrap():
    x = np.array([0.0, 0.5, -0.7, 0.9, -1.2, 40.0], np.float32)
    y = audio.limit(x)
    assert list(y[:3]) == list(x[:3])
    assert 0.7 < y[3] < 0.9 and -1.0 < y[4] < -0.9 and 0.99 < y[5] <= 1.0
    assert audio.to_int16(np.array([1.0, -1.0, 2.0], np.float32)).tolist() == [32767, -32768, 32767]
