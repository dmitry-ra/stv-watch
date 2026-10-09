"""Session state-machine tests.

No network and no recordings: a fake socket records what the session sends, a fake
sink records the lifecycle callbacks, and `poll(now=...)` takes an injected clock
so every breaker is exercised deterministically. This is the layer that decides
whether a session survives a map change or reconnects into a flood, so it is
tested by driving the transitions directly rather than hoping a live run hits
them.
"""

import os
import struct

import pytest

from stvwatch.net import messages, netchan, supervisor, wire
from stvwatch.net import session as sess
from stvwatch.net import split as sp


class FakeSock:
    def __init__(self):
        self.sent = []

    def sendto(self, packet, addr):
        self.sent.append(packet)


class FakeConn:
    def __init__(self):
        self.sock = FakeSock()
        self.server = ("127.0.0.1", 27020)
        self.challenge = 0xDEADBEEF


class FakeSink:
    def __init__(self):
        self.signons = []
        self.breaks = []
        self.mapchanges = []
        self.splits = 0
        self.outbound = 0
        self.replay_bits = []

    def on_outbound(self, packet):
        self.outbound += 1

    def on_split(self, datagram):
        self.splits += 1

    def on_signon(self, state):
        self.signons.append(state)

    def on_break(self, cause, detail):
        self.breaks.append((cause, detail))

    def on_mapchange(self, map_name):
        self.mapchanges.append(map_name)

    def on_replay_bit(self, flag):
        self.replay_bits.append(flag)


def make_session():
    return sess.Session(FakeConn(), supervisor.Options(crc=0xD9B6082D), FakeSink())


def inbound(session, seq, unreliable=b"", reliable_region=b"", relstate=0):
    """A valid inbound datagram, checksum and all, addressed at this session's
    challenge. Built with the same netchan the client uses, so the test cannot
    drift from the wire format."""
    return netchan.build_packet(
        seq,
        1,
        session.conn.challenge,
        relstate,
        reliable_region=reliable_region,
        unreliable=unreliable,
    )


# --- signon ladder ---------------------------------------------------------


def test_forward_progress_emits_each_state_once():
    s = make_session()
    s.spawncount = 7  # as if svc_ServerInfo already latched
    for state in (wire.SIGNON_NEW, wire.SIGNON_PRESPAWN, wire.SIGNON_SPAWN):
        s._advance(state)
    assert s.sink.signons == [wire.SIGNON_NEW, wire.SIGNON_PRESPAWN, wire.SIGNON_SPAWN]
    assert s.state == wire.SIGNON_SPAWN


def test_state_never_moves_backward_on_a_lower_push():
    s = make_session()
    s.spawncount = 7
    s._advance(wire.SIGNON_SPAWN)
    s.sink.signons.clear()
    s._advance(wire.SIGNON_NEW)  # a stale lower push
    assert s.state == wire.SIGNON_SPAWN  # unchanged
    assert s.sink.signons == []  # no spurious on_signon


def test_changelevel_push_breaks_the_session():
    s = make_session()
    s.spawncount = 7
    s._advance(wire.SIGNON_CHANGELEVEL)
    assert not s.alive
    assert s.broke[0] == sess.BREAK_CHANGELEVEL


def test_regression_to_connected_after_full_breaks():
    s = make_session()
    s.spawncount = 7
    s.state = wire.SIGNON_FULL
    s._advance(wire.SIGNON_CONNECTED)  # relay yanked us back = map change
    assert not s.alive
    assert s.broke[0] == sess.BREAK_CHANGELEVEL
    assert "regressed" in s.broke[1]


def test_echo_is_keyed_by_spawncount_and_state():
    s = make_session()
    s.spawncount = 7
    s._advance(wire.SIGNON_PRESPAWN)  # sends one reliable echo
    assert s.pending is not None
    s.pending = None  # pretend it was acked
    s._advance(wire.SIGNON_PRESPAWN)  # same (spawncount,state) again
    assert s.pending is None  # NOT re-echoed


def test_new_spawncount_rearms_the_echo():
    s = make_session()
    s.spawncount = 7
    s._advance(wire.SIGNON_PRESPAWN)
    s.pending = None
    s.echoed.clear()  # a new spawncount clears this (below)
    s.spawncount = 8
    s._advance(wire.SIGNON_PRESPAWN)
    assert s.pending is not None  # re-echoed under the new count


# --- map change (single-emission fold) -------------------------------------


