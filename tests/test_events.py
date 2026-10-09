"""Game events in the stv-watch feed, its JSON lines and session files.

Records are the shapes stream.streamevents.Collector emits (its decoding has
its own tests); tables are player_info_t structs as the relay sends them.
"""

import json
import os
import re
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest
from helpers import (
    CHALLENGE,
    T0,
    account,
    body,
    chat,
    chat_bytes,
    packet,
    reliable_packet,
    steamid64,
    table_update,
    usermessage,
    voice_payload,
    write_recording,
)
from voicegen import payload as steam_voice

from stvwatch import events as ge
from stvwatch.app import TSV_HEAD, App
from stvwatch.cli import default_out, parse_args
from stvwatch.net import dump, netchan, wire
from stvwatch.source import Pacer
from stvwatch.stream import streamevents as se
from stvwatch.stream.userinfo import STEAMID64_BASE, scan_players

CREATE, UPDATE = ge.STRING_TABLE_CREATE, ge.STRING_TABLE_UPDATE
STV = [sys.executable, "-B", "-m", "stvwatch.cli"]
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


def steam2(acc):
    return f"STEAM_0:{acc & 1}:{acc >> 1}"


def player_info(name, guid, friends_id, userid):
    s = name.encode().ljust(32, b"\0") + struct.pack("<i", userid)
    s += guid.encode().ljust(33, b"\0") + b"\0" * 3 + struct.pack("<I", friends_id)
    return s + b"\0" * 40


def table(*players, shift=0):
    blob = b"\x07" + b"".join(player_info(*p) for p in players)
    payload = (int.from_bytes(blob, "little") << shift).to_bytes(len(blob) + 1, "little")
    return payload, 0, len(payload) * 8


def u(acc):
    return f"[U:1:{acc}]"


class Feed:
    """GameEvents behind a stand-in framer: session and receive time."""

    def __init__(self, types=ge.TYPES):
        self.fr = SimpleNamespace(cur_session=1, cur_t_ns=T0)
        self.g = ge.GameEvents(types)
        self.g.attach(self.fr)

    def at(self, s, session=None):
        self.fr.cur_t_ns = T0 + int(s * 10**9)
        if session is not None:
            self.fr.cur_session = session

    def tab(self, mid, *players):
        self.g.on_table(*table(*players), mid)

    def msg(self, _name, **f):
        self.g.record(
            {
                "kind": "usermsg",
                "name": _name,
                "t_ns": self.fr.cur_t_ns,
                "session": self.fr.cur_session,
                "tick": 0,
                "f": f,
            }
        )

    def ev(self, _name, **f):
        self.g.record(
            {
                "kind": "event",
                "name": _name,
                "t_ns": self.fr.cur_t_ns,
                "session": self.fr.cur_session,
                "tick": 0,
                "f": f,
            }
        )

    def take(self):
        out, self.g.out = self.g.out, []
        return out


def make_app(tmp_path, *extra):
    return App(parse_args(["--replay", "x.tvd", "--out", str(tmp_path), *extra]))


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


def line(app, rec):
    return "".join(t for t, _s in app.game_spans(rec))


def test_userinfo_players_carry_userid_and_bots_at_any_bit_offset():
    acc = account(4242)
    for shift in (0, 5):
        got = scan_players(
            *table(("caf\u00e9", "STEAM_H:1:12345", acc, 7), ("Sniper", "BOT", 0, 303), shift=shift)
        )
        assert sorted(got) == [(0, "Sniper", 303), (acc, "caf\u00e9", 7)]
    # a bot guid with a friends id, or a userid out of range, is a false match
    assert scan_players(*table(("x", "BOT", 5, 9), ("y", "[U:1:5]", 5, 0))) == []


def test_every_event_type_becomes_one_self_contained_line(app):
    a, b, c = account(1001), account(2002), account(3003)
    f = Feed()
    f.tab(CREATE, ("alice", u(a), a, 11), ("Sniper", "BOT", 0, 12))
    f.at(1)
    f.tab(UPDATE, ("bob\x1b[2J", u(b), b, 13))  # new entry: connect
    f.tab(UPDATE, ("alice", u(a), a, 11))  # same entry: nothing
    f.msg("TextMsg", dest=1, msg="#Game_connected", params=["bob\x1b[2J", "", "", ""])
    f.msg("SayText2", ent=1, chat=1, fmt="HL2MP_Chat_All", params=["alice", "hi", "", ""])
    f.msg("SayText2", ent=1, chat=1, fmt="HL2MP_Chat_AllDead", params=["alice", "dead hi", "", ""])
    f.msg("SayText2", ent=1, chat=1, fmt="HL2MP_Chat_AllSpec", params=["tv", "w", "", ""])
    f.msg("SayText2", ent=0, chat=0, fmt="\x01\x0700FFFFbob\x1b[2J: \x01yo", params=[""] * 4)
    f.msg("SayText2", ent=0, chat=0, fmt="\x01\x04bob has planted \x01slam mine!", params=[""] * 4)
    f.msg("SayText", ent=0, text="Console: [ADMIN] hello\n", chat=1)
    f.msg("TextMsg", dest=3, msg="\x01\x04[SM Parachute]\x01 Welcome!", params=[""] * 4)
    f.msg("TextMsg", dest=4, msg="Voting in: 15s", params=[""] * 4)
    f.msg("HudMsg", channel=1, text="dm_test by someone")
    f.ev("server_cvar", cvarname="sm_nextmap", cvarvalue="dm_runoff")
    f.ev("player_death", userid=13, attacker=11, weapon="shotgun")
    f.ev("player_death", userid=11, attacker=11, weapon="slam")
    f.ev("player_death", userid=12, attacker=0, weapon="worldspawn")
    f.ev("player_team", userid=11, team=3, oldteam=2, disconnect=0, name="alice")
    f.tab(UPDATE, ("alice2", u(a), a, 11))  # nick change
    f.ev(
        "player_disconnect",
        userid=13,
        reason="Client Disconnect",
        name="bob\x1b[2J",
        networkid=u(b),
        bot=0,
    )
    f.ev("player_disconnect", userid=12, reason="Kicked", name="Sniper", networkid="BOT", bot=1)
    f.tab(UPDATE, ("carol", "STEAM_H:0:1", c, 14))
    got = [(r["type"], line(app, r)) for r in f.take()]
    assert got == [
        ("connect", f"connect bob?[2J connecting  {steam2(b)}"),
        ("join", f"join  bob?[2J entered the game  {steam2(b)}"),
        ("chat", "chat  alice: hi"),
        ("chat", "chat  alice [alldead]: dead hi"),
        ("chat", "chat  tv [allspec]: w"),
        ("chat", "chat  bob?[2J [plugin]: yo"),
        ("sourcemod", "sourcemod bob has planted slam mine!"),
        ("console", "console Console: [ADMIN] hello"),
        ("sourcemod", "sourcemod [SM Parachute] Welcome!"),
        ("server", "server Voting in: 15s"),
        ("server", "server dm_test by someone"),
        ("server", "server cvar sm_nextmap = dm_runoff"),
        ("death", "death alice killed bob?[2J (shotgun)"),
        ("death", "death alice killed self (slam)"),
        ("death", "death Sniper died (worldspawn)"),
        ("team", f"team  alice team combine -> rebels  {steam2(a)}"),
        ("name", f"name  alice -> alice2  {steam2(a)}"),
        ("leave", f"leave bob?[2J left: Client Disconnect  {steam2(b)}"),
        ("leave", "leave Sniper left: Kicked  BOT"),
        ("connect", f"connect carol connecting  {steam2(c)}"),
    ]
    assert f.g.kill_summary(T0 + 2 * 10**9) == "kills 3 3/min top alice 1"
    assert f.g.kill_summary(T0 + 70 * 10**9) == "kills 3 0/min top alice 1"


