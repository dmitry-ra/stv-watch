"""Steam voice payload of svc_VoiceData: parse and build.

    u64 SteamID64
    chunks until the CRC:
      0x0B u16 sample rate
      0x06 u16 blob length, blob = frames: u16 len | u16 seq | opus bytes;
           len 0xFFFF is a decoder-reset mark with no seq and no bytes
      0x00 u16 silence samples (at the stream rate)
    u32 CRC32 of everything before it

Layout as measured on live relay traffic; the CRC passes on every message cut
from a clean walk.
"""

import struct
import zlib
from dataclasses import dataclass

CHUNK_SILENCE = 0x00
CHUNK_OPUS = 0x06
CHUNK_SAMPLERATE = 0x0B
RESET_MARK = 0xFFFF


@dataclass(frozen=True)
class Frame:
    """One unit of a channel's audio stream, in payload order."""

    kind: str  # "opus" | "reset" | "silence" | "rate"
    seq: int = -1  # opus frame sequence (u16), -1 otherwise
    value: int = 0  # silence samples or sample rate
    data: bytes = b""  # opus bytes


@dataclass(frozen=True)
class Payload:
    steamid64: int
    frames: tuple
    crc_ok: bool
    error: str = ""  # first framing error, "" if the payload parsed whole


def parse(p):
    if len(p) < 12:
        return Payload(0, (), False, "short")
    steamid64 = struct.unpack_from("<Q", p, 0)[0]
    crc_ok = zlib.crc32(p[:-4]) == int.from_bytes(p[-4:], "little")
    out = []
    off, end = 8, len(p) - 4
    err = ""
    while off < end:
        t = p[off]
        off += 1
        if off + 2 > end:
            err = "chunk header past end"
            break
        val = struct.unpack_from("<H", p, off)[0]
        off += 2
        if t == CHUNK_SAMPLERATE:
            out.append(Frame("rate", value=val))
        elif t == CHUNK_SILENCE:
            out.append(Frame("silence", value=val))
        elif t == CHUNK_OPUS:
            blob_end = off + val
            if blob_end > end:
                err = "opus blob past end"
                break
            while off + 2 <= blob_end:
                flen = struct.unpack_from("<H", p, off)[0]
                off += 2
                if flen == RESET_MARK:
                    out.append(Frame("reset"))
                    continue
                if off + 2 + flen > blob_end:
                    err = "opus frame past blob"
                    break
                seq = struct.unpack_from("<H", p, off)[0]
                off += 2
                out.append(Frame("opus", seq=seq, data=bytes(p[off : off + flen])))
                off += flen
            if not err and off < blob_end:
                err = "opus blob trailing byte"
            if err:
                break
        else:
            err = f"unknown chunk 0x{t:02x}"
            break
    return Payload(steamid64, tuple(out), crc_ok, err)


def build(steamid64, frames, rate=24000):
    """Inverse of parse, for synthetic recordings."""
    body = struct.pack("<QBH", steamid64, CHUNK_SAMPLERATE, rate)
    blob = b""
    for f in frames:
        if f.kind == "reset":
            blob += struct.pack("<H", RESET_MARK)
        elif f.kind == "opus":
            blob += struct.pack("<HH", len(f.data), f.seq) + f.data
        elif f.kind == "silence":
            if blob:
                body += struct.pack("<BH", CHUNK_OPUS, len(blob)) + blob
                blob = b""
            body += struct.pack("<BH", CHUNK_SILENCE, f.value)
    if blob:
        body += struct.pack("<BH", CHUNK_OPUS, len(blob)) + blob
    return body + struct.pack("<I", zlib.crc32(body))
