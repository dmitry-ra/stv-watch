"""How a run ends: one exit code per class (README, "Exit codes") and a last
line `done` that says the same in fields, on every path that can still write."""

import json
import os
import resource
import signal
import subprocess
import sys
import threading
import time

import pytest
from fakerelay import FakeRelay
from test_sample import SAMPLE, SCHEMA, check

from stvwatch import cli, source
from stvwatch.app import App, ReaderFailed
from stvwatch.net import dump
from stvwatch.net import main as netmain

STV = [sys.executable, "-B", "-m", "stvwatch.cli"]
ENV = {k: v for k, v in os.environ.items() if k != "STV_WATCH_PASSWORD"}
ENV["PYTHONDONTWRITEBYTECODE"] = "1"


def sessions(out):
    return sorted(p for p in out.iterdir() if p.is_dir())


def lines_of(session):
    return [json.loads(ln) for ln in (session / "events.jsonl").read_text().splitlines()]


def done_of(session):
    last = lines_of(session)[-1]
    assert last["type"] == "done"
    with open(SCHEMA, encoding="utf-8") as f:
        assert check(last, json.load(f)) == []
    return last


def fields(done):
    return {k: done[k] for k in ("quit", "class", "cause", "rc", "retrying", "tvdump_rc")}


def meta_of(session):
    return json.loads((session / "meta.json").read_text())