def test_steam2_form_of_networkid_names_the_same_account():
    a = account(7)
    assert ge.account_of(steam2(a)) == ge.account_of(u(a)) == a
    assert ge.account_of("BOT") == 0


@pytest.mark.parametrize(
    "spec,want",
    [
        ("", ("chat", "console", "connect", "join", "leave", "name")),
        ("default", ("chat", "console", "connect", "join", "leave", "name")),
        (
            "all",
            (
                "chat",
                "console",
                "connect",
                "join",
                "leave",
                "death",
                "team",
                "name",
                "server",
                "sourcemod",
            ),
        ),
        ("none", ()),
        ("death,chat,death", ("death", "chat")),
        (
            "all,-server",
            ("chat", "console", "connect", "join", "leave", "death", "team", "name", "sourcemod"),
        ),
        (
            "-server",
            ("chat", "console", "connect", "join", "leave", "death", "team", "name", "sourcemod"),
        ),
        (
            " -server , -death",
            ("chat", "console", "connect", "join", "leave", "team", "name", "sourcemod"),
        ),
        ("default,-name", ("chat", "console", "connect", "join", "leave")),
        ("-chat,chat,death", ("death",)),
        ("all,-default", ("death", "team", "server", "sourcemod")),
        ("none,-chat", ()),
    ],
)
def test_events_spec_to_types(spec, want):
    assert ge.parse_types(spec) == want


@pytest.mark.parametrize(
    "argv,want",
    [
        (
            ["--events", "-server", "--debug"],
            ("chat", "console", "connect", "join", "leave", "death", "team", "name", "sourcemod"),
        ),
        (
            ["--events=-server,-death"],
            ("chat", "console", "connect", "join", "leave", "team", "name", "sourcemod"),
        ),
        (["--events", "default,-name"], ("chat", "console", "connect", "join", "leave")),
    ],
)
def test_events_exclusions_on_the_command_line(argv, want):
    assert parse_args(["--replay", "x.tvd"] + argv).event_types == want


@pytest.mark.parametrize("spec", ["chat,kills", "-kills", "all,-kills", "-", "chat,,leave"])
def test_events_spec_with_an_unknown_name_is_an_error(spec):
    with pytest.raises(ValueError, match="unknown event type"):
        ge.parse_types(spec)


def test_filter_hides_types_but_kills_are_counted():
    a, b = account(1), account(3)
    f = Feed(types=ge.DEFAULT)
    f.tab(CREATE, ("a", u(a), a, 2), ("b", u(b), b, 4))
    f.ev("player_death", userid=4, attacker=2, weapon="ar2")
    f.ev("server_cvar", cvarname="x", cvarvalue="1")
    f.msg("SayText2", ent=1, chat=1, fmt="HL2MP_Chat_All", params=["a", "gg", "", ""])
    assert [r["type"] for r in f.take()] == ["chat"]
    assert f.g.counts["death"] == 1 and f.g.kill_summary(T0) == "kills 1 1/min top a 1"
    # muted (replay fast-forward): state kept, nothing shown or counted
    f.g.muted = True
    f.ev("player_death", userid=4, attacker=2, weapon="ar2")
    f.msg("SayText2", ent=1, chat=1, fmt="HL2MP_Chat_All", params=["a", "gg", "", ""])
    assert f.take() == [] and f.g.deaths == 1


def test_join_repeated_after_a_map_change_is_not_a_new_join():
    a, b, c = account(1), account(2), account(3)
    f = Feed()
    f.tab(CREATE, ("in-game", u(a), a, 10))
    f.at(5)
    f.tab(UPDATE, ("connecting", u(b), b, 11))  # connects, no join yet
    f.at(60, session=2)  # map change: new session
    f.tab(CREATE, ("in-game", u(a), a, 10), ("connecting", u(b), b, 11))
    f.at(66)
    f.msg("TextMsg", dest=1, msg="#Game_connected", params=["in-game", "", "", ""])
    f.msg("TextMsg", dest=1, msg="#Game_connected", params=["connecting", "", "", ""])
    f.tab(UPDATE, ("newcomer", u(c), c, 12))
    f.msg("TextMsg", dest=1, msg="#Game_connected", params=["newcomer", "", "", ""])
    f.at(200, session=3)  # next map: all three stay
    f.tab(
        CREATE,
        ("in-game", u(a), a, 10),
        ("connecting", u(b), b, 11),
        ("newcomer", u(c), c, 12),
    )
    for n in ("in-game", "connecting", "newcomer"):
        f.msg("TextMsg", dest=1, msg="#Game_connected", params=[n, "", "", ""])
    # one repeat per map change: a second greeting on the same map is a join
    f.msg("TextMsg", dest=1, msg="#Game_connected", params=["in-game", "", "", ""])
    # the one who left and came back during the map is a join again
    f.ev("player_disconnect", userid=12, reason="x", name="newcomer", networkid=u(c), bot=0)
    f.tab(UPDATE, ("newcomer", u(c), c, 13))
    f.msg("TextMsg", dest=1, msg="#Game_connected", params=["newcomer", "", "", ""])
    got = [(r["type"], r["nick"], round((r["t_ns"] - T0) / 1e9)) for r in f.take()]
    assert got == [
        ("connect", "connecting", 5),
        ("join", "connecting", 66),
        ("connect", "newcomer", 66),
        ("join", "newcomer", 66),
        ("join", "in-game", 200),
        ("leave", "newcomer", 200),
        ("connect", "newcomer", 200),
        ("join", "newcomer", 200),
    ]
    assert f.g.counts["join_repeat"] == 4


