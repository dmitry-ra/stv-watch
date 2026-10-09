#!/usr/bin/env python3
"""One receive path: datagram -> netchannel packet -> walked message streams.

The live session (session.py) and every offline reader (stream/framing.py)
feed datagrams through the same Receiver, so a recording replays through
exactly the code the live client ran. What differs is only what the caller
does with the result: the session acks and climbs the ladder, the framer
extracts voice and hooks.

Per datagram, in this order:
  1. `-2` split parts are joined (split.py); a part alone yields nothing.
  2. SNAP is inflated (netchan.unwrap); anything not in-band is a fate.
  3. Checksum BEFORE any state: a corrupt packet with a high sequence would
     otherwise poison in_seq for good.
  4. Sequence: stale or duplicate is a fate; a gap is counted as choked
     (declared by the header) or lost.
  5. Reliable region: the 3-bit subchannel is latched first; the region
     either parses (`reliable_ok`, the receiver flips its bit) or is a fate
     detail, and then the unreliable tail has no known start.
  6. Offline, our recorded acks (`observe_outbound`, the dump's
     DATAGRAM_OUT) tell per delivered reliable packet whether the live
     client flipped its bit (counters ack_flipped / ack_unflipped, `_split`
     for packets joined from -2). The sender repeats an unflipped packet on
     the same subchannel with the same region (CNetChan::ProcessPacket):
     such a packet is checked bit-equal, its region skipped, its tail walked.
     Live every delivered packet is flipped, so the session does not observe
     its acks; this acts on dumps of a client that dropped what this reader
     joins (a client without -2 support).
  7. Message streams are walked by chain.walk_detect; the replay bit is
     latched from reliable streams.
"""

from collections import Counter
from dataclasses import dataclass, field

from . import chain, netchan, reliable, wire
from .split import SplitReassembler

# Packet fates.
OK = "ok"
RESEND = "resend"


@dataclass
class Walk:
    stream: str  # "reliable" | "unreliable"
    payload: bytes
    msgs: list  # chain.walk entries (id, start, end, fields)
    stop: object  # None = clean end, else chain.walk stop dict

    @property
    def clean(self):
        return self.stop is None


@dataclass
class Packet:
    index: int  # datagram index (the completing part when joined)
    t_ns: int
    via_split: bool
    parts: tuple = ()  # datagram indices of the joined parts
    fate: str = OK
    header: object = None
    payload: bytes = b""
    subchannel: object = None  # latched index of a reliable packet
    reliable_ok: bool = False  # region parsed: the receiver flips its bit
    reliable_error: str = ""  # why the region did not parse
    resend_of: object = None  # index of the packet this one repeats
    prev_seq: int = 0  # highest sequence before this packet
    lost: int = 0  # sequence numbers skipped before it, not declared choked
    walks: list = field(default_factory=list)


def _reliable_why(e):
    if isinstance(e, reliable.ReliableError):
        why = str(e).split(" ")[0]
        return {
            "continuation": "unseen_transfer",
            "fragment": "frag_past_end",
            "declared": "over_cap",
            "inflate": "inflate",
        }.get(why, why)
    return type(e).__name__


def _bits(data, start, end):
    n = end - start
    return (int.from_bytes(data, "little") >> start) & ((1 << n) - 1), n


class _Delivered:
    """The last reliable packet delivered on one subchannel, until an ack
    tells whether the sending side saw it received."""

    __slots__ = ("index", "seq", "bit_before", "flipped", "region", "region_bits", "via_split")

    def __init__(self, index, seq, bit_before, region, region_bits, via_split):
        self.index, self.seq, self.bit_before = index, seq, bit_before
        self.via_split = via_split
        self.flipped = None
        self.region, self.region_bits = region, region_bits