def _serverinfo(spawncount, map_name):
    return {"protocol": wire.PROTOCOL_VERSION, "spawncount": spawncount, "map": map_name}


def test_first_serverinfo_is_not_a_mapchange():
    s = make_session()
    s._handle_message(wire.SVC_SERVERINFO, _serverinfo(7, "dm_lockdown"))
    assert s.sink.mapchanges == []  # first latch, nothing changed yet
    assert s.spawncount == 7 and s.map_name == "dm_lockdown"


def test_real_mapchange_emits_exactly_one_event():
    s = make_session()
    s._handle_message(wire.SVC_SERVERINFO, _serverinfo(7, "dm_lockdown"))
    s._handle_message(wire.SVC_SERVERINFO, _serverinfo(8, "dm_overwatch"))
    assert s.sink.mapchanges == ["dm_overwatch"]  # one, not two


def test_repeated_identical_serverinfo_is_not_a_mapchange():
    s = make_session()
    s._handle_message(wire.SVC_SERVERINFO, _serverinfo(7, "dm_lockdown"))
    s._handle_message(wire.SVC_SERVERINFO, _serverinfo(7, "dm_lockdown"))
    assert s.sink.mapchanges == []


def test_spawncount_change_clears_echo_guards():
    s = make_session()
    s.spawncount = 7
    s.echoed.add((7, wire.SIGNON_PRESPAWN))
    s._handle_message(wire.SVC_SERVERINFO, _serverinfo(8, "dm_overwatch"))
    assert s.echoed == set()  # stale echoes dropped for the new count


# --- breakers (injected clock) ---------------------------------------------


def test_silence_breaker_uses_the_signon_limit_below_full():
    s = make_session()
    t0 = s.last_inbound
    s.poll(now=t0 + s.opt.silence_signon_s - 0.1)  # just under
    assert s.alive
    s.poll(now=t0 + s.opt.silence_signon_s + 0.1)  # just over
    assert not s.alive
    assert s.broke[0] == sess.BREAK_SILENCE


def test_silence_breaker_uses_the_looser_full_limit_after_full():
    s = make_session()
    s.state = wire.SIGNON_FULL
    t0 = s.last_inbound
    # Between the two limits: would have broken during signon, tolerated at FULL.
    s.poll(now=t0 + s.opt.silence_signon_s + 0.1)
    assert s.alive
    s.poll(now=t0 + s.opt.silence_full_s + 0.1)
    assert not s.alive
    assert s.broke[0] == sess.BREAK_SILENCE


def test_signon_stall_fires_on_no_progress_while_packets_arrive():
    s = make_session()
    s.spawncount = 7
    s._advance(wire.SIGNON_NEW)
    # The first poll after a state change syncs the progress axis and resets its
    # clock IN THAT SAME call, so a stall can only accrue from a later poll.
    s.last_inbound = s.started  # keep silence quiet by refreshing below
    s.poll(now=s.started)  # baseline: progress NONE->NEW recorded
    now = s.started + s.opt.signon_deadline_s + 1
    s.last_inbound = now  # packets ARE arriving: not silence
    s.poll(now=now)
    assert not s.alive
    assert s.broke[0] == sess.BREAK_SIGNON_STALL


def test_ladder_progress_resets_the_stall_clock():
    s = make_session()
    s.spawncount = 7
    s._advance(wire.SIGNON_NEW)
    mid = s.started + s.opt.signon_deadline_s - 1
    s.last_inbound = mid
    s.poll(now=mid)  # progress axis still moving? no, but under
    assert s.alive
    s._advance(wire.SIGNON_PRESPAWN)  # progress -> resets _last_progress_at
    later = mid + s.opt.signon_deadline_s - 1
    s.last_inbound = later
    s.poll(now=later)  # would have stalled without the reset
    assert s.alive


def test_reliable_dead_breaker_fires_when_acks_stop_advancing():
    s = make_session()
    now = s.started + s.opt.reliable_dead_s + 1
    # Reliable packets keep arriving (seen recent) but none has completed for
    # > reliable_dead_s. Keep last_inbound fresh so the silence breaker, which is
    # checked first, does not preempt this one.
    s.last_inbound = now
    s._reliable_seen_at = now
    s._reliable_done_at = s.started  # last acceptance long ago
    s.poll(now=now)
    assert not s.alive
    assert s.broke[0] == sess.BREAK_RELIABLE_DEAD


