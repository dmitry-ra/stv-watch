"""The session stream (events.jsonl, --json, feed.log) is in the order of
time on the recording's clock: one recording gives the same stream replayed
at any speed, with a recognizer or without, cut into sessions or not. Voice
is a talk line when an utterance starts and a voice line when it closes,
paired by utt; lines wait only for the text of an utterance that closed.
And how a run ends, in meta.json's `quit`."""

import functools
import hashlib
import json
import queue
import re
import threading
import time

import pytest
from helpers import T0, account, chat_bytes, packet, reliable_packet, steamid64, table_update
from test_ending import run_with, until
from test_rotate import chain, lines_of, meta_of, rows_of, wavs
from test_voice import Engine
from voicegen import noise, tone, voice_plan

from stvwatch import cli, source
from stvwatch.app import LIVE_LAG_NS, App, Backoff
from stvwatch.asr import recognizer
from stvwatch.net import dump, netchan
from stvwatch.times import utc_iso

NS = 10**9
ALICE, BOB = steamid64(1), steamid64(2)


def at(s):
    return T0 + int(s * NS)


def iso(s):
    return utc_iso(at(s))


def players(seq):
    return netchan.build_packet(
        seq,
        1,
        0x11223344,
        0,
        unreliable=table_update(
            ("alice", f"[U:1:{account(1)}]", account(1), 2),
            ("bob", f"[U:1:{account(2)}]", account(2), 3),
            create=True,
        ),
    )


@functools.cache
def timeline():
    """09:40:00-09:41:15 UTC. alice: a phrase at 5 s, a 32 s monologue from
    20 s that --max-utt-ms 30000 cuts in two, and a phrase at 63 s that only
    the traffic after a gap closes; bob talks across the minute; the relay is
    silent from 64 to 70 s; a chat line at 10, 60 (on the minute) and 72 s."""
    plan = voice_plan(
        [
            (5.0, ALICE, tone(2.0)),
            (20.0, ALICE, tone(32.0, pauses=((25.0, 26.0),))),
            (58.0, BOB, noise(4.5)),
            (63.0, ALICE, tone(0.8, freq=330.0)),
        ]
    )
    slots = {ALICE: 1, BOB: 2}
    timed = [(0.0, players)]
    for k, (t, text) in enumerate(((10.0, "one"), (60.0, "two"), (72.0, "three"))):
        timed.append(
            (
                t,
                lambda seq, text=text, k=k: reliable_packet(
                    seq, chat_bytes("bob", text), sub=k % 2
                ),
            )
        )
    timed += [(t, lambda seq, s=s, p=p: packet(seq, [(slots[s], p)])) for t, s, p in plan]
    quiet = [k * 0.05 for k in range(1, 1500)]
    timed += [(t, lambda seq: packet(seq)) for t in quiet if not 64.0 <= t < 70.0]
    timed.sort(key=lambda e: e[0])
    return [(at(t), make(n + 1)) for n, (t, make) in enumerate(timed)]


@pytest.fixture(scope="module")
def rec(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("rec") / "stream.tvd")
    with dump.DumpWriter(path, "127.0.0.1:27020", fsync_ms=0) as w:
        for t, d in timeline():
            w.write(dump.DATAGRAM_IN, d, t_ns=t)
    return path


def replay(rec, out, *extra, speed="0"):
    argv = ["--replay", rec, "--speed", speed, "--json", "--events", "all", "--out", str(out)]
    assert App(cli.parse_args(argv + ["--max-utt-ms", "30000", *extra])).run() == 0
    return chain(out)


