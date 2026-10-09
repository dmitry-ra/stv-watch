"""Write tests/data/sample.tvd: a synthetic recording with one of every game
event, voice messages around them, a split packet, a lost sequence gap and the
session lifecycle records the network client writes.

    uv run python tests/make_sample.py [OUT.tvd]

The recording is built only from the client's own writers, so it stays in step
with the wire format; tests/data/sample.events.jsonl is what a replay of it
gives (tests/test_sample.py checks both).
"""

import json
import os
import sys

from helpers import (
    CHALLENGE,
    T0,
    account,
    body,
    chat,
    ones_pad,
    reliable_packet,
    split,
    steamid64,
    userinfo_entries,
    usermessage,
    voice_payload,
    write_create,
    write_update,
    write_voice,
)

from stvwatch.net import dump, netchan, wire
from stvwatch.stream import streamevents as se

ALICE, BOB, CAROL = account(1), account(2), account(3)
EVENTS = {
    # id: (name, [(type, key)]); types: 1 string, 4 short, 5 byte, 6 bool
    3: (
        "player_death",
        [(4, "userid"), (4, "attacker"), (1, "weapon")],
    ),
    4: (
        "player_disconnect",
        [(4, "userid"), (1, "reason"), (1, "name"), (1, "networkid"), (4, "bot")],
    ),
    5: (
        "player_team",
        [(4, "userid"), (5, "team"), (5, "oldteam"), (6, "disconnect"), (1, "name")],
    ),
    6: ("server_cvar", [(1, "cvarname"), (1, "cvarvalue")]),
}


def bits_into(w, src):
    for i in range(src.nbits()):
        w.write_one_bit(src.get_bytes()[i >> 3] >> (i & 7))


def event_list(w):
    lst = wire.BitWriter()
    for eid, (name, keys) in EVENTS.items():
        lst.write_ubit(eid, 9)
        lst.write_string(name)
        for t, k in keys:
            lst.write_ubit(t, 3)
            lst.write_string(k)
        lst.write_ubit(0, 3)
    w.write_ubit(se.SVC_GAMEEVENTLIST, wire.NETMSG_TYPE_BITS)
    w.write_ubit(len(EVENTS), 9)
    w.write_ubit(lst.nbits(), 20)
    bits_into(w, lst)


def game_event(w, eid, **vals):
    ev = wire.BitWriter()
    ev.write_ubit(eid, 9)
    for t, k in EVENTS[eid][1]:
        v = vals[k]
        if t == 1:
            ev.write_string(v)
        elif t == 4:
            ev.write_ubit(v & 0xFFFF, 16)
        elif t == 5:
            ev.write_byte(v)
        else:
            ev.write_one_bit(v)
    w.write_ubit(se.SVC_GAMEEVENT, wire.NETMSG_TYPE_BITS)
    w.write_ubit(ev.nbits(), 11)
    bits_into(w, ev)


def text_msg(w, dest, msg, *params):
    def fill(b):
        b.write_byte(dest)
        b.write_string(msg)
        for p in params:
            b.write_string(p)

    usermessage(w, se.USER_MESSAGES.index("TextMsg"), body(fill))


def say_text(w, ent, text):
    usermessage(
        w,
        se.USER_MESSAGES.index("SayText"),
        body(lambda b: (b.write_byte(ent), b.write_string(text), b.write_byte(1))),
    )


def hud_msg(w, text):
    def fill(b):
        b.write_byte(1)  # channel
        b.write_ubit(0, 2 * 32 + 9 * 8 + 4 * 32)  # position, colours, effect, times
        b.write_string(text)

    usermessage(w, se.USER_MESSAGES.index("HudMsg"), body(fill))


def server_info(w):
    """svc_ServerInfo with the trailing replay bit, as current builds send it."""
    w.write_ubit(wire.SVC_SERVERINFO, wire.NETMSG_TYPE_BITS)
    w.write_ubit(wire.PROTOCOL_VERSION, 16)
    w.write_long(1)  # spawncount
    w.write_one_bit(1)  # SourceTV
    w.write_one_bit(1)  # dedicated
    w.write_long(0)  # client CRC
    w.write_ubit(200, 16)  # classes
    w.write_bytes(b"\0" * 16)  # map MD5
    w.write_byte(0)  # player slot
    w.write_byte(16)  # max clients
    w.write_long(0x3C888889)  # tick interval, 1/66 s as float bits
    w.write_byte(ord("l"))
    w.write_string("hl2mp")
    w.write_string("dm_sample")
    w.write_string("sky_day01_01")
    w.write_string("Sample server")
    w.write_one_bit(0)  # not a replay


def userinfo_create(*players):
    """svc_CreateStringTable 'userinfo' carrying player_info_t entries."""
    return write_create(wire.BitWriter(), userinfo_entries(players))