def test_full_self_promotion_requires_a_server_tick():
    s = make_session()
    s.spawncount = 7
    s.state = wire.SIGNON_SPAWN
    s.poll(now=s.started + 1)  # no tick yet
    assert s.state == wire.SIGNON_SPAWN
    s.server_tick = 42
    s.poll(now=s.started + 2)
    assert s.state == wire.SIGNON_FULL
    assert wire.SIGNON_FULL in s.sink.signons


# --- feed(): the unreliable-blindness fix ----------------------------------


def test_unreliable_disconnect_is_seen_not_mistaken_for_silence():
    """The exact bug the fix exists for: a relay refusing us with an UNRELIABLE
    net_Disconnect must break with cause 'disconnect', not sit until the silence
    breaker fires."""
    s = make_session()
    w = wire.BitWriter()
    w.write_ubit(wire.NET_DISCONNECT, wire.NETMSG_TYPE_BITS)
    w.write_string("Bad spectator password")
    s.feed(inbound(s, seq=5, unreliable=w.get_bytes()))
    assert not s.alive
    assert s.broke[0] == sess.BREAK_DISCONNECT
    assert "password" in s.broke[1]


def test_feed_never_raises_on_garbage():
    s = make_session()
    for junk in (b"", b"\x00", b"\xff" * 3, b"\xde\xad\xbe\xef" * 4, os.urandom(64)):
        s.feed(junk)  # must not raise
    assert s.alive  # garbage costs a packet, not the session


def test_stale_and_duplicate_sequences_are_dropped():
    s = make_session()
    s.feed(inbound(s, seq=10))
    assert s.in_seq == 10
    s.feed(inbound(s, seq=10))  # duplicate
    s.feed(inbound(s, seq=3))  # stale
    assert s.in_seq == 10  # unmoved
    assert s.counters.get("stale_sequence", 0) >= 2


def test_split_datagram_with_bad_header_is_recorded_not_parsed():
    s = make_session()
    split = struct.pack("<i", wire.MARK_SPLIT) + b"\x00" * 12
    s.feed(split)
    assert s.sink.splits == 1
    assert s.alive


def _split(packet, group, size):
    parts = [packet[i : i + size] for i in range(0, len(packet), size)]
    return [
        struct.pack("<iiBBH", wire.MARK_SPLIT, group, len(parts), n, size) + p
        for n, p in enumerate(parts)
    ]


def test_split_parts_are_joined_and_parsed_in_any_order():
    """A packet sent as -2 parts is a normal packet once joined: its sequence
    is taken and its unreliable messages applied. Parts may arrive reordered
    and duplicated; the group completes once, on the last missing part."""
    s = make_session()
    w = wire.BitWriter()
    w.write_ubit(wire.NET_DISCONNECT, wire.NETMSG_TYPE_BITS)
    w.write_string("x" * 300)
    parts = _split(inbound(s, seq=9, unreliable=w.get_bytes()), 7, 120)
    assert len(parts) == 3
    for p in (parts[2], parts[0], parts[0]):
        s.feed(p)
    assert s.in_seq == 0 and s.alive
    s.feed(parts[1])
    assert s.in_seq == 9
    assert s.broke[0] == sess.BREAK_DISCONNECT


@pytest.mark.parametrize("count, outcome", [(8, "joined"), (9, [sp.OVERSIZE] * 9)])
def test_a_split_group_is_judged_by_its_declared_size_before_a_part_is_kept(count, outcome):
    """Parts of an eighth of the cap: eight make the cap and are joined; nine
    declare more, and no part of them is held or joined."""
    r = sp.SplitReassembler()
    size = sp.MAX_JOINED // 8
    head = struct.Struct("<iiBBH")
    got = [
        r.feed(head.pack(wire.MARK_SPLIT, 1, count, n, size) + bytes(size), 0, n)
        for n in range(count)
    ]
    held = sum(len(p) for g in r.open.values() for _i, p in g.parts.values())
    done = "joined" if got[-1] is not None else [r.fates.get(n) for n in range(count)]
    assert (done, held) == (outcome, 0)


def test_open_split_groups_are_capped_and_the_oldest_gives_way():
    r = sp.SplitReassembler()
    head = struct.Struct("<iiBBH")
    for gid in range(sp.MAX_OPEN + 1):
        r.feed(head.pack(wire.MARK_SPLIT, gid, 2, 0, 100) + bytes(100), gid, gid)
    assert (sorted(r.open), r.fates) == (list(range(1, sp.MAX_OPEN + 1)), {0: sp.EVICTED})


