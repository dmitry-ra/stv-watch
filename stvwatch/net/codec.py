#!/usr/bin/env python3
"""Snappy raw-block decompression (stdlib only) for `-3 SNAP` datagrams.

A `-3` datagram of a relay is `int32(-3) | "SNAP" | <raw Snappy block>`. It is
self-contained: the block inflates to exactly ONE complete inner netchannel
packet, sharing the same sequence and reliable-state space as a plain in-band
packet. No cross-datagram reassembly.

"SNAP" is Snappy, NOT Valve LZSS - the two share the `-3` marker and nothing
else. Raw block format (no stream framing): varint uncompressed length, then
tagged elements. Implemented here rather than pulled in as a dependency.
"""

import struct

SNAP_MAGIC = b"SNAP"
MAX_OUTPUT = 1 << 20  # 1 MiB cap: a netchannel packet is ~1-4 KB


class SnappyError(Exception):
    pass


def _read_varint(data, pos):
    result = shift = 0
    while shift < 35:
        if pos >= len(data):
            raise SnappyError("varint past end")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    raise SnappyError("varint too long")


def uncompress(block):
    """Inflate a raw Snappy block. Raises SnappyError on malformed input."""
    expected, pos = _read_varint(block, 0)
    if expected > MAX_OUTPUT:
        raise SnappyError(f"declared size {expected} over cap {MAX_OUTPUT}")
    out = bytearray()
    n = len(block)
    while pos < n:
        tag = block[pos]
        pos += 1
        kind = tag & 0x03
        if kind == 0:  # literal
            ln = tag >> 2
            if ln >= 60:  # extra length bytes follow
                extra = ln - 59
                if pos + extra > n:
                    raise SnappyError("literal length past end")
                ln = int.from_bytes(block[pos : pos + extra], "little")
                pos += extra
            ln += 1
            if pos + ln > n:
                raise SnappyError("literal past end")
            out += block[pos : pos + ln]
            pos += ln
            continue
        if kind == 1:  # copy, 11-bit offset, 3-bit length
            ln = 4 + ((tag >> 2) & 0x07)
            if pos >= n:
                raise SnappyError("copy1 past end")
            offset = ((tag >> 5) << 8) | block[pos]
            pos += 1
        elif kind == 2:  # copy, 16-bit offset
            ln = (tag >> 2) + 1
            if pos + 2 > n:
                raise SnappyError("copy2 past end")
            offset = struct.unpack_from("<H", block, pos)[0]
            pos += 2
        else:  # copy, 32-bit offset
            ln = (tag >> 2) + 1
            if pos + 4 > n:
                raise SnappyError("copy4 past end")
            offset = struct.unpack_from("<I", block, pos)[0]
            pos += 4
        if offset == 0 or offset > len(out):
            raise SnappyError(f"bad copy offset {offset} at output {len(out)}")
        if len(out) + ln > MAX_OUTPUT:
            raise SnappyError("output over cap")
        start = len(out) - offset
        for i in range(ln):  # byte-wise: copies may overlap
            out.append(out[start + i])
    if len(out) != expected:
        raise SnappyError(f"size mismatch: got {len(out)}, declared {expected}")
    return bytes(out)


LZSS_MAGIC = b"LZSS"
LZSS_LOOKSHIFT = 4


def lzss_uncompress(data):
    """Valve LZSS (tier1/lzss.cpp), used for COMPRESSED reliable transfers.

    Header: b"LZSS" | u32 actual_size. Then command bytes, one bit per element,
    LSB first: 0 = literal byte, 1 = a 2-byte back-reference (12-bit distance,
    4-bit length). A length field of 0 (count == 1) terminates the stream.

    Distinct from the Snappy path above: `-3 SNAP` datagrams are Snappy, while a
    reliable transfer flagged compressed is LZSS. Same word "compressed", two
    unrelated formats.
    """
    if len(data) < 8 or data[:4] != LZSS_MAGIC:
        raise SnappyError("not an LZSS block")
    actual_size = struct.unpack_from("<I", data, 4)[0]
    if actual_size > MAX_OUTPUT:
        raise SnappyError(f"LZSS declared size {actual_size} over cap")
    out = bytearray()
    src = 8
    cmd_byte = 0
    get_cmd = 0
    while len(out) < actual_size and src < len(data):
        if get_cmd == 0:
            cmd_byte = data[src]
            src += 1
            get_cmd = 8
        if cmd_byte & 1:
            if src + 1 >= len(data):
                break
            pos = (data[src] << LZSS_LOOKSHIFT) | (data[src + 1] >> LZSS_LOOKSHIFT)
            count = (data[src + 1] & 0xF) + 1
            src += 2
            if count == 1:
                break
            ref = len(out) - pos - 1
            if ref < 0:
                raise SnappyError(f"LZSS back-reference before start ({ref})")
            for _ in range(count):
                out.append(out[ref])
                ref += 1
        else:
            out.append(data[src])
            src += 1
        cmd_byte >>= 1
        get_cmd -= 1
    return bytes(out)


def inflate_compressed(payload):
    """Inflate a COMPRESSED reliable-transfer payload, dispatching on its magic.

    Measured on live relays: these carry `b"SNAP" | <raw Snappy block>` - the same
    framing as a `-3` datagram, not LZSS. LZSS is still handled because other
    builds use it for the same flag.
    """
    if payload[:4] == SNAP_MAGIC:
        return uncompress(payload[4:])
    if payload[:4] == LZSS_MAGIC:
        return lzss_uncompress(payload)
    raise SnappyError(f"unknown compressed magic {payload[:4]!r}")


def inflate_snap(datagram):
    """`int32(-3) | "SNAP" | block` -> the inner netchannel packet bytes.

    Returns None if the datagram is `-3` but not SNAP-framed (e.g. an LZSS
    variant), so the caller can record it rather than guess.
    """
    if len(datagram) < 8 or datagram[4:8] != SNAP_MAGIC:
        return None
    return uncompress(datagram[8:])
