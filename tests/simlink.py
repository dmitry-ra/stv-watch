"""A simulated relay and a faulty link, for driving the real client offline.

The relay side models what the receive path depends on, after the engine's
CNetChan (net_chan.cpp): one reliable batch in flight on a subchannel, its
bit flipped in the sender's out-state when sent; an ack packet whose
sequence_ack covers the batch either shows the receiver's bit flipped (done)
or not (resend the same region on the same subchannel); data is taken only
from the head of the queue; a message stream larger than one batch goes out
as a multi-fragment transfer, at most 7 fragments of 256 bytes per batch;
datagrams over `max_packet` bytes leave as `-2` split parts. `changelevel()`
clears the queue and the batch in flight, as CBaseClient::Inactivate ->
CNetChan::Clear does.

The link drops, duplicates and delays datagrams, and can drop single `-2`
parts. Everything is seeded: a run is a function of its seed.

Messages are svc_Print strings, so the receiver's walk sizes them and a test
reads them back from the walked reliable streams.
"""

import random
import struct

from stvwatch.net import netchan, wire

SVC_PRINT = 7
FRAG = 256
MAX_FRAGS = 7


def print_body(text):
    w = wire.BitWriter()
    w.write_ubit(SVC_PRINT, wire.NETMSG_TYPE_BITS)
    w.write_string(text)
    return w.get_bytes()


def read_print(payload, start):
    br = wire.BitReader(payload)
    br.pos = start
    return br.read_string()


class Link:
    """One direction. send() at time t; due(t) returns what arrives by t, in
    arrival order."""

    def __init__(self, rng, loss=0.0, dup=0.0, delay_s=0.02, jitter_s=0.0, part_loss=0.0):
        self.rng = rng
        self.loss, self.dup, self.delay, self.jitter = loss, dup, delay_s, jitter_s
        self.part_loss = part_loss
        self.q = []
        self.n = 0
        self.dropped = self.duplicated = self.parts_dropped = 0

    def send(self, data, t):
        is_part = wire.classify(data) == "split"
        if is_part and self.rng.random() < self.part_loss:
            self.parts_dropped += 1
            return
        if self.rng.random() < self.loss:
            self.dropped += 1
            return
        copies = 2 if self.rng.random() < self.dup else 1
        self.duplicated += copies - 1
        for _ in range(copies):
            at = t + self.delay + self.rng.random() * self.jitter
            self.q.append((at, self.n, data))
            self.n += 1

    def due(self, t):
        self.q.sort()
        out = [d for at, _n, d in self.q if at <= t]
        self.q = [x for x in self.q if x[0] > t]
        return out


