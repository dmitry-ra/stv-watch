"""The userinfo string table, decoded entry by entry: every branch of the
entry encoding, table numbering and reset, and a cut of a real relay stream."""

import os
import struct

import pytest
from helpers import (
    account,
    lzss,
    player_info,
    userinfo_entries,
    write_create,
    write_entries,
    write_update,
)

from stvwatch.net import wire
from stvwatch.stream import userinfo
from stvwatch.stream.framing import Framer, frame
from stvwatch.stream.recording import Recording
from stvwatch.stream.userinfo import NickBook, Player, StringTables, Table

HERE = os.path.dirname(os.path.abspath(__file__))


def feed(tables, w, session=1):
    data = w.get_bytes() + b"\0"
    return tables.feed(data, wire.NETMSG_TYPE_BITS, w.nbits(), session)


def msg(write, *args, **kw):
    w = wire.BitWriter()
    write(w, *args, **kw)
    return w


def u(acc):
    return f"[U:1:{acc}]"


ALICE = player_info("alice", u(account(1)), account(1), 11)
BOB = player_info("bob", u(account(2)), account(2), 12)
# a bot whose server made up a friendsID for it
BOT = player_info("Sniper", "BOT", 777, 13)


def test_entries_decode_in_every_encoding_of_index_key_and_user_data():
    """Create: consecutive indices, plain keys and keys built on a previous
    one (prefix + suffix), user data absent or sized. Update: indices out of
    order, unchanged keys, absent user data empties a slot."""
    tables = StringTables()
    create = [
        (0, b"slot0", None),
        (1, (0, 4, b"1"), ALICE),  # "slot" from entry 0 + "1"
        (2, (1, 5, b"2"), BOT),  # history holds the built key "slot1"
        (3, b"slot3", BOB),
    ]
    mid, got = feed(tables, msg(write_create, create))
    assert mid == userinfo.TABLE_CREATE
    assert got == [
        (0, None),
        (1, Player(account(1), "alice", 11)),
        (2, Player(0, "Sniper", 13)),
        (3, Player(account(2), "bob", 12)),
    ]
    assert tables.userinfo(1).keys == [b"slot0", b"slot1", b"slot12", b"slot3"]
    mid, got = feed(tables, msg(write_update, [(3, None, None), (0, None, ALICE)]))
    assert mid == userinfo.TABLE_UPDATE
    assert got == [(3, None), (0, Player(account(1), "alice", 11))]
    assert [tables.owner(s, 1) for s in (0, 3, 4)] == [Player(account(1), "alice", 11), None, None]
    assert tables.userinfo(1).keys[3] == b"slot3"


def test_fixed_size_user_data_is_read_by_its_bit_count():
    w = wire.BitWriter()
    write_entries(w, [(0, b"a", b"\x05\x0a"), (1, b"b", None), (2, b"c", b"\xff\x0f")], 16, 12)
    w.write_ubit(0x2A, 6)  # what follows the entries stays in step
    br = wire.BitReader(w.get_bytes())
    t = Table("x", 16, fixed_bytes=2, fixed_bits=12)
    assert t.parse(br, 3) == [0, 1, 2]
    assert t.data == [b"\x05\x0a", None, b"\xff\x0f"]
    assert br.read_ubit(6) == 0x2A


def test_a_compressed_create_decodes_like_a_plain_one():
    entries = userinfo_entries(
        (("alice", u(account(1)), account(1), 11, 0), ("bob", "BOT", 0, 12, 1))
    )
    blob = lzss(b"abcabcabcabc" * 3)
    assert userinfo.lzss(blob) == b"abcabcabcabc" * 3
    assert len(blob) < 8 + len(b"abc" * 12)  # back references, not literals only
    plain, packed = StringTables(), StringTables()
    assert feed(plain, msg(write_create, entries)) == feed(
        packed, msg(write_create, entries, compressed=lzss)
    )
    assert packed.owner(0, 1) == Player(account(1), "alice", 11)
    assert userinfo.lzss(b"raw bytes") == b"raw bytes"


