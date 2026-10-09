"""Datagrams -> netchannel packets -> walked messages for the hooks.

The receive path is net/receiver.py, the same object the live session
feeds: split reassembly, SNAP inflate, checksum, sequence, reliable
subchannel reassembly, resends of unacked reliable packets, the message-chain
walk. This module only reads the walked messages (voice, hooks) and records
a fate for every packet, so a loss has a named cause.

Packet fates (one per netchannel packet, plain or joined from split parts):

    ok                       chain(s) walked to the end
    resend                   a repeat of a reliable packet already delivered
                             (our recorded acks did not flip its bit); its
                             reliable region is skipped, its tail walked
    oob                      connectionless, not netchannel
    drop_<kind>              compressed_bad / compressed_other / short
    nested_split, bad_checksum, bad_header
    stale_sequence           sequence <= highest seen (duplicate or reordered)
    reliable_error:<why>     reliable region unparseable; the unreliable tail
                             after it has no known start and is lost with it
    unreliable_stop:<why>    tail walk stopped early; messages after the stop
                             point are lost
    reliable_stop:<why>      a completed reliable stream stopped early
    split_dropped            only with reassemble_splits=False

Counters beside the fates: `seq_lost` = sequence numbers sent by the relay
and never received (`seq_choked`, declared choked by the header, were never
sent), `challenge_change` = new connections detected from the header when the
recording has no marker, `resend_skipped` / `resend_mismatch`.

Our own outbound packets are needed to recognize resends: feed the
recording's non-inbound records (the session markers too) to `observe` in
file order (`frame()` does).

Messages before a stop point, voice among them, are still handed over: the
walk is sequential, so everything before the stop was sized correctly.
"""

import zlib
from dataclasses import dataclass

from ..net import receiver, wire
from .recording import Boundaries

SVC_VOICEDATA = 15
DATAGRAM_OUT = 0x02  # same record type in .tvd and .hcap
STRING_TABLE_IDS = (12, 13)  # svc_CreateStringTable, svc_UpdateStringTable
# decoded by the walk, never handed to on_msg
_LIFECYCLE = (
    wire.NET_DISCONNECT,
    wire.NET_TICK,
    wire.NET_SIGNONSTATE,
    wire.SVC_SERVERINFO,
)


def steam_voice_crc_ok(payload):
    """Steam voice ends with CRC32 of everything before it: a check that a
    message was cut from the stream at the right bit, independent of the walk."""
    return len(payload) > 12 and zlib.crc32(payload[:-4]) == int.from_bytes(payload[-4:], "little")


@dataclass(frozen=True)
class VoiceMessage:
    """One svc_VoiceData as it left the chain."""

    index: int  # datagram index of the packet that carried it
    t_ns: int  # receive time of that packet (client clock)
    session: int
    sequence: int  # netchannel sequence of the packet
    tick: int  # last server tick seen in this session (server clock)
    from_client: int  # slot; NOT an identity (renumbered per map)
    proximity: int
    data: bytes  # Steam voice payload; first 8 bytes = SteamID64
    via_split: bool  # carried by a packet reassembled from -2 parts
    stream: str  # "reliable" | "unreliable"


@dataclass(frozen=True)
class PacketFate:
    index: int  # datagram index (last part for a joined packet)
    session: int
    fate: str
    via_split: bool
    parts: tuple = ()  # datagram indices of split parts, if joined


