"""Protocol layer: bit codec, outbound messages, Snappy and LZSS, netchannel
header, dump format.

The reference CONNECTED packet in data/ was captured from a stock client and is
used read-only: proving the generated packet matches it is what allows the
runtime to carry no canned bytes at all.
"""

import os
import tempfile

import pytest

from stvwatch.net import codec, dump, messages, netchan, wire

GOLD_REPLY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "connected_reply.bin")


# --- wire ------------------------------------------------------------------


def test_bit_roundtrip():
    w = wire.BitWriter()
    w.write_ubit(5, 3)
    w.write_varint32(300)
    w.write_string("hi")
    w.write_long(-7)
    w.write_one_bit(1)
    w.write_byte(0xAB)
    r = wire.BitReader(w.get_bytes())
    assert r.read_ubit(3) == 5
    assert r.read_varint32() == 300
    assert r.read_string() == "hi"
    assert r.read_long() == -7
    assert r.read_one_bit() == 1
    assert r.read_byte() == 0xAB


def test_bitreader_reads_reference_packet():
    """The reference CONNECTED reply decodes to its documented contents."""
    if not os.path.exists(GOLD_REPLY):
        pytest.skip("reference packet not present")
    br = wire.BitReader(open(GOLD_REPLY, "rb").read()[16:])
    assert br.read_ubit(3) == 0  # subchannel
    assert (br.read_one_bit(), br.read_one_bit(), br.read_one_bit()) == (1, 0, 0)
    body = wire.BitReader(br.read_bytes(br.read_varint32()))
    assert body.read_ubit(6) == wire.NET_SETCONVAR
    cvars = dict((body.read_string(), body.read_string()) for _ in range(body.read_byte()))
    assert len(cvars) == 27 and cvars["name"] == "unnamed"
    assert cvars["rate"] == "80000"
    assert body.read_ubit(6) == wire.NET_SIGNONSTATE
    assert body.read_byte() == wire.SIGNON_CONNECTED
    assert body.read_long() == -1


# --- messages --------------------------------------------------------------


def test_generated_connected_reply_matches_reference():
    """Every MEANINGFUL bit of the generated CONNECTED reply equals the captured
    one. The tail differs only in the message stream's byte-alignment padding,
    which carries no information - so comparison is bit-exact up to the last
    meaningful bit, not byte-exact over the whole buffer."""
    if not os.path.exists(GOLD_REPLY):
        pytest.skip("reference packet not present")
    gold = open(GOLD_REPLY, "rb").read()[16:]
    body = messages.connected_reply_body("unnamed")
    mine = messages.reliable_region(body, 0)
    assert len(mine) == len(gold), (len(mine), len(gold))

    # The message stream is byte-padded before being embedded, so the region has
    # two padding runs that carry no information: the stream's own tail padding,
    # and the region's final alignment. Compare content only.
    inner = messages.setconvar(messages.userinfo_for("unnamed"))
    messages.signonstate(wire.SIGNON_CONNECTED, -1, inner)
    body_used = inner.nbits()  # 3684: real content
    lw = wire.BitWriter()
    lw.write_varint32(len(body))
    prefix = 3 + 3 + lw.nbits()  # subchan + flags + length
    meaningful = list(range(prefix + body_used))  # everything up to the padding
    meaningful.append(prefix + len(body) * 8)  # the stream-1 "no data" bit

    g, m = wire.BitReader(gold), wire.BitReader(mine)
    diff = []
    for i in meaningful:
        g.pos = m.pos = i
        if g.read_ubit(1) != m.read_ubit(1):
            diff.append(i)
    assert not diff, f"meaningful bits differ at {diff}"
    assert body_used < len(body) * 8, "expected stream padding to exist"


# --- codec -----------------------------------------------------------------