def test_tables_are_numbered_per_connection_and_start_over():
    """Updates name a table by its creation order; svc_ServerInfo (a map
    change) and a new session start an empty list."""
    tables = StringTables()
    feed(tables, msg(write_create, [(0, b"x", None)], name="downloadables", fixed=(1, 1)))
    feed(tables, msg(write_create, [(0, b"0", None), (1, b"1", None)]))
    assert feed(tables, msg(write_update, [(1, None, BOB)], table=1))[1] == [
        (1, Player(account(2), "bob", 12))
    ]
    assert feed(tables, msg(write_update, [(0, None, ALICE)], table=0))[1] == []
    assert feed(tables, msg(write_update, [(0, None, ALICE)], table=5))[1] == []
    assert tables.owner(1, 1).name == "bob" and tables.owner(0, 1) is None
    assert tables.owner(1, 2) is None  # a new session: no table yet
    feed(tables, msg(write_create, [(0, b"0", None), (1, b"1", BOB)]), session=2)
    tables.reset()
    assert tables.userinfo(2) is None
    assert feed(tables, msg(write_update, [(1, None, ALICE)], table=0), session=2)[1] == []
    assert tables.errors == 0


def test_a_message_that_does_not_decode_changes_nothing():
    tables = StringTables()
    feed(tables, msg(write_create, [(0, b"0", ALICE)]))
    w = msg(write_update, [(0, None, BOB)])
    n = w.nbits() - 200  # the message ends inside the user data
    data = w.get_bytes()[: (n + 7) // 8] + b"\xff" * 200  # the next message
    assert tables.feed(data, wire.NETMSG_TYPE_BITS, n, 1) == (userinfo.TABLE_UPDATE, [])
    assert tables.errors == 1
    assert tables.owner(0, 1).name == "alice"


@pytest.mark.parametrize(
    "raw, name",
    [
        ("caf\u00e9".encode(), "caf\u00e9"),
        # 31 bytes of name cut inside a two-byte character
        (("\u0416" * 15).encode() + b"\xd0", "\u0416" * 15),
    ],
)
def test_nicks_are_utf8_and_a_cut_last_character_is_dropped(raw, name):
    ud = raw.ljust(32, b"\0") + struct.pack("<i", 3) + b"[U:1:5]".ljust(36, b"\0")
    ud += struct.pack("<I", 5) + b"\0" * 56
    assert userinfo.player_info(ud) == Player(5, name, 3)
    book = NickBook()
    book.update([(1, userinfo.player_info(ud)), (2, None), (3, Player(0, "bot", 4))])
    assert book.by_account == {5: name}


def test_a_cut_of_a_relay_stream_map_change_join_leave_and_voice():
    """tests/data/userinfo_cut.tvd (tests/cut_userinfo.py): 2 min 40 s of a
    real relay, the server's own table bits with the people made up. A map
    change brings 18 new tables, userinfo the eighth; players join and
    change, slot 1 empties and fills again with a new userid; every voice
    message comes from its speaker's slot."""
    tables, book, seen = StringTables(), NickBook(), []
    fr = None

    def on_table(payload, start, end):
        mid, entries = tables.feed(payload, start, end, fr.cur_session)
        book.update(entries)
        if mid == userinfo.TABLE_UPDATE:
            seen.append(("update", [(s, p and p.userid) for s, p in entries]))

    def on_info(info):
        tables.reset()
        seen.append(("map", info["map"]))

    fr = Framer(keep_fates=False, on_table=on_table, on_info=on_info, slot_owner=tables.owner)
    voice = []
    for _dg in frame(Recording(os.path.join(HERE, "data", "userinfo_cut.tvd")), fr):
        voice += fr.voice
        fr.voice.clear()
    assert seen[0] == ("map", "dm_dod_donner_night_kb")
    assert [t.name for t in tables.tables].index("userinfo") == 7 and len(tables.tables) == 18
    assert ("update", [(1, None)]) in seen
    assert seen[-1] == ("update", [(1, 28)])
    assert tables.owner(1, fr.cur_session).userid == 28 and tables.owner(7, fr.cur_session) is None
    assert len(voice) == 3
    for m in voice:
        assert m.owner.friends_id == struct.unpack_from("<Q", m.data)[0] - userinfo.STEAMID64_BASE
    assert book.by_account == {acc: f"player{acc - account(0)}" for acc in book.by_account}
    assert {6, 7, 9, 10, 15, 19} <= {acc - account(0) for acc in book.by_account}
    assert tables.errors == 0