def app_for(argv, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    a = cli.parse_args(argv)
    return App(a, passwords=cli.password_source(a))


def run_with(app, drive, stop=True):
    """app.run() here (signals need the main thread), `drive` beside it; a
    failing `drive` still stops the run."""
    err = []

    def go():
        try:
            drive()
        except BaseException as e:  # noqa: BLE001
            err.append(e)
        finally:
            if stop or err:
                app.ask_stop("SIGTERM")

    th = threading.Thread(target=go, daemon=True)
    th.start()
    rc = app.run()
    th.join(60)
    if err:
        raise err[0]
    return rc


def until(cond, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_a_replay_that_ends_says_so_in_fields(tmp_path):
    p = subprocess.run(
        STV + ["--replay", SAMPLE, "--speed", "0", "--json", "--out", str(tmp_path)],
        capture_output=True,
        env=ENV,
        timeout=60,
    )
    assert p.returncode == 0
    (s,) = sessions(tmp_path)
    assert fields(done_of(s)) == {
        "quit": "end of recording",
        "class": "ok",
        "cause": "",
        "rc": 0,
        "retrying": False,
        "tvdump_rc": None,
    }
    meta = meta_of(s)
    assert (meta["quit"], meta["class"], meta["cause"], meta["rc"]) == (
        "end of recording",
        "ok",
        "",
        0,
    )
    assert not os.path.exists(s / "meta.json.tmp")


def test_a_bug_ends_with_1_a_done_line_and_its_traceback_in_the_session(
    tmp_path, monkeypatch, capfd
):
    monkeypatch.setattr(
        App, "handle_dg", lambda app, dg: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    app = app_for(
        ["--replay", SAMPLE, "--speed", "0", "--json", "--out", str(tmp_path)],
        monkeypatch,
        tmp_path,
    )
    with pytest.raises(RuntimeError, match="boom"):
        app.run()
    (s,) = sessions(tmp_path)
    assert fields(done_of(s)) == {
        "quit": "crash",
        "class": "bug",
        "cause": "RuntimeError: boom",
        "rc": 1,
        "retrying": False,
        "tvdump_rc": None,
    }
    assert meta_of(s)["class"] == "bug"
    err = (s / "stderr.log").read_text()
    assert "Traceback" in err and "RuntimeError: boom" in err


def test_a_bug_while_winding_down_still_writes_done(tmp_path, monkeypatch):
    app = app_for(
        ["--replay", SAMPLE, "--speed", "0", "--json", "--out", str(tmp_path)],
        monkeypatch,
        tmp_path,
    )
    monkeypatch.setattr(app.seg, "flush", lambda now: (_ for _ in ()).throw(RuntimeError("late")))
    with pytest.raises(RuntimeError, match="late"):
        app.run()
    (s,) = sessions(tmp_path)
    done = done_of(s)
    assert (done["quit"], done["class"], done["cause"], done["rc"]) == (
        "crash",
        "bug",
        "RuntimeError: late",
        1,
    )
    assert (meta_of(s)["class"], meta_of(s)["rc"]) == ("bug", 1)


def test_a_reader_that_fails_is_a_bug_not_the_end_of_the_recording(tmp_path, monkeypatch):
    """A journal whose NEXT_FILE record names a file that is not there: the
    reader thread stops on it."""
    path = str(tmp_path / "j.tvd")
    w = dump.DumpWriter(path, "127.0.0.1:9", fsync_ms=0)
    w.write(dump.DATAGRAM_IN, b"\xff\xff\xff\xffx", t_ns=10**18)
    data = json.dumps({"path": str(tmp_path / "gone.tvd")}).encode()
    w.write(dump.NEXT_FILE, data, t_ns=10**18 + 1)
    w.close()
    out = tmp_path / "o"
    app = app_for(["--follow", path, "--json", "--out", str(out)], monkeypatch, tmp_path)
    with pytest.raises(ReaderFailed):
        app.run()
    (s,) = sessions(out)
    done = done_of(s)
    assert (done["class"], done["rc"]) == ("bug", 1)
    assert done["cause"].startswith("reader: RecordingError: next file of a rotated recording")


@pytest.mark.parametrize(
    "argv,said",
    [
        (["--replay", "missing.tvd"], "--replay: No such file or directory: missing.tvd"),
        (["--follow", __file__], "--follow: "),
    ],
)
def test_a_recording_that_cannot_be_read_is_a_usage_error_before_any_session(
    tmp_path, monkeypatch, capsys, argv, said
):
    monkeypatch.chdir(tmp_path)
    assert cli.main(argv + ["--out", str(tmp_path / "o")]) == 2
    assert said in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_a_session_that_cannot_be_made_is_5_without_a_traceback(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    p = subprocess.run(
        STV + ["--replay", SAMPLE, "--speed", "0", "--json", "--out", str(blocker / "o")],
        capture_output=True,
        env=ENV,
        timeout=60,
    )
    assert p.returncode == 5
    assert b"cannot write the session under" in p.stderr and b"Traceback" not in p.stderr


def limited(nbytes):
    return lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (nbytes, nbytes))


def test_a_session_file_that_cannot_be_written_is_5_and_meta_stays_json(tmp_path):
    """RLIMIT_FSIZE stands for a full disk: events.jsonl outgrows it first."""
    p = subprocess.run(
        STV
        + ["--replay", SAMPLE, "--speed", "0", "--json", "--events", "all", "--debug"]
        + ["--out", str(tmp_path)],
        capture_output=True,
        env=ENV,
        timeout=60,
        preexec_fn=limited(2000),
    )
    assert p.returncode == 5, p.stderr
    last = json.loads(p.stdout.decode().splitlines()[-1])
    assert (last["type"], last["class"], last["rc"], last["quit"]) == (
        "done",
        "io",
        5,
        "write error",
    )
    assert "File too large" in last["cause"]
    (s,) = sessions(tmp_path)
    # whole: the one written at the start; the final one did not fit
    assert "end_utc" not in meta_of(s)
    assert not os.path.exists(s / "meta.json.tmp")
    assert b"Traceback" not in p.stderr


def watch(relay, out, *extra, preexec_fn=None):
    env = dict(ENV, XDG_CONFIG_HOME=str(out) + "-cfg")
    return subprocess.Popen(
        STV + ["--relay", relay.addr, "--json", "--events", "all", "--out", str(out), *extra],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        preexec_fn=preexec_fn,
    )


def child_of(pid):
    with open(f"/proc/{pid}/task/{pid}/children") as f:
        (kid,) = [int(k) for k in f.read().split()]
    return kid


@pytest.mark.parametrize(
    "sig,quit,cause",
    [
        (signal.SIGKILL, "client died (rc -9)", "killed by SIGKILL"),
        (signal.SIGTERM, "client died (rc 0)", "stopped from outside (exit code 0)"),
    ],
)
def test_a_network_client_killed_from_outside_is_4(tmp_path, sig, quit, cause):
    with FakeRelay() as r:
        p = watch(r, tmp_path)
        try:
            assert r.wait(lambda: r.connected() == 1)
            os.kill(child_of(p.pid), sig)
            out, _err = p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
    assert p.returncode == 4
    (s,) = sessions(tmp_path)
    got = fields(done_of(s))
    assert (got["quit"], got["class"], got["cause"], got["rc"]) == (quit, "client", cause, 4)
    assert json.loads(out.decode().splitlines()[-1])["class"] == "client"


def test_a_network_client_that_cannot_write_its_journal_is_5_and_says_why(tmp_path):
    with FakeRelay() as r:
        p = watch(r, tmp_path, preexec_fn=limited(8192))
        out, _err = p.communicate(timeout=60)
    assert p.returncode == 5
    (s,) = sessions(tmp_path)
    done = done_of(s)
    assert (done["quit"], done["class"], done["tvdump_rc"]) == ("client died (rc 5)", "io", 5)
    assert done["cause"].startswith("dump write failed:") and "File too large" in done["cause"]
    shown = [x for x in lines_of(s) if x["type"] == "tvd" and "write error" in x["text"]]
    assert len(shown) == 1


class CrashingClient:
    """A network client that dies of a bug, as net/main.py reports one."""

    def __init__(self, ip, port, dump_path, log_path, **_kw):
        self.dump_path, self.log_path = dump_path, log_path
        self.end = 0.0

    def start(self):
        dump.DumpWriter(self.dump_path, "127.0.0.1:9", fsync_ms=0).close()
        with open(self.log_path, "a") as f:
            f.write("Traceback (most recent call last):\nRuntimeError: boom\n")
            f.write("[tvdump] crash: RuntimeError: boom\n")
        self.end = time.monotonic() + 0.3
        return True

    def alive(self):
        return time.monotonic() < self.end

    def stop(self):
        return 1

    def cpu_s(self):
        return 0.0


def test_a_crashed_network_client_names_its_cause_on_screen_and_in_done(tmp_path, monkeypatch):
    monkeypatch.setattr(source, "LiveClient", CrashingClient)
    monkeypatch.setattr(App, "precheck", lambda app, have: "")
    monkeypatch.setattr(App, "a2s_poll", lambda app: None)
    app = app_for(
        ["--relay", "127.0.0.1:9", "--json", "--out", str(tmp_path)], monkeypatch, tmp_path
    )
    app.a2s_before = None
    assert app.run() == 4
    (s,) = sessions(tmp_path)
    assert fields(done_of(s)) == {
        "quit": "client died (rc 1)",
        "class": "client",
        "cause": "RuntimeError: boom",
        "rc": 4,
        "retrying": False,
        "tvdump_rc": 1,
    }
    tvd = [x["text"] for x in lines_of(s) if x["type"] == "tvd"]
    assert "[tvdump] crash: RuntimeError: boom" in tvd


@pytest.mark.parametrize(
    "err,rc,said",
    [
        (dump.DumpError("dump write failed: full"), 5, "write error: dump write failed: full"),
        (RuntimeError("boom"), 1, "crash: RuntimeError: boom"),
    ],
)
def test_the_network_client_reports_its_own_death(tmp_path, monkeypatch, capsys, err, rc, said):
    def run(sup):
        raise err

    monkeypatch.setattr(netmain.supervisor.Supervisor, "run", run)
    argv = ["run", "--dump", str(tmp_path / "a.tvd"), "--ip", "127.0.0.1", "--port", "9"]
    assert netmain.main(argv) == rc
    assert capsys.readouterr().out.splitlines()[-1] == "[tvdump] " + said


def test_a_stderr_log_that_cannot_be_opened_is_5_not_a_bug(tmp_path, monkeypatch):
    out = tmp_path / "out"
    app = app_for(
        ["--replay", SAMPLE, "--speed", "0", "--json", "--out", str(out)], monkeypatch, tmp_path
    )

    def start(screen, stderr_path=None):
        raise PermissionError(13, "Permission denied", stderr_path)

    monkeypatch.setattr(type(app.screen), "start", start)
    assert app.run() == 5
    # after a write error the session takes no more lines, done included
    assert app.outcome[0] == "io" and "stderr.log" in app.outcome[1]


def test_a_bad_password_source_stops_before_any_weights_are_fetched(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    fetched = []
    monkeypatch.setattr(cli.weights, "ensure", lambda *a, **k: fetched.append(a))
    argv = ["--relay", "192.0.2.1:27020", "--asr", "parakeet", "--out", str(tmp_path / "o")]
    assert cli.main(argv + ["--password-file", str(tmp_path / "missing")]) == 2
    assert fetched == [] and "password" in capsys.readouterr().err


def test_a_meta_json_that_fails_at_the_end_leaves_no_done_that_says_otherwise(
    tmp_path, monkeypatch
):
    out = tmp_path / "out"
    app = app_for(
        ["--replay", SAMPLE, "--speed", "0", "--json", "--out", str(out)], monkeypatch, tmp_path
    )
    real = App.write_meta

    def write_meta(self, meta):
        if "end_utc" in meta:
            self.io_failed(OSError(28, "No space left on device"), "meta.json")
            return
        real(self, meta)

    monkeypatch.setattr(App, "write_meta", write_meta)
    assert app.run() == 5
    (session,) = sessions(out)
    done = [x for x in lines_of(session) if x["type"] == "done"]
    assert all(fields(x)["rc"] == 5 for x in done)