def check_stream(sessions):
    """What every run holds to. -> the run's lines, sessions one after the other."""
    talks, voices, out = {}, {}, []
    for k, s in enumerate(sessions):
        lines = lines_of(s)
        times = [r["t_utc"] for r in lines]
        assert times == sorted(times), s.name
        if k:
            assert (lines[0]["type"], lines[0]["prev_dir"]) == ("rotate", str(sessions[k - 1]))
            assert lines[0]["t_utc"] == out[-1]["t_utc"]
        if k < len(sessions) - 1:
            assert (lines[-1]["type"], lines[-1]["next_dir"]) == ("rotate", str(sessions[k + 1]))
            # strictly before the cut: what happens at it is the next session's
            assert max(times[:-1]) < times[-1]
        else:
            assert lines[-1]["type"] == "done"
        for r in lines:
            if r["type"] == "talk":
                assert r["utt"] not in talks
                talks[r["utt"]] = (k, r)
            elif r["type"] == "voice":
                assert r["utt"] in talks and r["utt"] not in voices
                tk, talk = talks[r["utt"]]
                assert k - tk in (0, 1)
                assert (r["steamid64"], r["start_utc"], r["continued"], r["slot"]) == (
                    talk["steamid64"],
                    talk["start_utc"],
                    talk["continued"],
                    talk["slot"],
                )
                assert r["start_utc"] <= r["end_utc"] <= r["t_utc"]
                voices[r["utt"]] = r
        voice = [r for r in lines if r["type"] == "voice"]
        rows = rows_of(s)
        assert [(r["t_start_utc"], r["t_end_utc"], r["nick"], r["result"]) for r in rows] == [
            (r["start_utc"], r["end_utc"], r["nick"], r["result"]) for r in voice
        ]
        assert sorted(r["audio"] for r in rows) == wavs(s)
        out += lines
    assert sorted(talks) == sorted(voices) == list(range(1, len(talks) + 1))
    return out


@pytest.fixture
def slow(monkeypatch):
    """The recognizer takes 0.5 s an utterance: at --speed 20, 10 s of recording."""
    engine = Engine(delay=0.5)
    monkeypatch.setattr(recognizer, "build", lambda *a: engine)
    return engine


RUNS = [
    [],
    ["--rotate", "* * * * *"],
    ["--asr", "parakeet"],
    ["--asr", "parakeet", "--rotate", "* * * * *"],
]


@pytest.mark.parametrize("extra", RUNS, ids=["plain", "rotate", "asr", "asr-rotate"])
def test_the_stream_is_in_time_order_and_each_talk_has_one_voice(rec, tmp_path, slow, extra):
    """bob's talk line is in the session before the minute, his voice line,
    row and WAV in the one after; alice starts while he talks, and her
    phrase closes when the traffic comes back, after the line that says it
    stopped."""
    sessions = replay(rec, tmp_path / "o", *extra)
    assert len(sessions) == (2 if "--rotate" in extra else 1)
    lines = check_stream(sessions)
    said = [(r["type"], r["nick"], r["utt"], r["continued"]) for r in lines if "utt" in r]
    assert {(r["nick"], r["slot"]) for r in lines if "utt" in r} == {("alice", 1), ("bob", 2)}
    assert said == [
        ("talk", "alice", 1, False),
        ("voice", "alice", 1, False),
        ("talk", "alice", 2, False),
        ("voice", "alice", 2, False),
        ("talk", "alice", 3, True),
        ("voice", "alice", 3, True),
        ("talk", "bob", 4, False),
        ("talk", "alice", 5, False),
        ("voice", "bob", 4, False),
        ("voice", "alice", 5, False),
    ]
    # made before the first record, the replay's own lines take its time
    early = [r for r in lines if r["type"] in ("asr", "play")]
    assert early and {r["t_utc"] for r in early} == {iso(0)}
    by = {(r["type"], r.get("utt")): r for r in lines}
    # a piece is cut when it passes the limit, after the pause it ends at
    cut = by[("voice", 2)]
    assert by[("talk", 3)]["t_utc"] == cut["t_utc"] > cut["end_utc"] == by[("talk", 3)]["start_utc"]
    stopped = next(r for r in lines if r["type"] == "net" and "stopped" in r["text"])
    assert stopped["t_utc"] == iso(63.95 + 3.0)
    assert by[("voice", 5)]["t_utc"] == iso(70.0) > stopped["t_utc"]
    if "--rotate" in extra:
        old, new = (lines_of(s) for s in sessions)
        # what happens at the cut is the new session's
        assert (new[1]["type"], new[1]["text"], new[1]["t_utc"]) == ("chat", "two", iso(60))
        assert ("talk", 4) in {(r["type"], r.get("utt")) for r in old}
        assert ("voice", 4) in {(r["type"], r.get("utt")) for r in new}
        bob = [r["audio"] for r in rows_of(sessions[1]) if r["nick"] == "bob"]
        assert [w for w in wavs(sessions[1]) if str(BOB) in w] == bob != [""]
    texts = {r["result"] for r in lines if r["type"] == "voice"}
    assert texts == ({"text"} if "--asr" in extra else {"asr off"})


