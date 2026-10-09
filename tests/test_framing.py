"""The receive path offline: datagrams of a recording -> packets -> walked
messages for the hooks. What a message after a voice message, a split packet,
a resend or a reconnect must look like to the game event decoder."""

import os
import threading
import time
from types import SimpleNamespace

from helpers import (
    CHALLENGE,
    body,
    chat,
    chat_bytes,
    ones_pad,
    packet,
    reliable_packet,
    split,
    steamid64,
    table_update,
    usermessage,
    voice_payload,
    write_recording,
    write_voice,
)

from stvwatch.net import dump, messages, netchan, wire
from stvwatch.stream import streamevents as se
from stvwatch.stream.framing import Framer, frame
from stvwatch.stream.recording import Datagram, Recording
from stvwatch.stream.userinfo import STEAMID64_BASE, NickBook, StringTables

A, B = steamid64(1), steamid64(2)
X = messages.signonstate_body(wire.SIGNON_SPAWN, 3)


def chats_of(fr_kw=None):
    """A Framer whose Collector keeps the chat texts it decodes, in order."""
    got = []
    col = se.Collector(lambda r: got.append(r["f"]["params"][1]) if r["name"] == "SayText2" else 0)
    fr = Framer(on_msg=col, **(fr_kw or {}))
    col.framer = fr
    return fr, got


def chat_tail(text):
    return lambda w: chat(w, "nick", text)


def ack(out_seq, seq, bits=0, challenge=CHALLENGE):
    return netchan.build_packet(out_seq, seq, challenge, bits)


def synthetic_datagrams():
    """Two connections. Chat after voice in plain packets, in a split packet
    (parts reordered, one duplicated), behind an unaligned reliable region."""
    out = [packet(1, [(1, voice_payload(A))], tail=chat_tail("one"))]
    big = packet(2, [(2, voice_payload(B, n=12))], tail=chat_tail("two"))
    parts = split(big, group=5)
    assert len(parts) >= 3
    out += [parts[1], parts[0], parts[0]] + parts[2:]
    out.append(packet(3, [(1, voice_payload(A))], reliable_first=True, tail=chat_tail("three")))
    # new connection: sequences restart, challenge differs
    out.append(packet(1, [(1, voice_payload(A))], challenge=0x55667788, tail=chat_tail("four")))
    return out


def synth(tmp_path, name="synth.tvd"):
    p = str(tmp_path / name)
    write_recording(p, [(T + i * 15_000_000, d) for i, d in enumerate(synthetic_datagrams())])
    return p


T = 1_000_000_000_000


def test_split_padded_and_reconnected_traffic_all_recovered(tmp_path):
    fr, got = chats_of()
    joined = []
    fr.on_packet = lambda p: joined.append(p.via_split)
    for dg in Recording(synth(tmp_path)):
        fr.feed(dg)
    fr.finish()
    assert got == ["one", "two", "three", "four"]
    assert joined == [False, True, False, False]
    assert fr.counters["challenge_change"] == 1
    assert fr.splits.counters["split_duplicate"] == 1
    assert {f.fate for f in fr.fates} == {"ok"}


def test_a_client_without_split_support_loses_exactly_the_split_packet(tmp_path):
    fr, got = chats_of({"reassemble_splits": False})
    for dg in Recording(synth(tmp_path)):
        fr.feed(dg)
    assert got == ["one", "three", "four"]


def test_voice_is_skipped_by_its_length_and_what_follows_stays_aligned():
    """Voice messages of odd sizes at an unaligned start, then a chat line:
    the chat decodes. A voice message whose length runs past the packet stops
    the walk there, by name, and nothing after it is invented."""
    fr, got = chats_of()
    mids = []
    hook = fr.on_msg
    fr.on_msg = lambda mid, *a: (mids.append(mid), hook(mid, *a))
    w = wire.BitWriter()
    for n in (1, 7, 30):
        write_voice(w, 1, voice_payload(A)[: n + 8])
    chat(w, "nick", "after voice")
    ones_pad(w)
    fr.feed(Datagram(0, 0, 0, netchan.build_packet(4, 1, CHALLENGE, 0, unreliable=w.get_bytes())))
    fr.feed(
        Datagram(
            1, 1, 0, packet(5, [(1, voice_payload(A))], reliable_first=True, tail=chat_tail("x"))
        )
    )
    assert got == ["after voice", "x"]
    assert [f.fate for f in fr.fates] == ["ok", "ok"]
    assert mids and 15 not in mids  # voice never reaches the game message hook
    w = wire.BitWriter()
    w.write_ubit(15, wire.NETMSG_TYPE_BITS)
    w.write_byte(1)
    w.write_byte(0)
    w.write_ubit(4000, 16)  # declared 4000 bits, carries far fewer
    w.write_bytes(b"\x00" * 40)
    chat(w, "nick", "lost")
    ones_pad(w)
    fr.feed(Datagram(2, 2, 0, netchan.build_packet(6, 1, CHALLENGE, 0, unreliable=w.get_bytes())))
    assert got == ["after voice", "x"]
    assert fr.fates[-1].fate == "unreliable_stop:id15:voice past end"


