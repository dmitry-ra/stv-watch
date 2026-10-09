#!/usr/bin/env python3
"""Wire primitives: LSB-first bit codec, netchannel checksum, protocol constants.

Pure - no I/O, no state, no logging. Everything here is either a bit-level codec
or a constant read off the Source 2013 wire (protocol 24).
"""

import struct
import zlib

PROTOCOL_VERSION = 24
NETMSG_TYPE_BITS = 6  # message id width in the message chain

# Connectionless (OOB) command chars.
A2S_GETCHALLENGE = ord("q")
S2C_CHALLENGE = ord("A")
C2S_CONNECT = ord("k")
S2C_CONNECTION = ord("B")
S2C_CONNREJECT = ord("9")
S2C_MAGICVERSION = 0x5A4F4933  # "3IOZ" LE, in the S2C_CHALLENGE reply

AUTH_HASHEDCDKEY = 2  # anonymous path an empty-tv_password relay accepts

# Datagram markers (int32 at offset 0).
MARK_OOB = -1
MARK_SPLIT = -2
MARK_COMPRESSED = -3

# Netchannel header flags.
FLAG_RELIABLE = 0x01
FLAG_COMPRESSED = 0x02
FLAG_ENCRYPTED = 0x04
FLAG_SPLIT = 0x08
FLAG_CHOKED = 0x10
FLAG_CHALLENGE = 0x20
# Bits 0x40/0x80 appear constantly on the live wire (0xa0 was the most common
# flags byte in reference captures) and are absent from the engine's documented
# set. They add NO conditional header fields: only CHOKED and CHALLENGE carry
# extra header bytes, and checksums validate on that basis.

# Signon ladder (protocol.h).
SIGNON_NONE = 0
SIGNON_CHALLENGE = 1
SIGNON_CONNECTED = 2
SIGNON_NEW = 3
SIGNON_PRESPAWN = 4
SIGNON_SPAWN = 5
SIGNON_FULL = 6
SIGNON_CHANGELEVEL = 7

SIGNON_NAMES = {
    SIGNON_NONE: "NONE",
    SIGNON_CHALLENGE: "CHALLENGE",
    SIGNON_CONNECTED: "CONNECTED",
    SIGNON_NEW: "NEW",
    SIGNON_PRESPAWN: "PRESPAWN",
    SIGNON_SPAWN: "SPAWN",
    SIGNON_FULL: "FULL",
    SIGNON_CHANGELEVEL: "CHANGELEVEL",
}

# Message ids the lifecycle needs. Everything else is opaque by design.
NET_NOP = 0
NET_DISCONNECT = 1
NET_TICK = 3
NET_SETCONVAR = 5
NET_SIGNONSTATE = 6
SVC_SERVERINFO = 8
CLC_CLIENTINFO = 8  # client->server, same id in the clc space

MAX_STREAMS = 2  # 0 = message stream, 1 = file stream
SUBCHANNEL_BITS = 3
SUBCHANNEL_COUNT = 1 << SUBCHANNEL_BITS
MAX_CUSTOM_FILES = 4