class Receiver:
    """Per connection. The live session owns exactly one connection, leaves
    `infer_connections` off and sets `challenge`: a packet whose header
    carries another one is not ours and touches no state. Offline the same
    rule holds once our own packets of the connection are seen (the challenge
    they carry); before that, with `infer_connections`, a header challenge
    change starts a new connection (recordings without session markers)."""

    def __init__(
        self, split_timeout_ns=5_000_000_000, reassemble_splits=True, infer_connections=False
    ):
        self.reassemble_splits = reassemble_splits
        self.infer_connections = infer_connections
        self.splits = SplitReassembler(split_timeout_ns)
        self.counters = Counter()
        self._auto_index = 0
        self.reset()

    def reset(self):
        """A new connection: sequence, transfers and ack state start over.
        Open split groups are the caller's to end (end_session)."""
        self.in_seq = 0
        self.challenge = None
        self.tick = 0  # the consumer's; reset with the connection
        self.replay_bit = True
        self.reasm = reliable.Reassembler()
        self.out_bits = None  # our last sent in_reliable_state
        self.out_challenge = None  # the challenge our sent packets carry
        self.out_ack = 0
        self.delivered = {}  # subchannel -> _Delivered

    def end_session(self):
        self.splits.end_session()
        self.reset()

    def finish(self):
        self.splits.finish()

    # -- our acks -----------------------------------------------------------
    def observe_outbound(self, packet):
        """One of our sent packets (live: as sent; offline: DATAGRAM_OUT).
        Connectionless ones (getchallenge, connect) carry no ack and precede
        the next session's marker in a dump: they are not ours to judge by."""
        if packet[:4] == b"\xff\xff\xff\xff":
            return
        try:
            h = netchan.decode_header(packet)
        except netchan.BadPacket:
            return
        if h.challenge is not None:
            self.out_challenge = h.challenge
        bits = h.reliable_state
        for sub, d in self.delivered.items():
            if d.flipped is None and h.sequence_ack >= d.seq:
                d.flipped = ((bits >> sub) & 1) != d.bit_before
                key = "ack_flipped" if d.flipped else "ack_unflipped"
                self.counters[key] += 1
                if d.via_split:
                    self.counters[key + "_split"] += 1
        self.out_bits, self.out_ack = bits, h.sequence_ack

    def _track(self, sub, index, seq, region, nbits, via_split):
        self.delivered[sub] = _Delivered(
            index, seq, (self.out_bits >> sub) & 1, region, nbits, via_split
        )

    # -- inbound ------------------------------------------------------------
    def feed(self, data, t_ns, index=None):
        """-> Packet, or None for a split part that completed nothing.
        Never raises on malformed input."""
        if index is None:
            index = self._auto_index
        self._auto_index = index + 1
        if wire.classify(data) == "split":
            self.counters["split_parts"] += 1
            if not self.reassemble_splits:
                return Packet(index, t_ns, True, (index,), fate="split_dropped")
            joined = self.splits.feed(data, t_ns, index)
            if joined is None:
                return None
            return self._packet(Packet(joined.index, joined.t_ns, True, joined.parts), joined.data)
        if self.reassemble_splits:
            self.splits.expire(t_ns)
        return self._packet(Packet(index, t_ns, False), data)

    def _packet(self, pkt, data):
        kind, payload = netchan.unwrap(data)
        if kind == "split":
            pkt.fate = "nested_split"
            return pkt
        if kind == "oob":
            pkt.fate = "oob"
            return pkt
        if kind not in ("inband", "snap"):
            pkt.fate = "drop_" + kind
            return pkt
        if not netchan.verify_checksum(payload):
            pkt.fate = "bad_checksum"
            return pkt
        try:
            header = netchan.decode_header(payload)
        except netchan.BadPacket:
            pkt.fate = "bad_header"
            return pkt
        pkt.header, pkt.payload = header, payload
        ours = self.out_challenge if self.infer_connections else self.challenge
        if None not in (header.challenge, ours) and header.challenge != ours:
            pkt.fate = "challenge_mismatch"
            return pkt
        if self.infer_connections and header.challenge not in (None, self.challenge):
            if self.challenge is not None:
                self.counters["challenge_change"] += 1
                self.reset()
            self.challenge = header.challenge
        pkt.prev_seq = self.in_seq
        if header.sequence <= self.in_seq:
            pkt.fate = "stale_sequence"
            return pkt
        if self.in_seq:
            gap = header.sequence - self.in_seq - 1
            choked = header.choked or 0
            pkt.lost = max(0, gap - choked)
            self.counters["seq_choked"] += min(gap, choked)
            self.counters["seq_lost"] += pkt.lost
        self.in_seq = header.sequence

        tail = header.body_offset * 8
        if header.reliable:
            tail = self._reliable(pkt, payload, header)
        if tail is not None and tail < len(payload) * 8:
            msgs, stop, _bit = chain.walk_detect(payload, tail, self.replay_bit)
            pkt.walks.append(Walk("unreliable", payload, msgs, stop))
        return pkt

    def _reliable(self, pkt, payload, header):
        """-> bit where the unreliable tail starts, or None if unknown."""
        sub = netchan.latch_subchannel(payload, header)
        pkt.subchannel = sub
        if sub is None:
            pkt.reliable_error = "no_subchannel_index"
            return None
        prev = self.delivered.get(sub)
        if prev is not None and prev.flipped is False:
            start = header.body_offset * 8
            if (
                start + prev.region_bits <= len(payload) * 8
                and _bits(payload, start, start + prev.region_bits)[0] == prev.region
            ):
                # the region repeats bit for bit: delivered already. Tracked
                # on, in case our acks missed this copy too.
                pkt.fate, pkt.resend_of = RESEND, prev.index
                self.counters["resend_skipped"] += 1
                self._track(
                    sub, prev.index, header.sequence, prev.region, prev.region_bits, pkt.via_split
                )
                return start + prev.region_bits
            self.counters["resend_mismatch"] += 1
        try:
            _idx, done = self.reasm.feed(payload, header)
        except (reliable.ReliableError, EOFError, ValueError) as e:
            pkt.reliable_error = _reliable_why(e)
            return None
        pkt.reliable_ok = True
        start, end = header.body_offset * 8, self.reasm.end_bit
        if self.out_bits is None:
            self.delivered.pop(sub, None)  # no acks seen: cannot judge
        else:
            region, nbits = _bits(payload, start, end)
            self._track(sub, pkt.index, header.sequence, region, nbits, pkt.via_split)
        for stream in done:
            msgs, stop, bit = chain.walk_detect(stream, 0, self.replay_bit)
            if bit is not None and bit != self.replay_bit:
                self.replay_bit = bit
                self.counters["replay_bit_switch"] += 1
            pkt.walks.append(Walk("reliable", stream, msgs, stop))
        return end
