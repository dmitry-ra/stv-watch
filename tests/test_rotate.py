"""--rotate: a new session directory at the times of a cron schedule, read in
the recording's time. The schedule's own rules, a replay rotated around voice
in progress, a rotated replay against an unrotated one, and the live path where
the network client rotates its journal and the viewer follows it."""

import functools
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from helpers import T0, account, chat_bytes, packet, reliable_packet, steamid64, table_update
from test_sample import SCHEMA, check
from test_voice import Engine, held_until_exit
from voicegen import noise, tone, voice_plan

from stvwatch import cli, rotate, source
from stvwatch.app import App
from stvwatch.asr import recognizer
from stvwatch.net import dump, netchan
from stvwatch.net import main as netmain
from stvwatch.stream.recording import Recording

NS = 10**9
EVERY_MINUTE = "* * * * *"
ALICE, BOB = steamid64(1), steamid64(2)


def at(s):
    return T0 + int(s * NS)


def iso(s):
    return (
        datetime.fromtimestamp(at(s) / NS, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )


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
    """09:40:50-09:41:10 UTC. alice talks 55-57 s and is done before the
    minute; bob starts at 59 s and talks over it; a chat line on each side."""
    plan = voice_plan([(55.0, ALICE, tone(2.0)), (59.0, BOB, noise(2.5))])
    slots = {ALICE: 1, BOB: 2}
    timed = [
        (50.0, players),
        (52.0, lambda seq: reliable_packet(seq, chat_bytes("bob", "before"))),
        (65.0, lambda seq: reliable_packet(seq, chat_bytes("alice", "after"), sub=1)),
    ]
    timed += [(t, lambda seq, s=s, p=p: packet(seq, [(slots[s], p)])) for t, s, p in plan]
    timed += [(50.0 + k * 0.05, lambda seq: packet(seq)) for k in range(1, 400)]
    timed.sort(key=lambda e: e[0])
    return [(at(t), make(n + 1)) for n, (t, make) in enumerate(timed)]


@pytest.fixture(scope="module")
def rec(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("rec") / "minute.tvd")
    with dump.DumpWriter(path, "127.0.0.1:27020", fsync_ms=0) as w:
        for t, d in timeline():
            w.write(dump.DATAGRAM_IN, d, t_ns=t)
    return path


def lines_of(session):
    return [json.loads(ln) for ln in (session / "events.jsonl").read_text().splitlines()]


def rows_of(session):
    head, *rows = (session / "transcript.tsv").read_text().splitlines()
    return [dict(zip(head.split("\t"), r.split("\t"), strict=True)) for r in rows]


def chain(out):
    """The run's sessions in order, by the rotate lines that link them."""
    (first,) = [d for d in out.iterdir() if "prev_dir" not in meta_of(d)]
    sessions = [first]
    while True:
        last = lines_of(sessions[-1])[-1]
        if last["type"] != "rotate":
            break
        assert last["next_events"] == os.path.join(last["next_dir"], "events.jsonl")
        sessions.append(out / os.path.basename(last["next_dir"]))
    assert sorted(sessions) == sorted(out.iterdir())
    return sessions


def meta_of(session):
    return json.loads((session / "meta.json").read_text())


def wavs(session):
    a = session / "audio"
    return sorted("audio/" + n for n in os.listdir(a)) if a.exists() else []


# ------------------------------------------------------------------ the schedule
@pytest.mark.parametrize(
    "argv, why",
    [
        (["--rotate", "0 6 * * * *"], "has 6 fields; it wants 5"),
        (["--rotate", "0 6 * *"], "has 4 fields; it wants 5"),
        (["--rotate", "0 0 */31 * 1#5"], "never fires"),
        (["--rotate", "0 0 30 2 *"], "Bad day-of-month"),
        (["--rotate", "60 * * * *"], "Bad minute"),
        (["--attach", "/tmp/x.sock", "--rotate", "0 6 * * *"], "--rotate is the engine's"),
    ],
)
def test_a_schedule_cron_would_misread_or_never_fire_is_refused_at_the_start(argv, why, capsys):
    """cronsim takes a sixth field for seconds ('0 6 * * * *' is 00:06), and
    gives up on a schedule only after searching 50 years."""
    if "--attach" not in argv:
        argv = ["--replay", "x.tvd"] + argv
    with pytest.raises(SystemExit) as e:
        cli.parse_args(argv)
    assert e.value.code == 2
    assert why in capsys.readouterr().err


def test_a_period_opens_once_at_its_fire_time_and_one_without_records_is_skipped():
    tz = ZoneInfo("Asia/Tokyo")

    def t(*a):
        return int(datetime(*a, tzinfo=tz).timestamp() * NS)

    s = rotate.Schedule("0 6 * * *", tz, t(2025, 10, 7))
    assert s.due(t(2025, 10, 7, 5, 0)) is None  # the first record starts the schedule
    assert s.due(t(2025, 10, 7, 5, 59, 59, 999999)) is None
    assert s.due(t(2025, 10, 7, 6, 0)) == t(2025, 10, 7, 6, 0)
    assert s.due(t(2025, 10, 7, 6, 0) + 1) is None
    # three days without a record: one new session, of the period the record is in
    assert s.due(t(2025, 10, 10, 7, 0)) == t(2025, 10, 10, 6, 0)
    assert s.due(t(2025, 10, 10, 23, 0)) is None


def test_a_wall_time_dst_skips_runs_once_and_one_it_repeats_runs_once_too():
    """Debian cron: 02:30 that does not exist runs right after the jump,
    02:30 that happens twice runs at the first."""
    s = rotate.Schedule("30 2 * * *", ZoneInfo("Europe/Berlin"), 0)

    def u(*a):
        return int(datetime(*a, tzinfo=timezone.utc).timestamp() * NS)

    assert s.due(u(2026, 3, 28, 12, 0)) is None
    assert s.due(u(2026, 3, 29, 1, 0)) == u(2026, 3, 29, 1, 0)  # 03:00 CEST
    assert s.due(u(2026, 10, 24, 12, 0)) == u(2026, 10, 24, 0, 30)
    assert s.due(u(2026, 10, 25, 0, 30)) == u(2026, 10, 25, 0, 30)  # 02:30 CEST
    assert s.due(u(2026, 10, 25, 1, 30)) is None  # 02:30 CET, the same wall time again


# ------------------------------------------------------------------ a replay
def replay(rec, out, *extra):
    args = cli.parse_args(
        ["--replay", rec, "--speed", "0", "--json", "--out", str(out), "--events", "all", *extra]
    )
    assert App(args).run() == 0
    return chain(out)


@pytest.mark.parametrize("asr", [False, True])
def test_a_rotation_takes_along_the_voice_that_has_no_line_yet(rec, tmp_path, monkeypatch, asr):
    """Without a recognizer alice's line is written when she stops, before the
    minute; with one her text is still being waited for at the minute, so her
    line, row and WAV go to the new session. Bob talks across the minute: all
    of him is in the new one, from his real start."""
    ready = []  # what the new directory holds when the old events.jsonl names it
    event = App.event

    def watched(app, tag, *rest):
        if tag == "rotate":
            ready.append(sorted(os.listdir(rest[-1]["extra"]["next_dir"])))
        return event(app, tag, *rest)

    monkeypatch.setattr(App, "event", watched)
    extra = []
    if asr:
        engine = Engine()
        monkeypatch.setattr(recognizer, "build", lambda *a: engine)
        held_until_exit(engine, monkeypatch, 0.0)
        extra = ["--asr", "parakeet"]
    old, new = replay(rec, tmp_path / "o", "--rotate", EVERY_MINUTE, *extra)
    files = ["events.jsonl", "feed.log", "meta.json", "stderr.log", "transcript.tsv"]
    assert ready == [sorted(files + (["audio"] if asr else []))]
    a, b = lines_of(old), lines_of(new)
    assert [r["type"] for r in a].count("rotate") == [r["type"] for r in b].count("rotate") == 1
    assert (a[-1]["t_utc"], a[-1]["next_dir"]) == (iso(60), str(new))
    assert (b[0]["type"], b[0]["prev_dir"], b[0]["prev_events"]) == (
        "rotate",
        str(old),
        str(old / "events.jsonl"),
    )
    assert b[0]["version"] == meta_of(new)["version"]
    chats = {s.name: [r["text"] for r in lines_of(s) if r["type"] == "chat"] for s in (old, new)}
    assert chats == {old.name: ["before"], new.name: ["after"]}
    said = {
        s.name: [(r["nick"], r["t_utc"]) for r in lines_of(s) if r["type"] == "voice"]
        for s in (old, new)
    }
    alice, bob = ("alice", iso(55.06)), ("bob", iso(59.06))
    if asr:
        assert said == {old.name: [], new.name: [alice, bob]}
    else:
        assert said == {old.name: [alice], new.name: [bob]}
    for s in (old, new):
        rows = rows_of(s)
        assert [(r["nick"], r["t_start_utc"]) for r in rows] == said[s.name]
        assert sorted(r["audio"] for r in rows) == wavs(s)
    m_old, m_new = meta_of(old), meta_of(new)
    assert (m_old["quit"], m_old["next_dir"]) == ("rotated", str(new))
    assert (m_new["prev_dir"], m_new["rotated_utc"], m_new["quit"]) == (
        str(old),
        iso(60),
        "end of recording",
    )
    assert new.name.startswith("20251007T094100Z_replay-minute_")


def test_a_rotated_replay_is_the_unrotated_one_cut_in_pieces(rec, tmp_path):
    """Every line, row and WAV once, in the order of the run without
    rotation; stdout is the sessions' events.jsonl one after the other, and
    every line keeps to the schema."""
    with open(SCHEMA, encoding="utf-8") as f:
        schema = json.load(f)

    def run(out, *extra):
        cmd = [sys.executable, "-B", "-m", "stvwatch.cli", "--replay", rec, "--out", str(out)]
        cmd += ["--speed", "0", "--json", "--events", "all", "--tz", "Europe/Berlin", *extra]
        p = subprocess.run(cmd, capture_output=True, timeout=120)
        assert p.returncode == 0, p.stderr
        return p.stdout.decode().splitlines()

    def comparable(lines):
        out = []
        for r in map(json.loads, lines):
            if r["type"] == "rotate":
                continue
            r.pop("version", None)
            if r["type"] == "play":
                del r["t_utc"], r["t_local"]
            if r["type"] == "done":
                r["text"] = r["text"].split(" -> ")[0]
            out.append(r)
        return out

    stdout = run(tmp_path / "r", "--rotate", "* * * * *")
    sessions = chain(tmp_path / "r")
    assert len(sessions) == 2
    joined = [ln for s in sessions for ln in (s / "events.jsonl").read_text().splitlines()]
    assert stdout == joined
    assert [check(json.loads(ln), schema) for ln in stdout] == [[]] * len(stdout)
    whole = run(tmp_path / "w")
    (one,) = list((tmp_path / "w").iterdir())
    assert comparable(stdout) == comparable(whole)
    assert [r for s in sessions for r in rows_of(s)] == rows_of(one)

    def sums(s):
        return {w: hashlib.sha256((s / w).read_bytes()).hexdigest() for w in wavs(s)}

    assert {k: v for s in sessions for k, v in sums(s).items()} == sums(one)


# ------------------------------------------------------------------ live
class FakeClient:
    """The network client as the viewer starts it, writing a prepared journal
    in one go through the real rotation of net/main.py: its arguments are the
    ones the viewer passes; its log lines are written by hand, since the real
    one moves its own stdout."""

    def __init__(self, ip, port, dump_path, log_path, name="", duration_ms=0, extra=()):
        argv = ["run", "--dump", dump_path, "--ip", ip, "--port", str(port), *extra]
        self.a = netmain.parser().parse_args(argv)
        self.log_path = log_path
        self.end = 0.0

    def log(self, path, text):
        with open(path, "a") as f:
            f.write(text + "\n")

    def start(self):
        next_path, _ = netmain.rotation(self.a)
        logs = [self.log_path]
        self.log(logs[-1], "[tvdump] early")
        w = dump.DumpWriter(
            self.a.dump,
            "127.0.0.1:9",
            fsync_ms=0,
            next_path=next_path,
            on_next=lambda p: logs.append(os.path.join(os.path.dirname(p), self.a.rotate_log)),
        )
        with w:
            for t, d in timeline():
                w.write(dump.DATAGRAM_IN, d, t_ns=t)
        self.log(logs[-1], "[tvdump] late")
        self.end = time.monotonic() + 1.0
        return True

    def alive(self):
        return time.monotonic() < self.end

    def stop(self):
        return 0

    def cpu_s(self):
        return 0.0


def test_live_the_session_follows_the_network_client_into_its_new_directory(tmp_path, monkeypatch):
    """The client starts the new journal in a directory of its own; the
    viewer, reading the old journal to its NEXT_FILE record, goes on in that
    directory with every file of the session, the client's log too."""
    monkeypatch.setattr(source, "LiveClient", FakeClient)
    monkeypatch.setattr(App, "precheck", lambda app: "")
    out = tmp_path / "o"
    args = cli.parse_args(
        ["--relay", "127.0.0.1:9", "--json", "--events", "all", "--out", str(out)]
        + ["--rotate", EVERY_MINUTE, "--tz", "Asia/Tokyo"]
    )
    assert App(args).run() == 0
    old, new = chain(out)
    assert new.name == f"20251007T094100Z_127.0.0.1_9_{os.getpid()}"
    caps = [list(dump.DumpReader(str(s / "capture.tvd"))) for s in (old, new)]
    assert caps[0][-1][1:] == (
        dump.NEXT_FILE,
        json.dumps({"path": str(new / "capture.tvd")}).encode(),
    )
    assert caps[0][-1][0] == caps[1][0][0] == at(60)
    before = sum(1 for t, _d in timeline() if t < at(60))
    assert [len(c) for c in caps] == [before + 1, len(timeline()) - before]
    # the log is read on the clock, not in the journal's order
    seen = {
        s.name: sorted(r["text"] for r in lines_of(s) if r["type"] in ("chat", "tvd"))
        for s in (old, new)
    }
    assert seen == {old.name: ["[tvdump] early", "before"], new.name: ["[tvdump] late", "after"]}
    assert meta_of(new)["prev_dir"] == str(old) and meta_of(new)["quit"] == "client exited"
    # a replay of the first journal stops at its end, a follower goes on
    assert sum(1 for _ in Recording(str(old / "capture.tvd"))) == len(caps[0]) - 1
    follow = Recording(str(old / "capture.tvd"), follow=True, idle_s=0.2)
    assert [d.t_ns for d in follow] == [t for t, _d in timeline()]


def test_a_rotated_journal_never_opens_over_a_file_that_exists(tmp_path, capsys, monkeypatch):
    """A --rotate-dir without {stamp} names one directory for every period: it
    is refused at the start. A writer told to go on in a file that exists
    keeps the journal it has rather than truncating it."""
    monkeypatch.setattr(netmain, "cmd_run", lambda a: 0)
    argv = ["run", "--dump", str(tmp_path / "a.tvd"), "--ip", "127.0.0.1", "--port", "9"]
    with pytest.raises(SystemExit) as e:
        netmain.main(argv + ["--rotate", EVERY_MINUTE, "--rotate-dir", str(tmp_path / "d")])
    assert e.value.code == 2 and "--rotate-dir needs {stamp}" in capsys.readouterr().err
    path = str(tmp_path / "j.tvd")
    w = dump.DumpWriter(path, "127.0.0.1:9", fsync_ms=0, next_path=lambda t: path if t else None)
    w.write(dump.DATAGRAM_IN, b"kept", t_ns=0)
    with pytest.raises(dump.DumpError, match="File exists"):
        w.write(dump.DATAGRAM_IN, b"next", t_ns=at(1))
    w.close()
    assert [(t, d) for t, _r, d in dump.DumpReader(path)] == [(0, b"kept")]


def test_the_network_client_moves_its_log_into_the_new_directory(tmp_path):
    """Its stdout and stderr are the log file the viewer opened for it."""
    path = tmp_path / "new.log"
    code = (
        "import os, sys\n"
        "from stvwatch.net import main\n"
        "print('old')\n"
        f"main._log_to({str(path)!r})\n"
        "print('new')\n"
        "os.write(2, b'err\\n')\n"
    )
    p = subprocess.run([sys.executable, "-B", "-u", "-c", code], capture_output=True, timeout=30)
    assert (p.returncode, p.stdout, p.stderr) == (0, b"old\n", b"")
    assert path.read_text() == "new\nerr\n"