def test_following_a_growing_journal_equals_reading_it_finished(tmp_path):
    """Live source = the same reader tailing a journal that is still being
    written; what the hooks get must match the finished file."""
    grown = str(tmp_path / "grown.tvd")
    data = synthetic_datagrams()
    done = threading.Event()

    def writer():
        with dump.DumpWriter(grown, "127.0.0.1:27020", fsync_ms=0) as w:
            for i, d in enumerate(data):
                w.write(dump.DATAGRAM_IN, d, t_ns=T + i * 15_000_000)
                time.sleep(0.01)
        done.set()

    threading.Thread(target=writer, daemon=True).start()
    deadline = time.monotonic() + 5
    while (not os.path.exists(grown) or os.path.getsize(grown) < 30) and (
        time.monotonic() < deadline
    ):
        time.sleep(0.005)
    live, got_live = chats_of()
    for _ in frame(Recording(grown, follow=True, poll_s=0.005, idle_s=0.5, stop=done.is_set), live):
        pass
    done.wait(5)
    replay, got_replay = chats_of()
    for _ in frame(Recording(synth(tmp_path)), replay):
        pass
    assert got_live == got_replay == ["one", "two", "three", "four"]
    assert [f.fate for f in live.fates] == [f.fate for f in replay.fates]


# --- resends of unacked reliable packets ------------------------------------


def test_resend_is_recognized_by_our_acks_and_its_tail_still_read():
    """A reliable packet our ack did not flip comes again on the same
    subchannel with the same region: skipped (also when unacked again), its
    unreliable tail read; a different region on that subchannel is new data."""
    y = messages.signonstate_body(wire.SIGNON_PRESPAWN, 4)
    streams = []
    fr, got = chats_of()
    fr.on_packet = lambda p: streams.extend(w.payload for w in p.walks if w.stream == "reliable")
    fr.observe(0, 2, ack(1, 0))
    for i, (seq, b) in enumerate([(10, X), (11, X), (12, X), (13, y)]):
        w = wire.BitWriter()
        w.write_ubit(0, wire.SUBCHANNEL_BITS)
        w.write_one_bit(1)
        w.write_one_bit(0)
        w.write_one_bit(0)
        w.write_varint32(len(b))
        w.write_bytes(b)
        w.write_one_bit(0)
        chat(w, "nick", f"tail {i}")
        ones_pad(w)
        fr.feed(
            Datagram(
                i, i, 0, netchan.build_packet(seq, 1, CHALLENGE, 0, reliable_region=w.get_bytes())
            )
        )
        fr.observe(0, 2, ack(2 + i, seq))  # never flipped: the live client lost it
    assert streams == [X, y]
    assert got == ["tail 0", "tail 1", "tail 2", "tail 3"]
    assert fr.counters["resend_skipped"] == 2 and fr.counters["resend_mismatch"] == 1
    assert [f.fate for f in fr.fates] == ["ok", "resend", "resend", "ok"]
    assert fr.counters["ack_unflipped"] == 4  # every copy, resends too


def test_flipped_packet_is_never_a_resend():
    fr = Framer()
    fr.observe(0, 2, ack(1, 0))
    fr.feed(Datagram(0, 0, 0, reliable_packet(10, X)))
    fr.observe(0, 2, ack(2, 10, bits=1))  # flipped
    fr.feed(Datagram(1, 1, 0, reliable_packet(11, X)))
    assert [f.fate for f in fr.fates] == ["ok", "ok"]
    assert fr.counters["ack_flipped"] == 1 and not fr.counters["resend_skipped"]