def voice_fields(payload, start):
    """svc_VoiceData body at `start` -> (from_client, proximity, data)."""
    br = wire.BitReader(payload)
    br.pos = start
    from_client = br.read_byte()
    proximity = br.read_byte()
    nbits = br.read_ubit(16)
    return from_client, proximity, br.read_bytes(nbits // 8)


def _stop(stop):
    return "message cap" if stop["id"] is None else f"id{stop['id']}:{stop['kind']}"


class Framer:
    """Feed recording.Datagram in order; voice messages collect in `voice`,
    hooks get the other walked messages, and a PacketFate is kept per packet
    (keep_fates).

    reassemble_splits=False drops split parts instead of joining them, as a
    client without -2 support would."""

    def __init__(
        self,
        reassemble_splits=True,
        split_timeout_ns=5_000_000_000,
        keep_fates=True,
        on_table=None,
        on_info=None,
        on_msg=None,
        on_packet=None,
    ):
        self.on_table = on_table  # (payload, start_bit, end_bit) of 12/13
        # (mid, payload, start_bit, end_bit) of every other sized message; the
        # range excludes the 6-bit id. Called with the packet context set in
        # self.cur_t_ns / self.cur_session / self.tick / self.cur_packet.
        self.on_msg = on_msg
        self.on_info = on_info  # svc_ServerInfo dict (map, hostname, ...)
        self.on_packet = on_packet  # receiver.Packet; its fate in self.cur_fate
        self.rx = receiver.Receiver(split_timeout_ns, reassemble_splits, infer_connections=True)
        self.keep_fates = keep_fates
        self.fates = []
        self.voice = []
        self.counters = self.rx.counters
        self.session = None
        self.boundary = Boundaries()
        self.cur_t_ns = self.cur_session = 0
        self.cur_packet = None
        self.cur_stream = None

    @property
    def splits(self):
        return self.rx.splits

    @property
    def in_seq(self):
        return self.rx.in_seq

    @property
    def tick(self):
        return self.rx.tick

    @property
    def replay_bit(self):
        return self.rx.replay_bit

    def observe(self, t_ns, rtype, data):
        """A non-inbound record of the recording, in file order."""
        if self.boundary(rtype, data):
            # Closed here, not at the next inbound datagram: our connected
            # reply is written before the relay's first packet, and it is
            # the new session's first ack.
            if self.session is not None:
                self.rx.end_session()
                self.session = None
        elif rtype == DATAGRAM_OUT:
            self.rx.observe_outbound(data)

    def _fate(self, index, session, fate, via_split, parts=()):
        self.counters[fate] += 1
        if via_split:
            self.counters["split:" + fate] += 1
        if self.keep_fates:
            self.fates.append(PacketFate(index, session, fate, via_split, parts))

    def feed(self, dg):
        if dg.session != self.session:
            if self.session is not None:
                self.rx.end_session()
            self.session = dg.session
        self.counters["datagrams"] += 1
        pkt = self.rx.feed(dg.data, dg.t_ns, dg.index)
        if pkt is not None:
            self._packet(pkt, dg.session)

    def finish(self):
        self.rx.finish()

    def _packet(self, pkt, session):
        fate = pkt.fate
        found = []
        if fate in (receiver.OK, receiver.RESEND):
            if pkt.reliable_error:
                why = (
                    "EOFError"
                    if pkt.reliable_error == "no_subchannel_index"
                    else pkt.reliable_error
                )
                fate = "reliable_error:" + why
            self.cur_t_ns, self.cur_session, self.cur_packet = pkt.t_ns, session, pkt
            for w in pkt.walks:
                self.cur_stream = w.stream
                self._dispatch(w, pkt, session, found)
                if w.stop is not None and fate == receiver.OK:
                    fate = f"{w.stream}_stop:" + _stop(w.stop)
            self.cur_stream = None
            self.voice.extend(found)
            self.counters["voice_msgs"] += len(found)
            self.counters["voice_crc_bad"] += sum(not steam_voice_crc_ok(v.data) for v in found)
            if pkt.via_split:
                self.counters["voice_msgs_via_split"] += len(found)
        self._fate(pkt.index, session, fate, pkt.via_split, pkt.parts)
        self.cur_fate = fate
        if self.on_packet is not None:
            self.on_packet(pkt)

    def _dispatch(self, w, pkt, session, found):
        rx, payload = self.rx, w.payload
        for mid, start, end, fields in w.msgs:
            if mid == SVC_VOICEDATA:
                fc, prox, data = voice_fields(payload, start)
                found.append(
                    VoiceMessage(
                        pkt.index,
                        pkt.t_ns,
                        session,
                        pkt.header.sequence,
                        rx.tick,
                        fc,
                        prox,
                        data,
                        pkt.via_split,
                        w.stream,
                    )
                )
            elif mid == wire.NET_TICK:
                if fields["tick"] > 0:
                    rx.tick = max(rx.tick, fields["tick"])
            elif mid == wire.SVC_SERVERINFO:
                if self.on_info is not None:
                    self.on_info(fields)
            elif mid in _LIFECYCLE:
                pass
            elif mid in STRING_TABLE_IDS and self.on_table is not None:
                self.on_table(payload, start, end)
            elif self.on_msg is not None:
                self.on_msg(mid, payload, start, end)


def attach(recording, framer):
    """Route the recording's non-inbound records (our acks among them) to
    framer.observe in file order; its own on_event, if any, still sees them."""
    prev = recording.on_event

    def on_event(t_ns, rtype, data):
        framer.observe(t_ns, rtype, data)
        if prev is not None:
            prev(t_ns, rtype, data)

    recording.on_event = on_event


def frame(recording, framer):
    """Iterate a recording through a framer; yields each datagram after
    feeding it."""
    attach(recording, framer)
    for dg in recording:
        framer.feed(dg)
        yield dg


def run(recording, **kw):
    fr = Framer(**kw)
    for _dg in frame(recording, fr):
        pass
    fr.finish()
    return fr