def comparable(sessions):
    """A run's lines, rows and WAVs without what depends on the wall clock:
    the replay speed in the play line, recognition time in details, where
    the sessions are."""
    names = {str(s): f"S{k}" for k, s in enumerate(sessions)}
    out = []
    for s in sessions:
        for r in lines_of(s):
            if r["type"] == "play":
                r["text"] = ""
            if "details" in r:
                r["details"] = re.sub(r" asr [0-9.]+s \+[0-9.]+s$", "", r["details"])
            for k, v in r.items():
                if isinstance(v, str):
                    for path, name in names.items():
                        v = v.replace(path, name)
                    r[k] = v
            out.append(r)
        for row in rows_of(s):
            row.pop("asr_ms"), row.pop("lag_ms")
            out.append(row)
        for w in wavs(s):
            out.append((w, hashlib.sha256((s / w).read_bytes()).hexdigest()))
    return out


@pytest.mark.parametrize("extra", RUNS, ids=["plain", "rotate", "asr", "asr-rotate"])
def test_a_replay_writes_the_same_stream_at_any_speed(rec, tmp_path, slow, extra):
    """At --speed 0 the reader runs ahead of the recognizer, at 20 the
    recognizer falls ten seconds of recording behind and the traffic gap is
    seen on the clock: the stream is the same."""
    fast = replay(rec, tmp_path / "fast", *extra)
    paced = replay(rec, tmp_path / "paced", *extra, speed="20")
    assert comparable(paced) == comparable(fast)


def test_lines_wait_only_for_the_text_of_a_closed_utterance(rec, tmp_path, monkeypatch):
    """The recognizer answers nothing until the exit: the stream is written
    up to the close of the first utterance, talk lines and the voice of
    nobody else held back; the rest follows once the text is there."""
    engine = Engine()
    monkeypatch.setattr(recognizer, "build", lambda *a: engine)
    gate, stream = threading.Event(), engine.open
    engine.open = lambda: (gate.wait(), stream())[1]
    seen = []
    shutdown = App.shutdown

    def at_exit(app):
        seen.extend(lines_of(chain(tmp_path / "o")[0]))
        gate.set()
        shutdown(app)

    monkeypatch.setattr(App, "shutdown", at_exit)
    (session,) = replay(rec, tmp_path / "o", "--asr", "parakeet")
    whole = lines_of(session)
    assert seen == whole[: len(seen)]
    first = next(r for r in whole if r["type"] == "voice")
    assert whole[len(seen)] == first
    assert ("talk", 1) in {(r["type"], r.get("utt")) for r in seen}
    assert max(r["t_utc"] for r in seen) <= first["t_utc"]


def test_live_the_stream_clock_waits_for_the_reader(tmp_path):
    """Live the clock is the wall clock less the journal's lag, but only once
    the reader has read all the network client wrote: records still queued or
    unread are older than that and must not find their moment passed."""
    app = App(cli.parse_args(["--relay", "127.0.0.1:9", "--out", str(tmp_path)]))

    class Reader:
        q, at_end, abandon = queue.Queue(), False, threading.Event()

        def waiting(self):
            return self.at_end

    # no network client yet: nothing of one is still to come
    before = time.time_ns()
    assert before <= app.dry_bound() <= time.time_ns()
    app.rec = Reader()
    assert app.dry_bound() is None
    app.rec.at_end = True
    before = time.time_ns()
    assert before - LIVE_LAG_NS <= app.dry_bound() <= time.time_ns() - LIVE_LAG_NS
    app.rec.q.put(("dg", None))
    assert app.dry_bound() is None
    # its journal read to the end, the client gone: the wall clock again
    app.rec.abandon.set()
    before = time.time_ns()
    assert before <= app.dry_bound() <= time.time_ns()