def test_players_refilling_an_empty_table_after_a_map_change_are_not_connects():
    """After a map change the userinfo table can be created empty and filled
    by updates: those who stayed are neither connects nor joins."""
    a, b = account(1), account(2)
    f = Feed()
    f.tab(CREATE, ("stays", u(a), a, 10))
    f.at(60, session=2)
    f.tab(CREATE, ("Sniper", "BOT", 0, 13))  # no player in it
    f.tab(UPDATE, ("stays", u(a), a, 10))
    f.msg("TextMsg", dest=1, msg="#Game_connected", params=["stays", "", "", ""])
    f.tab(UPDATE, ("new", u(b), b, 11))
    got = [(r["type"], r["nick"]) for r in f.take()]
    assert got == [("connect", "new")]
    assert f.g.counts["join_repeat"] == 1


def test_reconnects_the_table_shows_late_or_we_missed_are_joins():
    """Two cases seen on live relays: a reconnect whose greeting comes in the
    same packet before its table entry; a player who left during a map change
    while we were reconnecting (his player_disconnect never reached us) and
    came back."""
    a, b = account(1), account(2)
    f = Feed()
    f.tab(CREATE, ("stays", u(a), a, 10), ("drops", u(b), b, 11))
    f.at(10)
    f.ev("player_disconnect", userid=10, reason="x", name="stays", networkid=u(a), bot=0)
    f.msg("TextMsg", dest=1, msg="#Game_connected", params=["stays", "", "", ""])
    f.tab(UPDATE, ("stays", u(a), a, 12))
    f.at(60, session=2)  # map change, b gone meanwhile
    f.tab(CREATE, ("stays", u(a), a, 12))
    f.at(70)
    f.tab(UPDATE, ("drops", u(b), b, 13))
    f.msg("TextMsg", dest=1, msg="#Game_connected", params=["drops", "", "", ""])
    got = [
        (r["type"], r["steamid64"] - STEAMID64_BASE, round((r["t_ns"] - T0) / 1e9))
        for r in f.take()
    ]
    assert got == [
        ("leave", a, 10),
        ("connect", a, 10),
        ("join", a, 10),
        ("connect", b, 70),
        ("join", b, 70),
    ]


def test_a_spectators_voice_is_marked_unless_alltalk():
    a = account(1)
    f = Feed()
    sid = STEAMID64_BASE + a
    f.tab(CREATE, ("spec", u(a), a, 10))
    assert not f.g.unheard(sid)  # team unknown: no mark
    f.ev("player_team", userid=10, team=1, oldteam=0, disconnect=0, name="spec")
    assert f.g.unheard(sid)
    f.ev("server_cvar", cvarname="sv_alltalk", cvarvalue="1")
    assert not f.g.unheard(sid)
    f.ev("server_cvar", cvarname="sv_alltalk", cvarvalue="0")
    assert f.g.unheard(sid)
    f.at(60, session=2)  # new map: unassigned again
    f.tab(CREATE, ("spec", u(a), a, 10))
    assert not f.g.unheard(sid)
    f.ev("player_team", userid=10, team=1, oldteam=0, disconnect=0, name="spec")
    f.ev("player_team", userid=10, team=0, oldteam=1, disconnect=1, name="spec")
    assert not f.g.unheard(sid)


# --- the receive path under the feed: resends never become duplicate lines ---


def chat_bodies(n):
    """Reliable bodies: a filler svc_Print sized like test_link's texts (so
    later ones go out as -2 and multi-fragment transfers), then a chat line."""
    import simlink

    out = []
    for i in range(n):
        w = wire.BitWriter()
        w.write_ubit(simlink.SVC_PRINT, wire.NETMSG_TYPE_BITS)
        w.write_string("x" * (40 + (i * 397) % 2500))
        chat(w, "nick", f"line {i:02d}")
        out.append(w.get_bytes())
    return out


def record_link(path, seed, legacy, steps, loss, dup, part_loss, bodies=40, drop=()):
    """The live client (Session) against simlink's relay; its datagrams both
    ways written as stv-watch's live mode records them. `drop`: steps whose
    relay packets the down link loses."""
    import simlink
    import test_link

    rng = simlink.seeded(seed)
    s, _got = test_link.client(legacy)
    relay = simlink.Relay(s.conn.challenge, chat_bodies(bodies))
    down = simlink.Link(rng, loss, dup, jitter_s=0.03 if loss else 0.0, part_loss=part_loss)
    up = simlink.Link(rng, loss, dup, jitter_s=0.03 if loss else 0.0)
    events = {k + 1: lambda _r: setattr(down, "loss", loss) for k in drop}
    events.update({k: lambda _r: setattr(down, "loss", 1.0) for k in drop})
    with dump.DumpWriter(path, "127.0.0.1:27020", fsync_ms=0) as w:
        simlink.run(
            s,
            relay,
            down,
            up,
            steps,
            events=events,
            on_inbound=lambda d, t: w.write(dump.DATAGRAM_IN, d, t_ns=t),
            on_outbound=lambda p, t: w.write(dump.DATAGRAM_OUT, p, t_ns=t),
        )
    return relay


def session_of(out):
    (session,) = list(out.iterdir())
    return session


def replay_lines(rec, out, *extra):
    args = parse_args(["--replay", rec, "--speed", "0", "--monitor", "--out", str(out), *extra])
    assert App(args).run() == 0
    return (session_of(out) / "feed.log").read_text().splitlines()


@pytest.mark.parametrize("legacy", [False, True])
def test_chat_over_a_faulty_link_is_in_the_feed_exactly_once(tmp_path, legacy):
    """Client that joins -2: loss, duplicates, reordering and lost -2 parts,
    every line once. Client without -2 support: it stalls, the relay resends
    the unacked batch into the void; replayed, the recording still gives each
    line once."""
    rec = str(tmp_path / "r.tvd")
    if legacy:
        relay = record_link(rec, 1, True, 600, 0.0, 0.0, 0.0)
    else:
        relay = record_link(rec, 4, False, 6000, 0.15, 0.1, 0.1)
    assert relay.resends > 0
    feed = replay_lines(rec, tmp_path / "out", "--events", "chat")
    got = [ln.split("\t", 2)[2] for ln in feed if "\tchat  " in ln]
    n = 40 if not legacy else next(i for i in range(40) if 40 + (i * 397) % 2500 > 1100) + 1
    assert got == [f"chat  nick: line {i:02d}" for i in range(n)]


def test_a_resent_first_packet_of_a_new_session_is_one_feed_line(tmp_path):
    from test_framing import resent_first_packet_of_session_2

    rec = str(tmp_path / "r.tvd")
    resent_first_packet_of_session_2(rec)
    feed = replay_lines(rec, tmp_path / "out", "--events", "chat")
    assert [ln.split("\t", 2)[2] for ln in feed if "\tchat  " in ln] == ["chat  nick: hello"]


def short_message(name, head, strings):
    """A user message that ends exactly at its declared length but names
    fewer parameters than its stock format uses."""

    def fill(b):
        for byte in head[:-1]:
            b.write_byte(byte)
        b.write_string(head[-1])
        for p in strings:
            b.write_string(p)

    w = wire.BitWriter()
    usermessage(w, se.USER_MESSAGES.index(name), body(fill))
    return w.get_bytes() + b"\x00"