def test_ones_padded_tail_after_unaligned_reliable_is_a_clean_end():
    """The sender pads the packet's last byte with 1 bits. When the unreliable
    stream starts mid-byte, walking a byte-rounded copy of it turns those pad
    bits into a phantom message, the tail is judged dirty and its net_Tick is
    discarded."""
    s = make_session()
    w = wire.BitWriter()
    body = messages.signonstate_body(wire.SIGNON_PRESPAWN, 7)
    w.write_ubit(0, wire.SUBCHANNEL_BITS)
    w.write_one_bit(1)
    w.write_one_bit(0)
    w.write_one_bit(0)
    w.write_varint32(len(body))
    w.write_bytes(body)
    w.write_one_bit(0)
    w.write_ubit(wire.NET_TICK, wire.NETMSG_TYPE_BITS)
    w.write_long(4242)
    w.write_ubit(0, 16)
    w.write_ubit(0, 16)
    pad = (-w.nbits()) % 8
    assert 0 < pad < wire.NETMSG_TYPE_BITS
    w.write_ubit((1 << pad) - 1, pad)
    s.feed(inbound(s, seq=5, reliable_region=w.get_bytes()))
    assert s.counters.get("unreliable_chain_stop", 0) == 0
    assert s.server_tick == 4242


def test_reliable_without_subchannel_index_walks_no_tail():
    """A reliable packet whose body is too short to latch a subchannel has no
    known tail start: nothing of it may be walked (a walk from a guessed
    offset reads garbage as messages), and the bit is not flipped."""
    s = make_session()
    body = messages.signonstate_body(wire.SIGNON_PRESPAWN, 7)
    region = messages.reliable_region(body, subchannel=0)
    s.feed(inbound(s, seq=5, reliable_region=region))
    state = s.in_reliable_state
    # RELIABLE set but no region: the flags byte is at offset 8, before the
    # checksummed region, so poking it keeps the checksum valid.
    forced = bytearray(netchan.build_packet(7, 1, s.conn.challenge, 0))
    forced[8] |= wire.FLAG_RELIABLE
    pkt = s.rx.feed(bytes(forced), 0)
    assert pkt.reliable_error == "no_subchannel_index" and pkt.walks == []
    forced = bytearray(netchan.build_packet(8, 1, s.conn.challenge, 0))
    forced[8] |= wire.FLAG_RELIABLE
    s.feed(bytes(forced))
    assert s.counters.get("no_subchannel_index") == 1
    assert s.in_reliable_state == state and s.alive


def unseen_continuation_region(subchannel=2):
    """A multi-fragment block that starts at fragment 1: a continuation of a
    transfer this receiver never saw begin. Unparseable by definition."""
    w = wire.BitWriter()
    w.write_ubit(subchannel, wire.SUBCHANNEL_BITS)
    w.write_one_bit(1)  # stream 0 follows
    w.write_one_bit(1)  # multi
    w.write_ubit(1, 18)  # start fragment 1
    w.write_ubit(1, 3)
    w.write_bytes(b"\x00" * 256)
    w.write_one_bit(0)  # stream 1 empty
    return w.get_bytes()


def test_unparseable_reliable_packet_is_not_acked():
    """CNetChan::ProcessPacket returns before FLIPBIT when ReadSubChannelData
    fails: acking it would tell the relay the block arrived and lose it."""
    s = make_session()
    s.feed(inbound(s, seq=5, reliable_region=unseen_continuation_region()))
    assert s.counters.get("reliable_parse_error") == 1
    assert s.in_reliable_state == 0
    ack = netchan.decode_header(s.conn.sock.sent[-1])
    assert ack.sequence_ack == 5 and ack.reliable_state == 0


def test_our_reliable_block_is_done_only_when_the_relay_flips_its_bit():
    """The CONNECTED reply goes out on subchannel 0 and flips our bit 0: a
    relay packet still showing bit 0 unflipped leaves it pending (and it is
    resent), one showing it flipped acks it."""
    s = make_session()
    s.send_connected_reply()
    sent = len(s.conn.sock.sent)
    s.feed(inbound(s, 1, relstate=0))
    assert s.pending is not None
    s.poll(now=s.pending_sent_at + s.opt.reliable_resend_s + 0.01)
    assert s.counters.get("reliable_resend") == 1 and len(s.conn.sock.sent) > sent + 1
    s.feed(inbound(s, 2, relstate=1))
    assert s.pending is None