# ------------------------------------------------------------------ how a run ends
class EndingClient:
    """The network client as the viewer starts it: a journal of one datagram,
    then it is gone with exit code `rc`."""

    rc = 0

    def __init__(
        self, ip, port, dump_path, log_path, name="", duration_ms=0, extra=(), password=""
    ):
        self.path, self.log_path = dump_path, log_path

    def start(self):
        with dump.DumpWriter(self.path, "127.0.0.1:9", fsync_ms=0) as w:
            w.write(dump.DATAGRAM_IN, packet(1), t_ns=at(1))
        open(self.log_path, "w").close()
        return True

    def alive(self):
        return False

    def stop(self):
        return self.rc

    def cpu_s(self):
        return 0.0


@pytest.mark.parametrize(
    "rc, extra, quit, code",
    [
        (0, ["--duration-ms", "1"], "client left: time limit", 0),
        (0, [], "client died (rc 0)", 4),
        # gone with 0 before the bound: stopped from outside, not the bound
        (0, ["--duration-ms", "60000"], "client died (rc 0)", 4),
        (3, ["--duration-ms", "1"], "client died (rc 3)", 4),
        (-9, [], "client died (rc -9)", 4),
    ],
)
def test_a_live_run_names_how_its_network_client_ended(
    tmp_path, monkeypatch, rc, extra, quit, code
):
    monkeypatch.setattr(source, "LiveClient", EndingClient)
    monkeypatch.setattr(EndingClient, "rc", rc)
    monkeypatch.setattr(App, "precheck", lambda app, have: "")
    argv = ["--relay", "127.0.0.1:9", "--json", "--out", str(tmp_path), *extra]
    assert App(cli.parse_args(argv)).run() == code
    (session,) = list(tmp_path.iterdir())
    assert meta_of(session)["quit"] == quit
    assert lines_of(session)[-1]["text"].startswith(quit + " -> ")


def test_a_signal_is_the_end_even_when_the_network_client_goes_first(tmp_path, monkeypatch):
    """Ctrl-C reaches the client too, which may be gone before the viewer
    looks: the run ended by the signal."""
    monkeypatch.setattr(source, "LiveClient", EndingClient)
    monkeypatch.setattr(App, "precheck", lambda app, have: "")
    tick = App.tick

    def signalled(app, dry):
        app.ask_stop("SIGINT")
        tick(app, dry)

    monkeypatch.setattr(App, "tick", signalled)
    argv = ["--relay", "127.0.0.1:9", "--json", "--out", str(tmp_path)]
    assert App(cli.parse_args(argv)).run() == 0
    assert meta_of(next(tmp_path.iterdir()))["quit"] == "SIGINT"


# ------------------------------------------------------------------ --keep-trying
class WallClient:
    """A network client whose journal is written as it would be live: the
    datagrams of its plan at their seconds after it starts, on the wall
    clock. The first one dies (killed) once its plan is out, the second one
    stays until stopped."""

    plans = []

    def __init__(
        self, ip, port, dump_path, log_path, name="", duration_ms=0, extra=(), password=""
    ):
        self.path, self.log_path = dump_path, log_path
        self.plan, self.rc = WallClient.plans.pop(0)

    def start(self):
        open(self.log_path, "w").close()
        t0 = time.time_ns()
        with dump.DumpWriter(self.path, "127.0.0.1:9", fsync_ms=0) as w:
            # every network client counts its sessions from 1
            w.write(dump.SESSION_START, b'{"session": 1}', t_ns=t0)
            for n, (s, make) in enumerate(self.plan):
                time.sleep(max(0.0, (t0 + int(s * NS) - time.time_ns()) / NS))
                w.write(dump.DATAGRAM_IN, make(n + 1), t_ns=t0 + int(s * NS))
        return True

    def alive(self):
        return self.rc is None

    def stop(self):
        return 0 if self.rc is None else self.rc

    def cpu_s(self):
        return 0.0


