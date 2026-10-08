#!/usr/bin/env python3
"""Netchannel layer: ONE inbound path, plus outbound packet construction.

Header (protocol 24, little-endian, byte-aligned):

    i32 sequence | i32 sequence_ack | u8 flags | u16 checksum | u8 reliable_state
    | [u8 choked   if flags & CHOKED]
    | [i32 challenge if flags & CHALLENGE]

Checksum covers everything AFTER the checksum field, i.e. from the reliable_state
byte to the end, folded CRC32 (low ^ high).

Two rules here are load-bearing:

1. Validate the checksum BEFORE touching any state. A corrupt packet carrying a
   high sequence number otherwise poisons `in_seq` permanently and every later
   legitimate packet is dropped as stale - an unrecoverable stall.
2. Latch the 3-bit subchannel index BEFORE parsing the reliable blocks, and ack
   from the latch even if block parsing throws. The index sits at a fixed offset
   right after the header, so it is always recoverable; losing it to a parse
   exception pins the relstate and the relay resends one fragment forever.
"""

import struct

from . import codec, wire

HEADER_MIN = 11  # seq(4) + ack(4) + flags(1) + checksum(2)


class Header:
    __slots__ = (
        "sequence",
        "sequence_ack",
        "flags",
        "checksum",
        "reliable_state",
        "choked",
        "challenge",
        "body_offset",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def reliable(self):
        return bool(self.flags & wire.FLAG_RELIABLE)

    def __repr__(self):
        return (
            f"<seq={self.sequence} ack={self.sequence_ack} "
            f"flags={self.flags:#04x} rel={self.reliable_state:#04x}>"
        )


class BadPacket(Exception):
    pass


def decode_header(data):
    """Parse an in-band header. Raises BadPacket on a short or corrupt packet."""
    if len(data) < HEADER_MIN:
        raise BadPacket(f"short packet ({len(data)}B)")
    seq, ack, flags, checksum = struct.unpack_from("<iiBH", data, 0)
    off = HEADER_MIN
    if len(data) < off + 1:
        raise BadPacket("no reliable_state byte")
    relstate = data[off]
    off += 1
    choked = None
    if flags & wire.FLAG_CHOKED:
        if len(data) < off + 1:
            raise BadPacket("truncated choked field")
        choked = data[off]
        off += 1
    challenge = None
    if flags & wire.FLAG_CHALLENGE:
        if len(data) < off + 4:
            raise BadPacket("truncated challenge field")
        challenge = struct.unpack_from("<I", data, off)[0]
        off += 4
    return Header(
        sequence=seq,
        sequence_ack=ack,
        flags=flags,
        checksum=checksum,
        reliable_state=relstate,
        choked=choked,
        challenge=challenge,
        body_offset=off,
    )


def verify_checksum(data):
    """True if the stored checksum matches. Coverage starts at reliable_state."""
    if len(data) < HEADER_MIN + 1:
        return False
    stored = struct.unpack_from("<H", data, 9)[0]
    return wire.fold_checksum(data[HEADER_MIN:]) == stored


def latch_subchannel(data, header):
    """The 3-bit subchannel index at the start of the reliable region.

    Read on its own, before any block parsing, so an ack survives a malformed
    block. Returns None if the packet is not reliable or is truncated.
    """
    if not header.reliable:
        return None
    br = wire.BitReader(data)
    br.pos = header.body_offset * 8
    try:
        return br.read_ubit(wire.SUBCHANNEL_BITS)
    except EOFError:
        return None


def unwrap(datagram):
    """Resolve a datagram to in-band bytes.

    Returns (kind, payload). kind is 'inband' | 'snap' | 'oob' | 'split' |
    'compressed_other' | 'compressed_bad' | 'short'. For 'snap' the payload is
    the INFLATED inner packet, which then goes through exactly the same path as
    a plain in-band packet - same sequence space, same reliable state.

    Total by contract: never raises. A malformed compressed block yields
    'compressed_bad' rather than an exception. One corrupted datagram - in
    flight, truncated, or spoofed at our ephemeral port - must cost one packet,
    never the process; this runs unattended for hours.
    """
    kind = wire.classify(datagram)
    if kind == "inband":
        return "inband", datagram
    if kind == "compressed":
        try:
            inner = codec.inflate_snap(datagram)
        except codec.SnappyError:
            return "compressed_bad", datagram
        if inner is None:
            return "compressed_other", datagram
        return "snap", inner
    return kind, datagram


# --- outbound --------------------------------------------------------------


def build_packet(
    out_seq, in_seq, challenge, in_reliable_state, reliable_region=b"", unreliable=b""
):
    """Assemble an outbound packet and its checksum.

    RELIABLE is set only when a reliable region is present. CHALLENGE is always
    set: the relay validates it. Undocumented flag bits are never emitted.
    """
    flags = wire.FLAG_CHALLENGE
    if reliable_region:
        flags |= wire.FLAG_RELIABLE
    after = (
        struct.pack("<B", in_reliable_state & 0xFF)
        + struct.pack("<I", challenge)
        + reliable_region
        + unreliable
    )
    checksum = wire.fold_checksum(after)
    return (
        struct.pack("<ii", out_seq, in_seq)
        + struct.pack("<B", flags)
        + struct.pack("<H", checksum)
        + after
    )


def build_ack(out_seq, in_seq, challenge, in_reliable_state):
    """Pure header: acks the relay and echoes our reliable receive state."""
    return build_packet(out_seq, in_seq, challenge, in_reliable_state)