class Relay:
    def __init__(self, challenge, bodies, max_packet=1200, split_size=600):
        self.challenge = challenge
        self.queue = list(bodies)
        self.max_packet, self.split_size = max_packet, split_size
        self.seq = 0
        self.out_state = 0
        self.sub = 0
        self.batch = None  # dict: sub, bits, seq, resend
        self.transfer = None  # dict: data, next
        self.group = 100
        self.tick = 1000
        self.peer_seq = 0  # highest client sequence seen
        self.resends = 0
        self.sent_batches = 0

    # -- reliable side -------------------------------------------------------
    def _region(self, w):
        """Write the next batch's region (new or resend) into w."""
        b = self.batch
        if b is not None:
            if not b["resend"]:
                return False
            b["resend"] = False
            b["seq"] = self.seq
            self.resends += 1
            for bit in b["bits"]:
                w.write_one_bit(bit)
            return True
        if self.transfer is None:
            if not self.queue:
                return False
            body = self.queue.pop(0)
            self.transfer = {"data": body, "next": 0, "multi": len(body) > MAX_FRAGS * FRAG}
        t = self.transfer
        r = wire.BitWriter()
        r.write_ubit(self.sub, wire.SUBCHANNEL_BITS)
        r.write_one_bit(1)  # stream 0 follows
        data = t["data"]
        if not t["multi"]:
            r.write_one_bit(0)
            r.write_one_bit(0)  # not compressed
            r.write_varint32(len(data))
            r.write_bytes(data)
            self.transfer = None
        else:
            total = (len(data) + FRAG - 1) // FRAG
            n = min(MAX_FRAGS, total - t["next"])
            r.write_one_bit(1)
            r.write_ubit(t["next"], 18)
            r.write_ubit(n, 3)
            if t["next"] == 0:
                r.write_one_bit(0)  # not a file
                r.write_one_bit(0)  # not compressed
                r.write_ubit(len(data), 26)
            r.write_bytes(data[t["next"] * FRAG : (t["next"] + n) * FRAG])
            t["next"] += n
            if t["next"] >= total:
                self.transfer = None
        r.write_one_bit(0)  # stream 1: nothing
        bits = list(r._bits)
        for bit in bits:
            w.write_one_bit(bit)
        self.out_state ^= 1 << self.sub
        self.batch = {"sub": self.sub, "bits": bits, "seq": self.seq, "resend": False}
        self.sub = (self.sub + 1) % wire.SUBCHANNEL_COUNT
        self.sent_batches += 1
        return True

    def on_ack(self, packet):
        try:
            h = netchan.decode_header(packet)
        except netchan.BadPacket:
            return
        if h.sequence <= self.peer_seq:
            return
        self.peer_seq = h.sequence
        b = self.batch
        if b is None or b["resend"] or h.sequence_ack < b["seq"]:
            return
        if ((h.reliable_state >> b["sub"]) & 1) == ((self.out_state >> b["sub"]) & 1):
            self.batch = None
        else:
            b["resend"] = True

    def changelevel(self):
        self.queue, self.batch, self.transfer = [], None, None

    @property
    def idle(self):
        return not self.queue and self.batch is None and self.transfer is None

    # -- packets ---------------------------------------------------------------
    def packet(self):
        """-> list of datagrams for one packet (one, or its -2 parts)."""
        self.seq += 1
        self.tick += 1
        w = wire.BitWriter()
        reliable = self._region(w)
        w.write_ubit(wire.NET_TICK, wire.NETMSG_TYPE_BITS)
        w.write_long(self.tick)
        w.write_ubit(0, 16)
        w.write_ubit(0, 16)
        pad = (-w.nbits()) % 8
        w.write_ubit((1 << pad) - 1, pad)
        body = w.get_bytes()
        flags = wire.FLAG_CHALLENGE | (wire.FLAG_RELIABLE if reliable else 0)
        after = struct.pack("<B", 0) + struct.pack("<I", self.challenge) + body
        pkt = (
            struct.pack("<ii", self.seq, self.peer_seq)
            + struct.pack("<B", flags)
            + struct.pack("<H", wire.fold_checksum(after))
            + after
        )
        if len(pkt) <= self.max_packet:
            return [pkt]
        self.group += 1
        n = self.split_size
        parts = [pkt[i : i + n] for i in range(0, len(pkt), n)]
        return [
            struct.pack("<iiBBH", wire.MARK_SPLIT, self.group, len(parts), k, n) + p
            for k, p in enumerate(parts)
        ]


def run(
    session,
    relay,
    down,
    up,
    steps,
    step_s=0.015,
    t0_ns=1_000_000_000_000,
    on_inbound=None,
    on_outbound=None,
    events=None,
):
    """Drive relay -> down link -> session -> up link -> relay for `steps`
    packets, or until the relay is idle and the links are empty. `events` maps
    a step number to a callable(relay). -> steps run."""
    sock = session.conn.sock
    sent = 0
    for k in range(steps):
        if events and k in events:
            events[k](relay)
        t = k * step_s
        for d in relay.packet():
            down.send(d, t)
        for d in down.due(t):
            t_ns = t0_ns + int(t * 1e9)
            if on_inbound:
                on_inbound(d, t_ns)
            session.feed(d, t_ns)
        for p in sock.sent[sent:]:
            if on_outbound:
                on_outbound(p, t0_ns + int(t * 1e9))
            up.send(p, t)
        sent = len(sock.sent)
        for p in up.due(t):
            relay.on_ack(p)
        if relay.idle and not down.q and not up.q and k > 10:
            return k + 1
    return steps


def seeded(seed):
    return random.Random(seed)