@pytest.mark.parametrize(
    "name, head, strings",
    [
        ("SayText2", (1, 1, "HL2MP_Chat_All"), []),
        ("SayText2", (1, 1, "HL2MP_Chat_All"), ["nick"]),
        ("TextMsg", (3, "#Game_connected"), []),
    ],
)
def test_a_stock_message_short_of_parameters_is_a_misparse_and_the_feed_goes_on(
    tmp_path, name, head, strings
):
    rec = str(tmp_path / "r.tvd")
    write_recording(
        rec,
        [
            (T0, reliable_packet(1, short_message(name, head, strings))),
            (T0 + 10**9, reliable_packet(2, chat_bytes("nick", "after"), sub=1)),
        ],
    )
    a = parse_args(["--replay", rec, "--speed", "0", "--monitor", "--out", str(tmp_path / "o")])
    app = App(a)
    assert (app.run(), app.game.collector.usermsg_misparse, app.game.counts) == (
        0,
        {name: 1},
        {"chat": 1},
    )


@pytest.mark.parametrize("debug", [False, True])
def test_a_lost_sequence_gap_is_one_feed_line_only_with_debug(tmp_path, debug):
    """The down link loses the relay's packets 6 and 7 (steps 5 and 6): with
    --debug one line at the receive time of 8, without it none; the counter
    counts them either way."""
    from stvwatch.app import utc_iso
    from stvwatch.stream.recording import Recording

    rec = str(tmp_path / "r.tvd")
    record_link(rec, 1, False, 400, 0.0, 0.0, 0.0, bodies=3, drop=(5, 6))
    t8 = next(
        dg.t_ns
        for dg in Recording(rec)
        if netchan.decode_header(netchan.unwrap(dg.data)[1]).sequence == 8
    )
    out = tmp_path / "out"
    args = parse_args(
        ["--replay", rec, "--speed", "0", "--monitor", "--events", "none", "--out", str(out)]
        + (["--debug"] if debug else [])
    )
    assert App(args).run() == 0
    session = session_of(out)
    feed = (session / "feed.log").read_text().splitlines()
    lost = [ln for ln in feed if "\tnet   lost" in ln]
    assert lost == ([f"{utc_iso(t8)}\t\tnet   lost: seq 5 -> 8 (2)"] if debug else [])
    recs = [json.loads(ln) for ln in (session / "events.jsonl").read_text().splitlines()]
    assert [r for r in recs if r["text"].startswith("lost")] == (
        [
            {
                "t_utc": utc_iso(t8),
                "type": "net",
                "steamid64": 0,
                "nick": "",
                "text": "lost: seq 5 -> 8 (2)",
                "seq_from": 5,
                "seq_to": 8,
                "lost": 2,
            }
        ]
        if debug
        else []
    )
    assert json.loads((session / "meta.json").read_text())["framer"]["seq_lost"] == 2


def chat_recording(path, voice=True):
    """Two utterances of one speaker (the second after a 2.7 s pause) and a
    chat line between them, then plain traffic: chat at 1.5 s, the recording
    ends at 6 s."""
    a = steamid64(1)
    w = wire.BitWriter()
    chat(w, "nick", "between")
    plan = [
        (0, packet(1, [(1, steam_voice(a, 0))] if voice else [])),
        (60, packet(2, [(1, steam_voice(a, 3))] if voice else [])),
        (1500, reliable_packet(3, w.get_bytes() + b"\x00")),
        (3000, packet(4, [(1, steam_voice(a, 0))] if voice else [])),
    ]
    seq = 5
    for ms in range(3020, 6000, 20):
        plan.append((ms, packet(seq)))
        seq += 1
    write_recording(path, [(10**18 + ms * 1_000_000, d) for ms, d in plan])