class BitReader:
    """LSB-first bit reader (Valve bf_read). Bits fill each byte low-to-high."""

    __slots__ = ("data", "pos")

    def __init__(self, data):
        self.data = data
        self.pos = 0  # absolute bit position

    def bits_left(self):
        return len(self.data) * 8 - self.pos

    def read_ubit(self, n):
        if n > self.bits_left():
            raise EOFError(f"read_ubit({n}) past end")
        val = 0
        for i in range(n):
            byte = self.data[(self.pos + i) >> 3]
            val |= ((byte >> ((self.pos + i) & 7)) & 1) << i
        self.pos += n
        return val

    def read_one_bit(self):
        return self.read_ubit(1)

    def read_byte(self):
        return self.read_ubit(8)

    def read_long(self):
        """Signed 32-bit, matching engine ReadLong."""
        v = self.read_ubit(32)
        return v - (1 << 32) if v & 0x80000000 else v

    def read_ulong(self):
        return self.read_ubit(32)

    def read_bytes(self, n):
        """Bulk read. Per-byte read_ubit ran at 0.7 MB/s -- and this is the call
        that copies every reliable fragment, so a few-hundred-KB signon transfer
        cost about half a second of pure CPU."""
        if n < 0 or n * 8 > self.bits_left():
            raise EOFError(f"read_bytes({n}) past end")
        start = self.pos >> 3
        shift = self.pos & 7
        self.pos += n * 8
        if shift == 0:
            return bytes(self.data[start : start + n])
        chunk = self.data[start : start + n + 1]
        return (int.from_bytes(chunk, "little") >> shift).to_bytes(len(chunk), "little")[:n]

    def read_bytes_remaining(self):
        """Repack every remaining bit into a buffer aligned at bit 0.

        The unreliable stream starts wherever the reliable region ended, which
        is rarely a byte boundary, so it cannot simply be sliced. A trailing
        partial byte is zero-padded; a walk ends on bits_left() anyway."""
        nbits = self.bits_left()
        if nbits <= 0:
            return b""
        nbytes = (nbits + 7) // 8
        tail = self.data[self.pos >> 3 :]
        shift = self.pos & 7
        self.pos += nbits
        if shift == 0:
            return bytes(tail[:nbytes])
        # One big-int shift instead of a per-bit Python loop. This runs on every
        # inbound packet: the loop cost 2.3 ms on a 1280 B packet, which at ~95
        # packets/s is a fifth of a core per relay, spent only on moving bits.
        return (int.from_bytes(tail, "little") >> shift).to_bytes(len(tail), "little")[:nbytes]

    def read_varint32(self):
        result = shift = 0
        while shift < 35:
            b = self.read_ubit(8)
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                return result & 0xFFFFFFFF
            shift += 7
        raise ValueError("varint32 too long")

    def read_string(self, maxlen=4096):
        out = bytearray()
        while len(out) < maxlen:
            c = self.read_ubit(8)
            if c == 0:
                break
            out.append(c)
        return out.decode("utf-8", "replace")


class BitWriter:
    """LSB-first bit writer (Valve bf_write). get_bytes() zero-pads the tail."""

    __slots__ = ("_bits",)

    def __init__(self):
        self._bits = bytearray()  # one entry per bit

    def write_ubit(self, val, n):
        for i in range(n):
            self._bits.append((val >> i) & 1)

    def write_one_bit(self, b):
        self._bits.append(b & 1)

    def write_byte(self, val):
        self.write_ubit(val & 0xFF, 8)

    def write_long(self, val):
        self.write_ubit(val & 0xFFFFFFFF, 32)

    def write_bytes(self, data):
        for b in data:
            self.write_ubit(b, 8)

    def write_string(self, s):
        self.write_bytes(s.encode("utf-8") + b"\x00")

    def write_varint32(self, val):
        val &= 0xFFFFFFFF
        while True:
            b = val & 0x7F
            val >>= 7
            if val:
                self.write_byte(b | 0x80)
            else:
                self.write_byte(b)
                return

    def nbits(self):
        return len(self._bits)

    def get_bytes(self):
        out = bytearray((len(self._bits) + 7) // 8)
        for i, bit in enumerate(self._bits):
            if bit:
                out[i >> 3] |= 1 << (i & 7)
        return bytes(out)


def fold_checksum(data):
    """net_chan.cpp BufferToShortChecksum: CRC32 folded low ^ high."""
    crc = zlib.crc32(data) & 0xFFFFFFFF
    return ((crc & 0xFFFF) ^ ((crc >> 16) & 0xFFFF)) & 0xFFFF


def classify(datagram):
    """Leading int32 -> 'oob' | 'split' | 'compressed' | 'inband'."""
    if len(datagram) < 4:
        return "short"
    mark = struct.unpack_from("<i", datagram, 0)[0]
    if mark == MARK_OOB:
        return "oob"
    if mark == MARK_SPLIT:
        return "split"
    if mark == MARK_COMPRESSED:
        return "compressed"
    return "inband"


def flip_bit(state, bit_index):
    """FLIPBIT - toggle one reliable-subchannel bit in m_nInReliableState."""
    return (state ^ (1 << bit_index)) & 0xFF