def userinfo_update(*players):
    return write_update(wire.BitWriter(), userinfo_entries(players))


def stream(*parts):
    w = wire.BitWriter()
    for p in parts:
        p(w)
    return w.get_bytes() + b"\x00"


def unreliable(seq, fill, voices=()):
    w = wire.BitWriter()
    for slot, p in voices:
        write_voice(w, slot, p)
    fill(w)
    ones_pad(w)
    return netchan.build_packet(seq, 1, CHALLENGE, 0, unreliable=w.get_bytes())


def build(path):
    u = lambda acc: f"[U:1:{acc}]"  # noqa: E731
    seq = [0]

    def nxt(skip=0):
        seq[0] += 1 + skip
        return seq[0]

    timed = []  # (ms, kind, data)

    def ev(ms, rtype, **f):
        timed.append((ms, rtype, json.dumps(f, sort_keys=True).encode()))

    def dg(ms, data):
        timed.append((ms, dump.DATAGRAM_IN, data))

    ev(0, dump.RECONNECT, attempt=1, ok=True)
    ev(1, dump.SESSION_START, session=1, attempt=1, endpoint="127.0.0.1:27020")
    tables = userinfo_create(
        ("alice", u(ALICE), ALICE, 11), ("bob", u(BOB), BOB, 12), ("Sniper", "BOT", 0, 13)
    )
    dg(100, reliable_packet(nxt(), stream(server_info, event_list, lambda w: bits_into(w, tables))))
    ev(150, dump.SIGNON, state=6, name="FULL")
    voice = [(1, voice_payload(steamid64(1)))]
    dg(200, unreliable(nxt(), lambda w: chat(w, "alice", "hello"), voice))
    dg(
        300,
        unreliable(
            nxt(), lambda w: bits_into(w, userinfo_update(("carol", u(CAROL), CAROL, 14, 4)))
        ),
    )
    dg(400, unreliable(nxt(), lambda w: text_msg(w, 1, "#Game_connected", "carol", "", "", "")))
    dg(500, unreliable(nxt(), lambda w: say_text(w, 0, "Console: map vote in 5 minutes\n")))
    dg(
        600,
        unreliable(nxt(), lambda w: text_msg(w, 3, "\x01\x04[SM]\x01 Welcome!", "", "", "", "")),
    )
    dg(700, unreliable(nxt(), lambda w: text_msg(w, 4, "Round %s1 of 3", "2", "", "", "")))
    dg(800, unreliable(nxt(), lambda w: hud_msg(w, "dm_sample by nobody")))
    dg(900, unreliable(nxt(), lambda w: game_event(w, 6, cvarname="sv_alltalk", cvarvalue="1")))
    big = unreliable(
        nxt(),
        lambda w: game_event(w, 3, userid=12, attacker=11, weapon="crossbow_bolt"),
        [(2, voice_payload(steamid64(2), n=12))],
    )
    for k, part in enumerate(split(big, group=1, size=300)):
        dg(1000 + k * 10, part)
    dg(1100, unreliable(nxt(), lambda w: game_event(w, 3, userid=11, attacker=11, weapon="slam")))
    dg(1200, unreliable(nxt(), lambda w: game_event(w, 3, userid=13, attacker=0, weapon="world")))
    team = dict(userid=12, team=1, oldteam=3, disconnect=0, name="bob")
    dg(1300, unreliable(nxt(), lambda w: game_event(w, 5, **team)))
    dg(
        1400,
        unreliable(nxt(), lambda w: bits_into(w, userinfo_update(("alice2", u(ALICE), ALICE, 11)))),
    )
    dg(1500, unreliable(nxt(skip=2), lambda w: chat(w, "bob", "gg", fmt="HL2MP_Chat_AllSpec")))
    quit_ = dict(userid=14, reason="Disconnect", name="carol", networkid=u(CAROL), bot=0)
    dg(1600, unreliable(nxt(), lambda w: game_event(w, 4, **quit_)))
    for ms in range(1620, 6000, 20):
        dg(ms, unreliable(nxt(), lambda w: None))
    ev(6000, dump.MAPCHANGE, map="dm_next")
    ev(6001, dump.BROKEN, cause="changelevel", detail="server signalled changelevel")
    ev(6002, dump.LEAVE, why="changelevel", sent=2)
    ev(6100, dump.RECONNECT, attempt=2, ok=False, error="timed out")
    with dump.DumpWriter(path, "127.0.0.1:27020", fsync_ms=0) as w:
        for ms, rtype, data in timed:
            w.write(rtype, data, t_ns=T0 + ms * 1_000_000)


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "data", "sample.tvd")
    build(out)
    print(out, os.path.getsize(out), "bytes")