def test_json_monitor_lines_reach_a_pipe_reader_as_they_happen(tmp_path):
    """What a program reading a pipe sees: each event is one JSON line
    readable before the process ends, no escape sequences, a voice utterance
    only as its final line."""
    import time

    rec = str(tmp_path / "r.tvd")
    chat_recording(rec)
    cmd = STV + [
        "--replay",
        rec,
        "--speed",
        "1",
        "--json",
        "--events",
        "all",
        "--out",
        str(tmp_path / "out"),
    ]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=ENV)
    seen = []
    try:
        for raw in p.stdout:
            seen.append((raw, p.poll(), time.monotonic()))
    finally:
        p.wait(timeout=60)
    assert p.returncode == 0
    recs = [json.loads(raw) for raw, _rc, _t in seen]
    assert all(b"\x1b" not in raw and raw.endswith(b"\n") for raw, _rc, _t in seen)
    assert [r["type"] for r in recs if r["type"] != "voice"] == ["play", "net", "chat", "done"]
    assert [(r["steamid64"], r["text"], r["result"]) for r in recs if r["type"] == "voice"] == [
        (steamid64(1), "", "asr off")
    ] * 2
    chat_rec = recs[[r["type"] for r in recs].index("chat")]
    assert (chat_rec["nick"], chat_rec["text"]) == ("nick", "between")
    assert {"t_utc", "type", "steamid64", "nick", "text"} <= set(chat_rec)
    assert "t_local" not in chat_rec
    # streamed: the chat line (record time 1.5 s) was read while the replay
    # still had 4.5 s of record time to pace, not flushed at exit
    i = next(k for k, (raw, _rc, _t) in enumerate(seen) if b'"chat"' in raw)
    assert seen[i][1] is None and seen[-1][2] - seen[i][2] > 2.0
    # the same as text: no status lines, no escapes, every line starts with its time
    cmd[cmd.index("--json")] = "--monitor"
    cmd[cmd.index("--speed") + 1] = "0"
    out = subprocess.run(
        cmd + ["--status-every-ms", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=60,
        env=ENV,
    ).stdout.decode()
    lines = out.splitlines()
    assert "\x1b" not in out and not any("status" in ln for ln in lines)
    assert all(ln[2] == ":" and ln[5] == ":" and ln[8] == "." and ln[12] == " " for ln in lines)
    # a voice line is written when the utterance closes (time = its start):
    # the chat datagram at 1.5 s is the one whose clock closes the first
    assert [ln[13:] for ln in lines if "nick" in ln or "voice" in ln] == [
        "chat  nick: between",
        f"voice {steam2(account(1))}: asr off  0.1s",
        f"voice {steam2(account(1))}: asr off  0.1s",
    ]
    # --debug: the transport numbers on screen too
    out = subprocess.run(
        cmd + ["--status-every-ms", "0", "--debug"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=60,
        env=ENV,
    ).stdout.decode()
    # the bit rate is the encoder's, not ours to pin
    shown = [re.sub(r" \d+kb/s ", " KBPS ", ln[13:]) for ln in out.splitlines() if " voice " in ln]
    assert shown == [
        f"voice {steam2(account(1))}: asr off  0.1s/0.1s fr 6 plc 0 gap 0 KBPS press 1 msg 2"
        " -2 0% arr 2 p50 60 max 60ms +1.4s",
        f"voice {steam2(account(1))}: asr off  0.1s/0.0s fr 3 plc 0 gap 0 KBPS press 1 msg 1"
        " -2 0% arr 1 p50 0 max 0ms +1.0s",
    ]


def run_on_tty(cmd, env, timeout=60, keys=None):
    """cmd with a 120x30 pseudo-terminal for stdin and stdout: the full screen.
    `keys` (bytes) are typed once the screen has drawn. -> (exit code, what it drew)"""
    import fcntl
    import pty
    import select
    import termios
    import time

    m, s = pty.openpty()
    fcntl.ioctl(s, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
    p = subprocess.Popen(cmd, stdin=s, stdout=s, stderr=subprocess.DEVNULL, env=env)
    os.close(s)
    end = time.monotonic() + timeout
    drawn = b""
    try:
        while time.monotonic() < end:
            if select.select([m], [], [], 0.2)[0]:
                try:
                    got = os.read(m, 65536)
                except OSError:  # EIO: the child closed the terminal
                    break
                if not got:
                    break
                drawn += got
                if keys and b"q quit" in drawn:
                    os.write(m, keys)
                    keys = None
            elif p.poll() is not None:
                break
    finally:
        os.close(m)
        if p.poll() is None:
            p.kill()
        p.wait(timeout=10)
    return p.returncode, drawn


def test_events_jsonl_holds_what_json_prints_in_every_screen_mode(tmp_path):
    """events.jsonl of a normal-screen, --plain, --monitor and --json run of one
    recording equals line for line what --json printed. Run-dependent only:
    the time of the replay's first line (written before any datagram, on the
    wall clock) and the session directory in the last one."""
    rec = str(tmp_path / "r.tvd")
    chat_recording(rec)

    def cmd(name, *mode):
        return STV + [
            "--replay",
            rec,
            "--speed",
            "0",
            "--events",
            "all",
            "--debug",
            "--out",
            str(tmp_path / name),
            *mode,
        ]

    def jsonl(name):
        return (session_of(tmp_path / name) / "events.jsonl").read_text().splitlines()

    def same(lines):
        out = []
        for ln in lines:
            r = json.loads(ln)
            if r["type"] == "play":
                del r["t_utc"]
            r["text"] = re.sub(r" -> /\S+$", " -> DIR", r["text"])
            out.append(r)
        return out

    printed = subprocess.run(
        cmd("json", "--json"),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=ENV,
        timeout=60,
    )
    assert printed.returncode == 0
    want = printed.stdout.decode().splitlines()
    assert jsonl("json") == want
    assert [json.loads(ln)["type"] for ln in want] == [
        "play",
        "net",
        "chat",
        "voice",
        "voice",
        "done",
    ]
    for name, mode in (
        ("plain", ["--plain", "--status-every-ms", "0"]),
        ("monitor", ["--monitor"]),
    ):
        assert (
            subprocess.run(
                cmd(name, *mode),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=ENV,
                timeout=60,
            ).returncode
            == 0
        )
        assert same(jsonl(name)) == same(want), name
    rc, drawn = run_on_tty(cmd("tty"), ENV)
    assert rc == 0 and b"\x1b[?25l" in drawn  # the pinned-block screen, not plain
    assert drawn.endswith(b"\x1b[?25h") or b"\x1b[?25h" in drawn[-200:]
    assert same(jsonl("tty")) == same(want)


def test_q_in_the_normal_screen_quits_cleanly(tmp_path):
    rec = str(tmp_path / "r.tvd")
    chat_recording(rec)
    cmd = STV + ["--replay", rec, "--speed", "0.05", "--out", str(tmp_path / "o")]
    rc, _drawn = run_on_tty(cmd, ENV, keys=b"q")
    assert rc == 0
    meta = json.loads((session_of(tmp_path / "o") / "meta.json").read_text())
    assert meta["quit"] == "key q"


def test_events_jsonl_is_flushed_line_by_line(tmp_path):
    """A reader tailing events.jsonl sees each line as it is made."""
    app = make_app(tmp_path, "--plain")
    path = os.path.join(app.dir, "events.jsonl")
    app.event("conn", "first", t_ns=T0)
    assert [json.loads(ln)["text"] for ln in open(path)] == ["first"]
    app.event("conn", "second", t_ns=T0)
    assert [json.loads(ln)["text"] for ln in open(path)] == ["first", "second"]
    app.jsonl_fh.close()


def test_time_is_utc_unless_tz_names_a_zone_and_only_then_t_local(tmp_path):
    """Default: screen in UTC, JSON with t_utc only. --tz: screen and t_local
    in that zone (2025-10-07 09:40 UTC is 12:40 in Moscow, 05:40 in New York)."""
    utc = make_app(tmp_path / "u", "--no-color")
    utc.screen = Lines()
    utc.event("conn", "x", t_ns=T0)
    assert json.loads(open(os.path.join(utc.dir, "events.jsonl")).read()) == {
        "t_utc": "2025-10-07T09:40:00.000Z",
        "type": "conn",
        "steamid64": 0,
        "nick": "",
        "text": "x",
    }
    assert text_of(utc.screen.fed[0]).startswith("09:40:00.000 conn")
    for zone, clock in (("Europe/Moscow", "12:40"), ("America/New_York", "05:40")):
        app = make_app(tmp_path / zone.replace("/", "_"), "--tz", zone)
        app.screen = Lines()
        app.pacer = Pacer(False)
        app.now = T0
        app.event("conn", "x", t_ns=T0)
        rec = json.loads(open(os.path.join(app.dir, "events.jsonl")).read())
        assert rec["t_local"] == f"2025-10-07 {clock}:00" and rec["t_utc"].endswith("09:40:00.000Z")
        assert list(rec)[:3] == ["t_utc", "t_local", "type"]
        assert text_of(app.screen.fed[0]).startswith(f"{clock}:00.000 conn")
        assert any(f"{clock}:00.000 {zone}" in t for t, _s in app.block()[1])
    utc.pacer = Pacer(False)
    utc.now = T0
    assert any("09:40:00.000 UTC" in t for t, _s in utc.block()[1])


def test_unknown_tz_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        parse_args(["--replay", "x.tvd", "--tz", "Mars/Olympus"])
    assert e.value.code == 2 and "unknown time zone 'Mars/Olympus'" in capsys.readouterr().err


BAD_PORT = "stv-watch: error: --relay: the port must be a number 1-65535, not "
BAD_ADDR = "stv-watch: error: --relay wants IP:PORT (IPv4 or a host name), not "


@pytest.mark.parametrize(
    "relay, outcome",
    [
        ("127.0.0.1:27020", "127.0.0.1:27020"),
        ("relay.example:027015", "relay.example:27015"),
        ("127.0.0.1:65535", "127.0.0.1:65535"),
        ("127.0.0.1:notaport", (2, BAD_PORT + "'notaport'")),
        ("127.0.0.1:0", (2, BAD_PORT + "'0'")),
        ("127.0.0.1:65536", (2, BAD_PORT + "'65536'")),
        ("127.0.0.1:70000", (2, BAD_PORT + "'70000'")),
        ("127.0.0.1:-1", (2, BAD_PORT + "'-1'")),
        ("127.0.0.1:\u0661\u0662", (2, BAD_PORT + "'\u0661\u0662'")),
        ("host:", (2, BAD_PORT + "''")),
        (":", (2, BAD_ADDR + "':'")),
        (":27020", (2, BAD_ADDR + "':27020'")),
        ("127.0.0.1", (2, BAD_ADDR + "'127.0.0.1'")),
        ("::1", (2, BAD_ADDR + "'::1'")),
        ("[::1]:27020", (2, BAD_ADDR + "'[::1]:27020'")),
    ],
)
def test_relay_address_is_checked_on_the_command_line(relay, outcome, capsys):
    """-> the canonical address, or (exit code, last line on stderr)."""
    try:
        got = parse_args(["--relay", relay]).relay
    except SystemExit as e:
        got = (e.code, capsys.readouterr().err.splitlines()[-1])
    assert got == outcome


def test_session_directory_defaults_to_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_out() == str(tmp_path / "xdg" / "stv-watch" / "sessions")
    assert parse_args(["--replay", "x.tvd"]).out == default_out()
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert default_out() == str(tmp_path / "home" / ".local" / "share" / "stv-watch" / "sessions")
    assert parse_args(["--replay", "x.tvd", "--out", "/elsewhere"]).out == "/elsewhere"


def test_session_files_of_a_replay(tmp_path):
    """The session directory holds the feed, the JSON lines, the transcript,
    a WAV per utterance and meta.json. Voice messages whose payload fails
    the Steam CRC are counted and make no speaker."""
    rec = str(tmp_path / "r.tvd")
    chat_recording(rec)
    replay_lines(rec, tmp_path / "out")
    session = session_of(tmp_path / "out")
    assert sorted(p.name for p in session.iterdir()) == [
        "audio",
        "events.jsonl",
        "feed.log",
        "meta.json",
        "stderr.log",
        "transcript.tsv",
    ]
    meta = json.loads((session / "meta.json").read_text())
    assert meta["quit"] == "end of recording" and meta["tz"] == "UTC"
    assert meta["traffic"]["in"] == 4 + len(range(3020, 6000, 20))
    sid = str(steamid64(1))
    assert meta["speakers"] == {
        sid: {"nick": "", "audio_ms": 180, "frames": 9, "utterances": 2, "phrases": 0}
    }
    # durations are whole milliseconds named _ms, moments ISO strings
    assert meta["args"]["quiet_ms"] == 3000 and meta["args"]["duration_ms"] == 0
    assert not [k for k in meta["args"] if k.endswith("_s")]
    assert meta["conn"]["state_utc"] is None and "state_ns" not in meta["conn"]
    assert meta["counters"]["payload_bad"] == 0 and "asr" not in meta
    rows = [ln.split("\t") for ln in (session / "transcript.tsv").read_text().splitlines()]
    assert rows[0] == list(TSV_HEAD)
    wavs = sorted(p.name for p in (session / "audio").iterdir())
    assert [(r[2], r[5], r[6], r[7]) for r in rows[1:]] == [
        (sid, "asr off", "", "audio/" + wavs[0]),
        (sid, "asr off", "", "audio/" + wavs[1]),
    ]
    assert wavs == [f"014640_{sid}_1.wav", f"014643_{sid}_2.wav"]


@pytest.mark.parametrize(
    "old, new",
    [
        ("--skip", "--skip-ms"),
        ("--seconds", "--duration-ms"),
        ("--quiet", "--quiet-ms"),
        ("--status-every", "--status-every-ms"),
        ("--drain", "--drain-ms"),
        ("--max-utt", "--max-utt-ms"),
    ],
)
def test_duration_options_are_whole_milliseconds(old, new, capsys):
    """The old seconds names are gone, not taken for abbreviations of the new
    ones (`--drain 20` would be 20 ms)."""
    dest = new[2:].replace("-", "_")
    assert getattr(parse_args(["--replay", "x", new, "2500"]), dest) == 2500
    with pytest.raises(SystemExit):
        parse_args(["--replay", "x", old, "2500"])
    assert f"unrecognized arguments: {old}" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        parse_args(["--replay", "x", new, "2.5"])
    assert "whole milliseconds" in capsys.readouterr().err


def test_every_option_the_docs_name_is_one_stv_watch_takes(capsys):
    """With abbreviations off, a stale name in the docs is an argument error."""
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    known = set(re.findall(r"--[a-z][a-z0-9-]*", capsys.readouterr().out))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    named = {}
    for doc in ("README.md", "AGENTS.md", "docs/session-files.md", "docs/events.schema.json"):
        with open(os.path.join(root, doc), encoding="utf-8") as f:
            for opt in re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", f.read()):
                named.setdefault(opt, doc)
    # uv's and black's, in the README's how-to
    stale = {o: d for o, d in named.items() if o not in known | {"--project", "--check"}}
    assert stale == {}


def test_crc_failing_voice_is_counted_and_makes_no_speaker(tmp_path):
    """Not Steam voice at all, and Steam voice whose CRC does not match: both
    counted by the framer and by the viewer, neither decoded."""
    a = steamid64(1)
    bad_crc = bytearray(steam_voice(a, 0))
    bad_crc[-1] ^= 1
    plan = [(0, packet(1, [(1, voice_payload(a)), (1, bytes(bad_crc))])), (2000, packet(2))]
    rec = str(tmp_path / "r.tvd")
    write_recording(rec, [(T0 + ms * 1_000_000, d) for ms, d in plan])
    out = tmp_path / "out"
    app = App(
        parse_args(["--replay", rec, "--speed", "0", "--monitor", "--debug", "--out", str(out)])
    )
    assert app.run() == 0
    session = session_of(out)
    lines = (session / "events.jsonl").read_text().splitlines()
    assert "voice" not in [json.loads(ln)["type"] for ln in lines]
    meta = json.loads((session / "meta.json").read_text())
    assert meta["counters"]["payload_bad"] == 2 and meta["speakers"] == {}
    assert meta["framer"]["voice_msgs"] == 2 and meta["framer"]["voice_crc_bad"] == 2
    (voice,) = ["".join(t for t, _s in ln) for ln in app.block() if ln and ln[0][0] == "voice"]
    assert voice.startswith("voice msgs 2 (via -2 0%)  bad 2  ")


class Lines:
    """Stand-in screen: what the app hands over, not how a terminal draws it."""

    def __init__(self, color=False):
        self.closed, self.fed, self.color = [], [], color

    def feed(self, spans):
        self.fed.append(spans)

    def live_open(self, key, spans):
        pass

    def live_close(self, key, spans, cont=()):
        self.closed.append(spans)


def text_of(spans):
    return "".join(t for t, _s in spans)


def test_follow_a_growing_journal_equals_replaying_it(tmp_path):
    """--follow: another process writes the journal while we read it. What it
    held before we came only sets the state (no lines); from then on the
    lines are those a replay of the finished file gives."""
    import threading
    import time

    def chat_packet(seq, text):
        w = wire.BitWriter()
        chat(w, "nick", text)
        return reliable_packet(seq, w.get_bytes() + b"\x00")

    a = steamid64(1)
    start = time.time_ns()
    past = [(start - 9 * 10**9, chat_packet(1, "before we came"))]
    plan = [
        (500, packet(2, [(1, voice_payload(a))])),
        (560, packet(3, [(1, voice_payload(a))])),
        (1500, chat_packet(4, "while we watch")),
    ]
    seq = 5
    for ms in range(1520, 3000, 20):
        plan.append((ms, packet(seq)))
        seq += 1
    path = str(tmp_path / "j.tvd")
    w = dump.DumpWriter(path, "127.0.0.1:27020", fsync_ms=0)
    w.write(
        dump.SESSION_START,
        b'{"session": 1, "endpoint": "127.0.0.1:27020"}',
        t_ns=start - 10 * 10**9,
    )
    for t, d in past:
        w.write(dump.DATAGRAM_IN, d, t_ns=t)

    def writer():
        for ms, d in plan:
            t = start + ms * 1_000_000
            time.sleep(max(0.0, (t - time.time_ns()) / 1e9))
            w.write(dump.DATAGRAM_IN, d, t_ns=t)
        w.close()

    th = threading.Thread(target=writer)
    th.start()
    follow = App(
        parse_args(
            ["--follow", path, "--duration-ms", "2400", "--monitor", "--out", str(tmp_path / "f")]
        )
    )
    assert follow.run() == 0
    th.join(timeout=10)
    assert (
        App(
            parse_args(
                ["--replay", path, "--speed", "0", "--monitor", "--out", str(tmp_path / "r")]
            )
        ).run()
        == 0
    )

    def lines(d):
        return [
            ln.split("\t", 2)[2]
            for ln in (session_of(tmp_path / d) / "feed.log").read_text().splitlines()
            if "\tplay " not in ln and "\tdone " not in ln
        ]

    # traffic start/stop lines tell the journal's gaps, seen from different
    # moments: compared apart from them
    got = [ln for ln in lines("f") if "traffic" not in ln]
    want = [ln for ln in lines("r") if "traffic" not in ln]
    old = ("chat  nick: before we came", "conn  session #1 open (127.0.0.1:27020)")
    assert [o in want for o in old] == [True, True] and [o in got for o in old] == [False] * 2
    assert got == [ln for ln in want if ln not in old]
    assert "chat  nick: while we watch" in got


def test_stv_watch_slot_check_uses_the_count_seen_while_in(tmp_path, monkeypatch):
    """Before 1 when we came, another watcher and a stranger joined later, 3
    while we were in, 2 after we left: freed, though 2 > 1. Without a look
    while in, the count before is the fallback."""
    from stvwatch import source

    fast = source.client.slot_freed
    monkeypatch.setattr(
        source.client, "slot_freed", lambda relay, n: fast(relay, n, settle_s=0, every=0)
    )
    monkeypatch.setattr(source.client, "slot_released", lambda relay, before: (2 <= before, 2))
    monkeypatch.setattr(source.client, "safe_info", lambda relay: {"players": 2})
    app = App(parse_args(["--relay", "127.0.0.1:27020", "--out", str(tmp_path)]))
    app.a2s_before = {"spectators_before": 1}
    app.with_us = 3
    assert app.slot_check() == (
        True,
        2,
        "slot check: relay viewers while in 3, after 2 - released",
    )
    app.with_us = None
    assert app.slot_check() == (
        False,
        2,
        "slot check: relay viewers before 1, after 2 - NOT RELEASED",
    )


def test_the_count_with_us_is_taken_only_well_into_a_flowing_session(tmp_path):
    """Seen on a local server killed and restarted: our session still said
    FULL while the new server answered A2S without us, and the slot check
    then compared against 0."""
    app = App(parse_args(["--relay", "127.0.0.1:27020", "--out", str(tmp_path)]))
    app.conn.state, app.conn.full_ns = "FULL", T0
    app.traffic.inbound(T0 + 2 * 10**9, 100, False)
    app.traffic.check(T0 + 2 * 10**9)
    app.note_relay_count(2, T0 + 2 * 10**9)  # too soon after FULL: A2S lags
    assert app.with_us is None
    app.traffic.inbound(T0 + 6 * 10**9, 100, False)
    app.traffic.check(T0 + 6 * 10**9)
    app.note_relay_count(3, T0 + 6 * 10**9)
    assert app.with_us == 3
    app.traffic.check(T0 + 20 * 10**9)  # the relay went silent
    app.note_relay_count(0, T0 + 20 * 10**9)
    assert app.with_us == 3


@pytest.mark.parametrize("color", [True, False])
def test_voice_and_chat_lead_the_feed_the_rest_steps_back(tmp_path, color):
    """Colour: voice and chat lines alike (tag, nick, colon, bold) but for the
    said text: voice white, chat yellow; console magenta, joins and leaves in
    their own muted green and red (not the plain ones of our connection
    lines), kills and server notices dimmest gray; all lines start at the
    same column. Without colour: the same text."""
    from stvwatch.app import ChannelAudio, Utt
    from stvwatch.model import Channel
    from stvwatch.render import to_ansi

    app = make_app(tmp_path)
    app.pacer = Pacer(False)
    app.screen = Lines(color)
    recs = [
        {
            "type": "chat",
            "t_ns": T0,
            "steamid64": 0,
            "nick": "bob",
            "text": "hi",
            "extra": {"channel": "all"},
        },
        {
            "type": "console",
            "t_ns": T0,
            "steamid64": 0,
            "nick": "Console",
            "text": "hey",
            "extra": {},
        },
        {
            "type": "join",
            "t_ns": T0,
            "steamid64": 0,
            "nick": "bob",
            "text": "entered the game",
            "extra": {},
        },
        {"type": "leave", "t_ns": T0, "steamid64": 0, "nick": "bob", "text": "x", "extra": {}},
        {
            "type": "death",
            "t_ns": T0,
            "steamid64": 0,
            "nick": "bob",
            "text": "died (slam)",
            "extra": {},
        },
    ]
    for r in recs:
        app.feed(app.game_spans(r), r["t_ns"], r)
    sid = steamid64(1)
    app.channels[sid] = Channel(sid, T0)
    app.audio[sid] = ChannelAudio()
    utt = app.utts[(sid, 1)] = Utt((sid, 1), sid, T0)
    utt.audio_s = 1.2
    app.finalize(utt, "ok", {})
    got = [to_ansi(sp, color) for sp in app.screen.fed + app.screen.closed]
    T, R = "\x1b[2m09:40:00.000 \x1b[0m", "\x1b[0m"
    if not color:
        assert got == [
            "09:40:00.000 chat  bob: hi",
            "09:40:00.000 console Console: hey",
            "09:40:00.000 join  bob entered the game",
            "09:40:00.000 leave bob left: x",
            "09:40:00.000 death bob died (slam)",
            f"09:40:00.000 voice {steam2(account(1))}: ok  1.2s",
        ]
        return
    assert got == [
        T + "\x1b[34mchat  " + R + "\x1b[36mbob" + R + ": " + "\x1b[1;33mhi" + R,
        T + "\x1b[35mconsole " + R + "\x1b[35mConsole: " + R + "\x1b[35mhey" + R,
        T
        + "\x1b[38;5;71mjoin  "
        + R
        + "\x1b[38;5;71mbob"
        + R
        + "\x1b[38;5;71m entered the game"
        + R,
        T + "\x1b[38;5;167mleave " + R + "\x1b[38;5;167mbob" + R + "\x1b[38;5;167m left: x" + R,
        T + "\x1b[2;90mdeath " + R + "\x1b[2;90mbob" + R + "\x1b[2;90m died (slam)" + R,
        T
        + "\x1b[34mvoice "
        + R
        + f"\x1b[36m{steam2(account(1))}"
        + R
        + ": "
        + "\x1b[1mok"
        + R
        + "\x1b[2m  1.2s"
        + R,
    ]


def test_players_are_keyed_by_steamid_and_named_from_the_stream_only(tmp_path):
    """A player's name is his nick in the stream's userinfo table, the current
    one; a rename is a `name` line, and his next voice line has the new nick.
    Every output that names a player carries his SteamID64: --json and
    feed.log."""
    acc = account(4242)
    sid = STEAMID64_BASE + acc
    w = wire.BitWriter()
    chat(w, "old", "hi")
    plan = [
        (
            0,
            netchan.build_packet(1, 1, CHALLENGE, 0, unreliable=table_update(("old", u(acc), acc))),
        ),
        (100, packet(2, [(1, steam_voice(sid, 0))])),
        (1500, reliable_packet(4, w.get_bytes() + b"\x00")),
        (
            2000,
            netchan.build_packet(5, 1, CHALLENGE, 0, unreliable=table_update(("new", u(acc), acc))),
        ),
    ]
    seq = 6
    for ms in range(2020, 3000, 20):
        plan.append((ms, packet(seq, [(1, steam_voice(sid, 0))] if ms == 2500 else [])))
        seq += 1
    rec = str(tmp_path / "r.tvd")
    write_recording(rec, [(T0 + ms * 1_000_000, d) for ms, d in plan])
    out = subprocess.run(
        STV
        + [
            "--replay",
            rec,
            "--speed",
            "0",
            "--json",
            "--events",
            "all",
            "--out",
            str(tmp_path / "o"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=60,
        env=ENV,
    ).stdout.decode()
    recs = [json.loads(ln) for ln in out.splitlines()]
    players = [
        (r["type"], r["steamid64"], r["nick"], r.get("old"))
        for r in recs
        if r["type"] in ("connect", "chat", "name")
    ]
    assert players == [
        ("connect", sid, "old", None),
        ("chat", sid, "old", None),
        ("name", sid, "new", "old"),
    ]
    voice = sorted((r["t_utc"], r["steamid64"], r["nick"]) for r in recs if r["type"] == "voice")
    assert [v[1:] for v in voice] == [(sid, "old"), (sid, "new")]
    feed = [
        ln.split("\t") for ln in (session_of(tmp_path / "o") / "feed.log").read_text().splitlines()
    ]
    assert [(f[1], f[2].split(" ")[0]) for f in feed if f[1]] == [
        (str(sid), "connect"),
        (str(sid), "chat"),
        (str(sid), "voice"),
        (str(sid), "name"),
        (str(sid), "voice"),
    ]


def test_a_rename_in_the_silence_before_the_close_names_the_utterance(tmp_path):
    """--speed 0 drains many packets between ticks: the nick is read from the
    table at the close, not from the last tick."""
    acc = account(4242)
    sid = STEAMID64_BASE + acc
    plan = [
        (
            0,
            netchan.build_packet(1, 1, CHALLENGE, 0, unreliable=table_update(("old", u(acc), acc))),
        ),
        (100, packet(2, [(1, steam_voice(sid, 0))])),
        (
            500,
            netchan.build_packet(3, 1, CHALLENGE, 0, unreliable=table_update(("new", u(acc), acc))),
        ),
    ]
    plan += [(ms, packet(4 + i, [])) for i, ms in enumerate(range(520, 3000, 20))]
    rec = str(tmp_path / "r.tvd")
    write_recording(rec, [(T0 + ms * 1_000_000, d) for ms, d in plan])
    out = subprocess.run(
        STV + ["--replay", rec, "--speed", "0", "--json", "--out", str(tmp_path / "o")],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=60,
        env=ENV,
    ).stdout.decode()
    recs = [json.loads(ln) for ln in out.splitlines()]
    assert [r["nick"] for r in recs if r["type"] == "voice"] == ["new"]


@pytest.mark.parametrize("debug", [False, True])
def test_a_kill_names_both_steamids_in_feed_log_and_with_debug(tmp_path, debug):
    k, v = account(1001), account(2002)
    app = make_app(tmp_path, *(["--debug"] if debug else []))
    app.pacer = Pacer(False)
    app.screen = Lines()
    f = Feed()
    f.tab(CREATE, ("killer", u(k), k, 11), ("victim", u(v), v, 13), ("Sniper", "BOT", 0, 12))
    f.ev("player_death", userid=13, attacker=11, weapon="ar2")
    f.ev("player_death", userid=12, attacker=11, weapon="ar2")
    app.game.out = f.take()
    app.game_lines()
    shown = [text_of(sp)[13:] for sp in app.screen.fed]
    logged = [
        ln.split("\t") for ln in (next(tmp_path.iterdir()) / "feed.log").read_text().splitlines()
    ]
    full = [
        f"death killer killed victim (ar2)  {steam2(k)} > {steam2(v)}",
        f"death killer killed Sniper (ar2)  {steam2(k)} > -",
    ]
    assert [ln[2] for ln in logged] == full
    assert [ln[1] for ln in logged] == [str(STEAMID64_BASE + k)] * 2
    assert shown == (full if debug else [ln.split("  STEAM")[0] for ln in full])
