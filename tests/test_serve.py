"""--serve and --attach: a headless engine's screen on a Unix socket, drawn by
any number of clients the way a local run draws it."""

import io
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

import pytest
from voicegen import demo

from stvwatch import cli, serve
from stvwatch.app import App
from stvwatch.cli import parse_args
from stvwatch.render import Screen

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, "data", "sample.tvd")
SIZE = (100, 300)  # rows enough that no line leaves the feed region


class FakeTty(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        raise OSError("no fd")


def tty_screen():
    return Screen(out=FakeTty(), color=False, size=lambda: SIZE)


@pytest.fixture
def sock():
    """A socket path short enough for AF_UNIX, whatever pytest's tmp_path."""
    d = tempfile.mkdtemp(prefix="sw", dir="/tmp")
    yield os.path.join(d, "s")
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def voice(tmp_path_factory):
    rec = str(tmp_path_factory.mktemp("rec") / "voice.tvd")
    demo(rec)
    return rec


def until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


class Client:
    """An --attach screen stepping in a thread of its own."""

    def __init__(self, path):
        self.c = serve.Attached(path, tty_screen())
        self.stop = threading.Event()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while not self.stop.is_set():
            self.c.step(0.01)

    def finish(self):
        """After the engine's bye and the end of its socket."""
        until(lambda: self.c.ended and self.c.sock is None)
        self.stop.set()
        self.t.join(5)
        return self.c


def normal(lines, session):
    """Screen lines with the run's own parts out: the session directory and
    the wall clock time of the replay's first line."""
    out = []
    for spans in lines:
        spans = [(t.replace(session, "DIR"), s) for t, s in spans]
        if len(spans) > 1 and spans[1] == ("play  ", "blue"):
            spans[0] = ("T ", "dim")
        out.append(spans)
    return out


def session_of(out):
    (d,) = list(out.iterdir())
    return str(d)


def screen_lines(sc):
    assert all(key is None for key, _spans in sc.lines), "a live line left open"
    return [spans for _key, spans in sc.lines]


class Recorded(Screen):
    """A local terminal screen that keeps the block it was stopped with."""

    def start(self, stderr_path=None):
        super().start(None)

    def stop(self, final_block=()):
        self.final = list(final_block)
        super().stop(final_block)


@pytest.fixture(scope="module")
def local(voice, tmp_path_factory):
    """The local screen of the voice replay: its lines and final block."""
    out = tmp_path_factory.mktemp("local")
    app = App(parse_args(["--replay", voice, "--speed", "0", "--events", "all", "--out", str(out)]))
    sc = app.screen = Recorded(out=FakeTty(), color=False, size=lambda: SIZE)
    assert app.run() == 0
    s = session_of(out)
    return normal(screen_lines(sc), s), normal(sc.final, s)


def served_app(voice, out, path, speed="0"):
    args = ["--replay", voice, "--speed", speed, "--events", "all", "--out", str(out)]
    return App(parse_args(args + ["--serve", path]))


def hold_start(monkeypatch, clients):
    """The engine starts reading once `clients` screens are connected."""
    start = serve.Server.start

    def gated(server):
        start(server)
        until(lambda: server.count() >= clients)

    monkeypatch.setattr(serve.Server, "start", gated)


def test_two_attached_screens_draw_what_the_local_screen_draws(
    voice, local, tmp_path, sock, monkeypatch
):
    hold_start(monkeypatch, 2)
    a, b = Client(sock), Client(sock)
    assert served_app(voice, tmp_path, sock).run() == 0
    s = session_of(tmp_path)
    lines, final = local
    for c in (a.finish(), b.finish()):
        assert normal(screen_lines(c.screen), s) == lines
        assert normal(c.block, s) == final
    assert not os.path.exists(sock)


def test_a_late_screen_gets_the_history_open_live_lines_and_block_on_connect(
    voice, local, tmp_path, sock, monkeypatch
):
    """Connected while alice talks and bob's line has closed: the snapshot
    holds bob's final text where his line was and alice's latest progress."""
    hold_start(monkeypatch, 1)
    early, late = Client(sock), []
    draw = serve.ServedScreen.draw

    def gate(screen, block):
        draw(screen, block)
        srv = screen.server
        closed = any(e.get("key") is not None and e["op"] == "line" for e in srv.ring)
        if not late and srv.open and closed:
            late.append(Client(sock))
            until(lambda: late[0].c.block is not None and early.c.block == srv.last["block"])
            e, lt = early.c, late[0].c
            assert lt.open == e.open and lt.block == e.block
            assert "talking 0.0s" not in "".join(t for t, _s in list(e.open.values())[0])

    monkeypatch.setattr(serve.ServedScreen, "draw", gate)
    assert served_app(voice, tmp_path, sock, speed="4").run() == 0
    assert late, "the replay never had an open line after a closed one"
    e, lt = early.finish(), late[0].finish()
    assert screen_lines(lt.screen) == screen_lines(e.screen)
    assert len(screen_lines(e.screen)) == len(local[0])


def served_screen(path, engine="e1"):
    sc = serve.ServedScreen(path, {"pid": 1, "version": "v", "tz": "UTC", "engine": engine})
    sc.start()
    return sc


def texts(sc):
    return ["".join(t for t, _s in spans) for _key, spans in sc.lines]


def test_a_screen_that_stops_reading_is_dropped_and_gets_what_it_missed_once(sock, monkeypatch):
    monkeypatch.setattr(serve, "CLIENT_BUF", 1 << 16)
    eng = served_screen(sock)
    c = serve.Attached(sock, tty_screen())
    assert c.connect()
    eng.feed([("first", "")])
    eng.live_open("k", [("k talking", "")])
    eng.draw([[("block", "")]])
    until(lambda: (c.step(0.01), c.open)[1])
    # the screen stops reading; the engine goes on and never waits for it
    slowest = 0.0
    for _ in range(400):
        t = time.monotonic()
        eng.draw([[("x" * 4000, "")]])
        slowest = max(slowest, time.monotonic() - t)
    assert eng.server.dropped == 1 and slowest < 0.05
    eng.live_close("k", [("k said", "")])
    eng.feed([("second", "")])
    eng.draw([[("block", "")]])
    until(lambda: (c.step(0.01), "second" in texts(c.screen))[1])
    shown = [t[13:] if " attach " in t else t for t in texts(c.screen)]
    assert shown == [
        "first",
        "k said",
        f"attach reconnected to {sock} (engine pid 1)",
        "second",
    ]
    eng.stop()
    c.close()


def test_a_killed_screen_does_not_stall_the_engine(sock):
    eng = served_screen(sock)
    p = subprocess.Popen(
        [sys.executable, "-B", "-m", "stvwatch.cli", "--attach", sock, "--plain"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        until(lambda: eng.server.count() == 1)
        eng.feed([("before", "")])
        eng.draw([[("block", "")]])
        p.kill()
        p.wait(5)
        slowest = 0.0
        for n in range(200):
            t = time.monotonic()
            eng.feed([(f"line {n}", "")])
            eng.draw([[("y" * 2000, "")]])
            slowest = max(slowest, time.monotonic() - t)
            time.sleep(0.002)
        until(lambda: eng.server.count() == 0)
        assert slowest < 0.05
    finally:
        p.kill()
        eng.stop()


def test_a_restarted_engine_is_found_again_and_named_in_one_line(voice, tmp_path, sock):
    """The first engine dies by SIGKILL while alice talks, leaving its socket
    file; the next one takes the path over and the screen comes back to it."""
    run = [sys.executable, "-B", "-m", "stvwatch.cli", "--replay", voice, "--out", str(tmp_path)]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    e1 = subprocess.Popen(run + ["--speed", "1", "--serve", sock], env=env)
    try:
        until(lambda: os.path.exists(sock))
        cl = subprocess.Popen(
            [sys.executable, "-B", "-m", "stvwatch.cli", "--attach", sock, "--plain"],
            stdout=subprocess.PIPE,
            env=env,
        )
        time.sleep(2.5)  # alice talks from 1.0 to 5.0 s of the recording
        e1.kill()
        e1.wait(5)
        assert stat.S_ISSOCK(os.lstat(sock).st_mode)  # stale, left behind
        e2 = subprocess.run(run + ["--speed", "4", "--serve", sock], env=env, timeout=60)
        assert e2.returncode == 0
        time.sleep(0.5)
        cl.send_signal(signal.SIGTERM)
        out = cl.communicate(timeout=10)[0].decode().splitlines()
        assert cl.returncode == 0
    finally:
        for p in (e1, cl):
            p.kill()
    notes = [ln[13:] for ln in out if ln[13:20] == "attach "]
    assert notes[0] == f"attach attached to {sock} (engine pid {e1.pid})"
    assert notes[1] == "attach engine gone, waiting for it"
    assert notes[2].startswith(f"attach engine restarted: pid {e1.pid} -> ")
    assert notes[3] == "attach engine ended, waiting for it"
    gone = out.index(next(ln for ln in out if ln.endswith("engine gone, waiting for it")))
    back = out.index(next(ln for ln in out if " engine restarted: " in ln))
    # alice's line of the dead engine stays as it was last seen
    frozen = [ln[13:] for ln in out[gone + 1 : back]]
    assert "voice alice" in " ".join(frozen)
    assert frozen and all(t.startswith("voice ") and " talking " in t for t in frozen)
    second = [ln[13:] for ln in out[back + 1 :] if " attach " not in ln]
    assert second[0].startswith("play  replay ") and second[-1].startswith("done  end of recording")
    assert sum(ln.startswith("voice ") for ln in second) == 3


@pytest.mark.parametrize("left", ["nothing", "stale", "live", "file"])
def test_serve_takes_a_path_only_from_a_dead_engine(sock, left):
    other = None
    if left == "stale":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(sock)
        s.close()
    elif left == "live":
        other = serve.listen(sock)
    elif left == "file":
        with open(sock, "w") as f:
            f.write("keep")
    try:
        if left in ("live", "file"):
            with pytest.raises(serve.ServeError):
                serve.listen(sock)
            assert left == "live" or open(sock).read() == "keep"
            return
        serve.listen(sock).close()
        st = os.stat(sock)
        assert stat.S_ISSOCK(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600
    finally:
        if other is not None:
            other.close()


def test_a_bare_name_is_a_socket_in_the_runtime_dir_made_private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = serve.socket_path("noob")
    assert path == str(tmp_path / "stv-watch" / "noob")
    serve.listen(path).close()
    assert stat.S_IMODE(os.stat(tmp_path / "stv-watch").st_mode) == 0o700
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    with pytest.raises(serve.ServeError):
        serve.socket_path("noob")


def test_the_command_line_keeps_the_two_sides_apart(sock, capsys):
    for args in (["--serve", sock, "--json"], ["--attach", sock, "--json"]):
        with pytest.raises(SystemExit):
            parse_args((["--replay", SAMPLE] if "--serve" in args else []) + args)
    assert cli.main(["--attach", sock]) == 2
    assert "no engine serves" in capsys.readouterr().err


def test_q_closes_the_attached_screen(sock):
    eng = served_screen(sock)
    sc = tty_screen()
    sc.key = lambda: "q"
    c = serve.Attached(sock, sc)
    assert c.connect()
    c.step(0.01)
    assert c.quit and eng.server.count() == 1
    c.close()
    eng.stop()


def test_attached_plain_screen_prints_the_lines_of_a_local_plain_run(sock, tmp_path, monkeypatch):
    """The sample through --serve and an --attach --plain process."""
    hold_start(monkeypatch, 1)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    attach = [sys.executable, "-B", "-m", "stvwatch.cli", "--attach", sock, "--plain"]
    attach += ["--status-every-ms", "600000"]
    cl = []
    listen = serve.Server.__init__

    def bound(server, *a):
        listen(server, *a)
        cl.append(subprocess.Popen(attach, stdout=subprocess.PIPE, env=env))

    monkeypatch.setattr(serve.Server, "__init__", bound)
    common = ["--replay", SAMPLE, "--speed", "0", "--events", "all", "--debug"]
    assert cli.main(common + ["--out", str(tmp_path / "s"), "--serve", sock]) == 0
    time.sleep(0.3)
    cl[0].send_signal(signal.SIGTERM)
    got = cl[0].communicate(timeout=10)[0].decode().splitlines()
    ref = (
        subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "stvwatch.cli",
                *common,
                "--plain",
                "--out",
                str(tmp_path / "l"),
            ],
            capture_output=True,
            env=env,
            timeout=60,
        )
        .stdout.decode()
        .splitlines()
    )

    def strip(lines):
        out = []
        for ln in lines:
            if ln.startswith("status ") or " attach " in ln:
                continue
            ln = ln.split(" -> ")[0] if " done " in ln else ln
            out.append(ln[13:] if " play " in ln else ln)
        return out

    assert strip(got) == strip(ref)
    assert any(ln.startswith("status REPLAY sample.tvd") for ln in got)
    assert [ln[13:] for ln in got if " attach " in ln][0].startswith("attach attached to ")