def snappy_varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def test_snappy_decodes_literals_and_overlapping_copies():
    """A hand-made raw block: a literal, a 1-byte-offset copy that overlaps
    its own output (run-length), a 2-byte-offset copy."""
    lit = b"abcd"
    block = snappy_varint(4 + 7 + 4 + 2)
    block += bytes([(len(lit) - 1) << 2]) + lit  # literal "abcd"
    block += bytes([0x01 | ((7 - 4) << 2), 1])  # copy1: 7 bytes from 1 back: "ddddddd"
    block += bytes([0x02 | ((4 - 1) << 2)]) + (11).to_bytes(2, "little")  # copy2: "abcd"
    block += bytes([(2 - 1) << 2]) + b"!?"
    assert codec.uncompress(block) == b"abcd" + b"d" * 7 + b"abcd" + b"!?"
    snap = (-3).to_bytes(4, "little", signed=True) + b"SNAP" + block
    assert codec.inflate_snap(snap) == b"abcdddddddd" + b"abcd!?"
    assert codec.inflate_compressed(b"SNAP" + block) == codec.uncompress(block)
    for bad in (block[:-1], snappy_varint(5) + bytes([0x01 | (3 << 2), 9])):
        with pytest.raises(codec.SnappyError):
            codec.uncompress(bad)
    assert netchan.unwrap(snap[:8] + b"\xff\xff")[0] == "compressed_bad"


def test_lzss_decodes_literals_and_back_references():
    """Valve LZSS: command bits LSB first, 0 = literal, 1 = 12-bit distance
    and 4-bit count; count 1 ends the stream."""
    data = b"LZSS" + (12).to_bytes(4, "little")  # declared more than the stream holds
    data += bytes([0b00110000])  # four literals, two references
    data += b"abcd"
    data += bytes([(3 >> 4), ((3 & 0xF) << 4) | (4 - 1)])  # copy 4 from 4 back
    data += bytes([0, 0])  # count 1: end
    assert codec.lzss_uncompress(data) == b"abcdabcd"
    assert codec.inflate_compressed(data) == b"abcdabcd"
    with pytest.raises(codec.SnappyError):
        codec.inflate_compressed(b"ZZZZ1234")


# --- netchan ---------------------------------------------------------------


def test_outbound_packet_checksum_selfconsistent():
    pkt = netchan.build_ack(out_seq=3, in_seq=9, challenge=0xDEADBEEF, in_reliable_state=0x05)
    assert netchan.verify_checksum(pkt)
    h = netchan.decode_header(pkt)
    assert (h.sequence, h.sequence_ack, h.reliable_state) == (3, 9, 0x05)
    assert h.challenge == 0xDEADBEEF and not h.reliable


# --- dump ------------------------------------------------------------------


def test_dump_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "x.tvd")
        with dump.DumpWriter(path, "127.0.0.1:27020", fsync_ms=0) as w:
            w.event(dump.SESSION_START, attempt=1)
            w.write(dump.DATAGRAM_IN, b"\xde\xad")
            w.event(dump.SIGNON, state=6)
            w.write(dump.DATAGRAM_OUT, b"\x01")
            w.event(dump.BROKEN, cause="changelevel", detail="")
        r = dump.DumpReader(path)
        assert r.endpoint == "127.0.0.1:27020" and r.version == dump.VERSION
        recs = list(r)
        assert [t for _, t, _ in recs] == [
            dump.SESSION_START,
            dump.DATAGRAM_IN,
            dump.SIGNON,
            dump.DATAGRAM_OUT,
            dump.BROKEN,
        ]
        assert not r.truncated_tail
        evs = {t: f for _, t, f in dump.DumpReader(path).events()}
        assert evs[dump.SIGNON]["state"] == 6
        assert evs[dump.BROKEN]["cause"] == "changelevel"


def test_dump_survives_truncated_tail():
    """A kill -9 mid-write must leave every complete record readable."""
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "x.tvd")
        with dump.DumpWriter(path, "127.0.0.1:27020", fsync_ms=0) as w:
            for i in range(5):
                w.write(dump.DATAGRAM_IN, bytes([i]) * 10)
        raw = open(path, "rb").read()
        open(path, "wb").write(raw[:-4])  # chop mid-record
        r = dump.DumpReader(path)
        assert len(list(r)) == 4
        assert r.truncated_tail