def test_a_restart_cuts_the_stream_where_the_new_client_starts(tmp_path, monkeypatch, slow):
    """alice is still talking when the first client dies: the end of its
    journal closes her utterance, so its talk and voice lines both stay in
    the old session, which goes on the wall clock while no client runs (the
    wait line is written during the wait) and ends with the rotate of the
    restart; the new client's lines are the new session's."""
    monkeypatch.setattr(Backoff, "CLIENT_S", 2.0)
    monkeypatch.setattr(source, "LiveClient", WallClient)
    monkeypatch.setattr(App, "precheck", lambda app, have: "")
    said = [
        (0.2 + t, lambda seq, p=p: packet(seq, [(1, p)]))
        for t, _sid, p in voice_plan([(0.0, ALICE, tone(1.0))])
    ]
    quiet = [(k * 0.05, lambda seq: packet(seq)) for k in range(1, 27)]
    first = sorted([(0.0, players)] + said + quiet, key=lambda e: e[0])
    hello = (0.1, lambda seq: reliable_packet(seq, chat_bytes("bob", "hello")))
    WallClient.plans = [(first, -9), ([(0.0, players), hello], None)]
    out = tmp_path / "o"
    argv = ["--relay", "127.0.0.1:9", "--events", "all", "--keep-trying", "--out", str(out)]
    app = App(cli.parse_args(argv + ["--asr", "parakeet"]))
    in_wait = []

    def seen(what):
        files = sorted(out.glob("*/events.jsonl"))
        return any(what(json.loads(ln)) for f in files for ln in f.read_text().splitlines())

    def drive():
        assert until(lambda: seen(lambda r: r["type"] == "wait"))
        in_wait.append(app.waiting is not None)
        assert until(lambda: seen(lambda r: r["text"] == "hello"))

    assert run_with(app, drive) == 0
    assert in_wait == [True]
    old, new = chain(out)
    lines = check_stream([old, new])
    a, b = lines_of(old), lines_of(new)
    assert [r["type"] for r in a if r["type"] in ("talk", "voice")] == ["talk", "voice"]
    assert [(r["type"], r.get("reason")) for r in a[-2:]] == [("wait", None), ("rotate", "restart")]
    assert (b[0]["type"], b[0]["reason"], b[-1]["type"]) == ("rotate", "restart", "done")
    assert [r["text"] for r in b if r["type"] == "chat"] == ["hello"]
    assert lines[-1]["t_utc"] >= lines[-2]["t_utc"]


def test_done_is_the_last_line_even_when_a_bug_leaves_voice_without_its_text(
    rec, tmp_path, monkeypatch
):
    """A bug while winding down, with utterances still waiting for the
    recognizer and the rotation at the minute behind them: their places are
    let go, the rotation is made, and only then meta.json and done, both in
    the session the stream ends in."""
    engine = Engine()
    monkeypatch.setattr(recognizer, "build", lambda *a: engine)
    gate, stream = threading.Event(), engine.open
    engine.open = lambda: (gate.wait(), stream())[1]

    def broken(app):
        raise RuntimeError("winding down")

    monkeypatch.setattr(App, "wind_down", broken)
    argv = ["--replay", rec, "--speed", "0", "--json", "--events", "all", "--asr", "parakeet"]
    argv += ["--rotate", "* * * * *", "--out", str(tmp_path / "o")]
    try:
        with pytest.raises(RuntimeError, match="winding down"):
            App(cli.parse_args(argv)).run()
    finally:
        gate.set()
    old, new = chain(tmp_path / "o")
    lines = lines_of(old) + lines_of(new)
    times = [r["t_utc"] for r in lines]
    assert times == sorted(times)
    assert (lines_of(old)[-1]["type"], lines[-1]["type"], lines[-1]["class"]) == (
        "rotate",
        "done",
        "bug",
    )
    assert (meta_of(old)["quit"], meta_of(new)["quit"], meta_of(new)["class"]) == (
        "rotated",
        "crash",
        "bug",
    )
    assert {r["type"] for r in lines} >= {"talk", "chat"}
    assert not any(r["type"] == "voice" for r in lines)