def test_same_region_in_a_new_session_is_new_data(tmp_path):
    """An unflipped packet at the end of session 1 and a bit-equal region on
    the same subchannel first thing in session 2: a reconnect drops the
    sender's queue, so this is not a resend."""
    p = str(tmp_path / "two.tvd")
    with dump.DumpWriter(p, "127.0.0.1:27020", fsync_ms=0) as w:
        w.event(dump.SESSION_START, session=1)
        w.write(dump.DATAGRAM_OUT, ack(1, 0), t_ns=T)
        w.write(dump.DATAGRAM_IN, reliable_packet(10, X), t_ns=T + 1)
        w.write(dump.DATAGRAM_OUT, ack(2, 10), t_ns=T + 2)  # unflipped
        w.event(dump.SESSION_START, session=2)
        w.write(dump.DATAGRAM_OUT, ack(1, 0), t_ns=T + 3)
        w.write(dump.DATAGRAM_IN, reliable_packet(11, X), t_ns=T + 4)
        w.write(dump.DATAGRAM_OUT, ack(2, 11, bits=1), t_ns=T + 5)
    fr = Framer()
    for _ in frame(Recording(p), fr):
        pass
    fr.finish()
    assert [f.fate for f in fr.fates] == ["ok", "ok"]
    assert not fr.counters["resend_skipped"]


def resent_first_packet_of_session_2(path):
    """Session 2 opens with our connected reply (written after SESSION_START,
    before any inbound datagram); its first reliable packet, a chat, is not
    flipped by our ack and comes again."""
    c2 = 0x55667788
    hello = [reliable_packet(n, chat_bytes("nick", "hello"), challenge=c2) for n in (1, 2)]
    with dump.DumpWriter(path, "127.0.0.1:27020", fsync_ms=0) as w:
        for t, rtype, data in [
            (T, dump.SESSION_START, b'{"session": 1}'),
            (T + 1, dump.DATAGRAM_OUT, ack(1, 0)),
            (T + 2, dump.DATAGRAM_IN, reliable_packet(10, X)),
            (T + 3, dump.DATAGRAM_OUT, ack(2, 10, bits=1)),
            (T + 4, dump.SESSION_START, b'{"session": 2}'),
            (T + 5, dump.DATAGRAM_OUT, ack(1, 0, challenge=c2)),
            (T + 6, dump.DATAGRAM_IN, hello[0]),
            (T + 7, dump.DATAGRAM_OUT, ack(2, 1, challenge=c2)),  # unflipped
            (T + 8, dump.DATAGRAM_IN, hello[1]),
            (T + 9, dump.DATAGRAM_OUT, ack(3, 2, bits=1, challenge=c2)),
        ]:
            w.write(rtype, data, t_ns=t)


def test_first_packet_of_a_new_session_is_judged_by_its_connected_reply(tmp_path):
    p = str(tmp_path / "two.tvd")
    resent_first_packet_of_session_2(p)
    fr, got = chats_of()
    for _ in frame(Recording(p), fr):
        pass
    assert got == ["hello"]


def test_challenge_change_without_markers_or_our_packets_also_resets():
    """An .hcap holds the relay's datagrams only: a new challenge is the only
    sign of a new connection, whose sequence starts over."""
    fr = Framer()
    fr.feed(Datagram(0, 0, 0, reliable_packet(10, X)))
    fr.feed(Datagram(1, 1, 0, reliable_packet(2, X, challenge=0x55667788)))
    assert [f.fate for f in fr.fates] == ["ok", "ok"]


def test_a_packet_under_a_challenge_our_packets_do_not_carry_touches_no_state():
    """As in the live session: once our own packets name the connection, a
    datagram under another challenge is not the relay's, whatever its
    sequence (the live client dropped it; the dump still holds it)."""
    fr = Framer()
    fr.observe(0, 2, ack(1, 0))
    fr.feed(Datagram(0, 0, 0, reliable_packet(10, X)))
    fr.feed(Datagram(1, 1, 0, packet(1_000_000, challenge=0x55667788)))
    fr.feed(Datagram(2, 2, 0, reliable_packet(11, X, sub=1)))
    assert [f.fate for f in fr.fates] == ["ok", "challenge_mismatch", "ok"]


def test_resend_lost_in_split_then_resent_plain_is_still_skipped():
    """First copy plain and parsed, unflipped; the first resend arrives as -2
    with a part missing (never completes); the next resend is plain: it is
    still the same region and must be skipped, its tail read."""
    fr, got = chats_of()
    fr.observe(0, 2, ack(1, 0))
    fr.feed(Datagram(0, 0, 0, reliable_packet(10, X, [(1, voice_payload(A))])))
    fr.observe(0, 2, ack(2, 10))
    big = reliable_packet(11, X, [(1, voice_payload(A, n=12))])
    parts = split(big, group=7)
    for i, part in enumerate(parts[:-1]):
        fr.feed(Datagram(1 + i, 1 + i, 0, part))
    fr.observe(0, 2, ack(3, 10))
    fr.feed(Datagram(9, 7_000_000_000, 0, reliable_packet(12, X, [(1, voice_payload(A))])))
    fr.finish()
    assert [f.fate for f in fr.fates] == ["ok", "resend"]
    assert fr.splits.counters["groups_split_orphan_timeout"] == 1


def test_unrecorded_ack_degrades_to_reading_twice_not_to_loss():
    """Our ack for the first copy is missing from the recording: the resend
    cannot be told apart and is read again: duplicates, never a loss."""
    fr = Framer()
    fr.observe(0, 2, ack(1, 0))
    fr.feed(Datagram(0, 0, 0, reliable_packet(10, X)))
    fr.feed(Datagram(1, 1, 0, reliable_packet(11, X)))
    assert [f.fate for f in fr.fates] == ["ok", "ok"]


def test_recording_cut_inside_a_split_group(tmp_path):
    """Killed mid-write: the last record is cut inside a -2 part. No raise,
    the open group is an orphan at the end, the truncation is reported."""
    p = str(tmp_path / "cut.tvd")
    big = reliable_packet(11, X, [(1, voice_payload(A, n=12))])
    parts = split(big, group=9)
    with dump.DumpWriter(p, "127.0.0.1:27020", fsync_ms=0) as w:
        w.write(dump.DATAGRAM_OUT, ack(1, 0), t_ns=T)
        w.write(dump.DATAGRAM_IN, reliable_packet(10, X), t_ns=T + 1)
        w.write(dump.DATAGRAM_OUT, ack(2, 10, bits=1), t_ns=T + 2)
        for i, part in enumerate(parts):
            w.write(dump.DATAGRAM_IN, part, t_ns=T + 3 + i)
    with open(p, "r+b") as f:
        f.truncate(os.path.getsize(p) - len(parts[-1]) // 2)
    rec = Recording(p)
    fr = Framer()
    for _ in frame(rec, fr):
        pass
    fr.finish()
    assert rec.truncated_tail
    assert fr.counters["ack_unflipped"] == 0 and fr.counters["ack_flipped"] == 1
    assert fr.splits.counters["groups_split_orphan_eof"] == 1


def test_each_delivered_packet_is_judged_once():
    """Every delivered reliable packet gets exactly one verdict, from the
    first ack covering it, however many acks follow."""
    fr = Framer()
    fr.observe(0, 2, ack(1, 0))
    fr.feed(Datagram(0, 0, 0, reliable_packet(10, X, sub=0)))
    fr.feed(Datagram(1, 1, 0, reliable_packet(11, X, sub=1)))
    for k in range(5):
        fr.observe(0, 2, ack(2 + k, 11, bits=0b01))  # sub 0 flipped, sub 1 not
    assert fr.counters["ack_flipped"] == 1 and fr.counters["ack_unflipped"] == 1


def test_handshake_records_in_a_recording_are_not_acks():
    """getchallenge/connect are recorded as DATAGRAM_OUT too; they carry no
    ack and must not be read as one."""
    from stvwatch.net import handshake

    fr = Framer()
    fr.observe(0, 2, ack(1, 0))
    fr.feed(Datagram(0, 0, 0, reliable_packet(10, X)))
    fr.observe(0, 2, handshake.build_getchallenge(0x12345678))
    assert fr.counters["ack_unflipped"] == 0 and fr.counters["ack_flipped"] == 0


def test_unparseable_reliable_region_leaves_the_tail_unread():
    """After a reliable region that does not parse, where the unreliable
    stream starts is unknown: nothing of the packet may be walked."""
    w = wire.BitWriter()
    w.write_ubit(0, wire.SUBCHANNEL_BITS)
    w.write_one_bit(1)
    w.write_one_bit(1)  # multi, continuation of an unseen transfer
    w.write_ubit(1, 18)
    w.write_ubit(1, 3)
    w.write_bytes(b"\x00" * 256)
    w.write_one_bit(0)
    chat(w, "nick", "never")
    ones_pad(w)
    walks = []
    fr, got = chats_of()
    fr.on_packet = lambda p: walks.extend(p.walks)
    fr.feed(
        Datagram(0, 0, 0, netchan.build_packet(9, 1, CHALLENGE, 0, reliable_region=w.get_bytes()))
    )
    assert [f.fate for f in fr.fates] == ["reliable_error:unseen_transfer"]
    assert walks == [] and got == []


# --- hooks ---------------------------------------------------------------------


def test_framer_hands_string_tables_to_the_table_hook():
    tables, book = StringTables(), NickBook()
    fr = Framer(on_table=lambda *m: book.update(tables.feed(*m)[1]))
    acc = A - STEAMID64_BASE
    fr.feed(
        Datagram(
            0,
            0,
            0,
            netchan.build_packet(
                3,
                1,
                CHALLENGE,
                0,
                unreliable=table_update(("nick", f"[U:1:{acc}]", acc), create=True),
            ),
        )
    )
    assert [f.fate for f in fr.fates] == ["ok"]
    assert book.nick(A) == "nick"


def test_chain_decodes_text_events_and_flags_bad_length():
    got = []
    col = se.Collector(got.append)
    fr = Framer(on_msg=col)
    col.framer = fr
    w = wire.BitWriter()
    # SayText2 from entity 3, two of four params filled.
    chat(w, "nick", "hello \u00e9", ent=3)
    # Event list: id 7 player_death(userid short, attacker short, weapon string),
    # id 9 hltv_status(clients long, master string).
    lst = wire.BitWriter()
    for eid, name, keys in (
        (7, "player_death", ((4, "userid"), (4, "attacker"), (1, "weapon"))),
        (9, "hltv_status", ((3, "clients"), (1, "master"))),
    ):
        lst.write_ubit(eid, 9)
        lst.write_string(name)
        for t, k in keys:
            lst.write_ubit(t, 3)
            lst.write_string(k)
        lst.write_ubit(0, 3)
    w.write_ubit(se.SVC_GAMEEVENTLIST, wire.NETMSG_TYPE_BITS)
    w.write_ubit(2, 9)
    w.write_ubit(lst.nbits(), 20)
    for i in range(lst.nbits()):  # bit-exact copy, no byte padding
        w.write_one_bit(lst.get_bytes()[i >> 3] >> (i & 7))
    ev = body(
        lambda b: (
            b.write_ubit(7, 9),
            b.write_ubit(5, 16),
            b.write_ubit(0xFFFF, 16),
            b.write_string("357"),
        )
    )
    w.write_ubit(se.SVC_GAMEEVENT, wire.NETMSG_TYPE_BITS)
    w.write_ubit(ev.nbits(), 11)
    for i in range(ev.nbits()):
        w.write_one_bit(ev.get_bytes()[i >> 3] >> (i & 7))
    # SayText whose declared length is one byte longer than its body.
    st = body(lambda b: (b.write_byte(0), b.write_string("x"), b.write_byte(1)))
    usermessage(w, se.USER_MESSAGES.index("SayText"), st, st.nbits() + 8)
    fr.feed(
        Datagram(0, 1, 0, netchan.build_packet(5, 1, 0x1234, 0, unreliable=w.get_bytes() + b"\x00"))
    )
    assert [f.fate for f in fr.fates] in (["ok"], ["unreliable_stop:id0:EOFError"])
    assert got and all(r["t_ns"] == 1 for r in got)
    assert [(r["kind"], r["name"]) for r in got] == [
        ("usermsg", "SayText2"),
        ("event", "player_death"),
    ]
    assert got[0]["f"] == {
        "ent": 3,
        "chat": 1,
        "fmt": "HL2MP_Chat_All",
        "params": ["nick", "hello \u00e9", "", ""],
    }
    assert got[1]["f"] == {"userid": 5, "attacker": -1, "weapon": "357"}
    assert col.event_lists == 1 and col.events_unknown_id == 0
    assert dict(col.usermsg_misparse) == {"SayText": 1}
    assert col.chat["SayText2:HL2MP_Chat_All"] == 1


def test_event_before_list_is_unknown_not_guessed():
    col = se.Collector()
    col.framer = SimpleNamespace(cur_t_ns=0, cur_session=0, tick=0)
    ev = body(lambda b: (b.write_ubit(7, 9), b.write_ubit(5, 16)))
    w = wire.BitWriter()
    w.write_ubit(ev.nbits(), 11)
    for i in range(ev.nbits()):
        w.write_one_bit(ev.get_bytes()[i >> 3] >> (i & 7))
    col(se.SVC_GAMEEVENT, w.get_bytes(), 0, w.nbits())
    assert col.events_unknown_id == 1 and not col.events


def test_chat_bytes_helper_decodes_through_a_reliable_stream():
    fr, got = chats_of()
    fr.feed(Datagram(0, 0, 0, reliable_packet(3, chat_bytes("nick", "reliable"))))
    assert got == ["reliable"]
