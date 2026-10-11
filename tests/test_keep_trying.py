"""--keep-trying: a refused or dead network client is waited out inside the
viewer, the password is read again before every attempt, and a changed
password file brings the next attempt at once. Also the password store."""

import json
import os
import re
import signal
import subprocess

import pytest
from fakerelay import BAD_PASSWORD, FakeRelay
from test_ending import ENV, STV, app_for, done_of, lines_of, meta_of, run_with, sessions, until

from stvwatch import cli, passwords
from stvwatch.app import Backoff

SECRET = "s3cret-Pw7"


def test_backoff_doubles_the_same_refusal_up_to_30_minutes_and_client_deaths_up_to_5():
    b = Backoff()
    refused = [b.next("refused", "Bad spectator password") for _ in range(5)]
    assert refused == [300, 600, 1200, 1800, 1800]
    assert b.next("refused", "relay does not answer A2S_INFO") == 300
    assert [b.next("client", f"rc {n}") for n in range(7)] == [10, 20, 40, 80, 160, 300, 300]
    assert b.next("refused", "relay does not answer A2S_INFO") == 300
    b.next("refused", "relay does not answer A2S_INFO")
    b.reset()
    assert b.next("refused", "relay does not answer A2S_INFO") == 300


def store(tmp_path, text, mode=0o600):
    path = tmp_path / "cfg" / "stv-watch" / "passwords"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    os.write(fd, text.encode())
    os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return path


def test_the_store_is_read_by_relay_and_refused_when_others_may_read_it(tmp_path):
    path = store(
        tmp_path,
        "# relays\n\n  Relay.Example:027020   pass word  \n10.0.0.1:27020 x\n",
    )
    src = passwords.Source("relay.example:27020", store=str(path))
    assert src.get() == ("pass word", "store")
    assert passwords.Source("10.0.0.2:27020", store=str(path)).get() == ("", None)
    assert passwords.Source("10.0.0.1:27020", store=str(tmp_path / "none")).get() == ("", None)
    for bad, said in [
        ("10.0.0.1 x\n", "line 1: wants 'HOST:PORT PASSWORD'"),
        ("10.0.0.1:27020\n", "line 1: wants"),
        ("a:1 x\nA:1 y\n", "line 2: a:1 is given twice"),
    ]:
        store(tmp_path, bad)
        with pytest.raises(passwords.PasswordError, match=said):
            src.get()
    store(tmp_path, f"relay.example:27020 {SECRET}\n", mode=0o640)
    with pytest.raises(passwords.PasswordError, match="mode 0640") as e:
        src.get()
    assert SECRET not in str(e.value)


def test_the_first_source_given_wins(tmp_path):
    path = store(tmp_path, "r:1 from-store\n")
    pw = tmp_path / "pw"
    pw.write_text("from-file\n")
    get = passwords.Source
    assert get("r:1", str(pw), "from-env", str(path)).get() == ("from-file", "file")
    assert get("r:1", None, "from-env", str(path)).get() == ("from-env", "env")
    assert get("r:1", None, None, str(path)).get() == ("from-store", "store")
    # a source given is the one read: a broken store behind it does not matter
    store(tmp_path, "garbage\n", mode=0o644)
    assert get("r:1", str(pw), None, str(path)).get() == ("from-file", "file")
    assert get("r:1", None, "from-env", str(path)).get() == ("from-env", "env")


def test_an_insecure_store_stops_the_run_with_2_before_anything_starts(tmp_path):
    store(tmp_path, f"127.0.0.1:9 {SECRET}\n", mode=0o644)
    p = subprocess.run(
        STV + ["--relay", "127.0.0.1:9", "--out", str(tmp_path / "o")],
        capture_output=True,
        env=dict(ENV, XDG_CONFIG_HOME=str(tmp_path / "cfg")),
        timeout=60,
    )
    assert p.returncode == 2
    assert b"mode 0644 lets other users read or change it" in p.stderr
    assert SECRET.encode() not in p.stderr + p.stdout
    assert not (tmp_path / "o").exists()


def test_the_store_password_goes_only_to_a_relay_that_asks_and_into_no_file(tmp_path, monkeypatch):
    out = tmp_path / "o"
    with FakeRelay(password=SECRET) as r:
        store(tmp_path, f"{r.addr} {SECRET}\n")
        app = app_for(["--relay", r.addr, "--json", "--out", str(out)], monkeypatch, tmp_path)
        assert run_with(app, lambda: r.wait(lambda: r.connected() == 1)) == 0
        assert r.passwords == [SECRET]
    (s,) = sessions(out)
    assert [f for f in s.rglob("*") if f.is_file() and SECRET.encode() in f.read_bytes()] == []
    with FakeRelay() as r:
        store(tmp_path, f"{r.addr} {SECRET}\n")
        app = app_for(["--relay", r.addr, "--json", "--out", str(out)], monkeypatch, tmp_path)
        assert run_with(app, lambda: r.wait(lambda: r.connected() == 1)) == 0
        assert r.passwords == [""]


def wait_lines(out):
    return [x for s in sessions(out) for x in lines_of(s) if x["type"] == "wait"]


def test_a_killed_client_is_waited_out_and_started_again_in_a_new_session(tmp_path, monkeypatch):
    monkeypatch.setattr(Backoff, "CLIENT_S", 0.5)
    out = tmp_path / "o"
    with FakeRelay() as r:
        app = app_for(
            ["--relay", r.addr, "--json", "--keep-trying", "--out", str(out)], monkeypatch, tmp_path
        )

        def drive():
            assert r.wait(lambda: r.connected() == 1)
            os.kill(app.client.pid, signal.SIGKILL)
            assert r.wait(lambda: r.connections == 2)

        assert run_with(app, drive) == 0
    first, second = sessions(out)
    (w,) = wait_lines(out)
    assert (w["quit"], w["class"], w["cause"], w["attempt"], w["retrying"]) == (
        "client died (rc -9)",
        "client",
        "killed by SIGKILL",
        1,
        True,
    )
    assert w["text"] == "client died (rc -9): killed by SIGKILL, next try in 0:00"
    last, head = lines_of(first)[-1], lines_of(second)[0]
    assert (last["type"], last["reason"], last["next_dir"]) == ("rotate", "restart", str(second))
    assert (head["type"], head["reason"], head["prev_dir"]) == ("rotate", "restart", str(first))
    assert (meta_of(first)["quit"], meta_of(second)["reason"]) == ("rotated", "restart")
    done = done_of(second)
    assert (done["quit"], done["class"], done["rc"], done["tvdump_rc"]) == ("SIGTERM", "ok", 0, 0)
    assert (first / "capture.tvd").exists() and (second / "capture.tvd").exists()


@pytest.mark.parametrize("via", ["file", "store"])
def test_a_password_set_mid_run_is_waited_out_until_the_file_changes(tmp_path, monkeypatch, via):
    """The relay takes a password while we are in and drops us (a map
    change): the reconnect is refused, the viewer waits; the password written
    to the file brings the next attempt at once, long before the wait ends."""
    monkeypatch.setattr(Backoff, "REFUSED_S", 600.0)
    out = tmp_path / "o"
    pw = tmp_path / "pw"
    pw.write_text("old\n")
    with FakeRelay(password="old" if via == "file" else None) as r:
        extra = ["--password-file", str(pw)] if via == "file" else []
        app = app_for(
            ["--relay", r.addr, "--json", "--keep-trying", "--out", str(out), *extra],
            monkeypatch,
            tmp_path,
        )

        def drive():
            assert r.wait(lambda: r.connected() == 1)
            r.password = SECRET
            r.drop()
            assert until(lambda: wait_lines(out))
            if via == "file":
                pw.write_text(SECRET + "\n")
            else:
                store(tmp_path, f"{r.addr} {SECRET}\n")
            assert r.wait(lambda: r.connected() == 1 and r.passwords[-1] == SECRET)

        assert run_with(app, drive) == 0
        before = "old" if via == "file" else ""
        assert r.passwords == [before, before, SECRET]
    (w,) = wait_lines(out)
    assert (w["quit"], w["class"], w["cause"]) == ("alarm", "refused", BAD_PASSWORD)
    assert w["text"] == f"refused: {BAD_PASSWORD}, next try in 10:00"
    texts = [x["text"] for s in sessions(out) for x in lines_of(s)]
    assert "password file changed: trying now" in texts
    assert len(sessions(out)) == 2
    for s in sessions(out):
        assert [f for f in s.rglob("*") if f.is_file() and SECRET.encode() in f.read_bytes()] == []


def test_a_precheck_refusal_is_waited_out_and_without_the_flag_ends_with_3(tmp_path, monkeypatch):
    """No password for a relay that announces one: without --keep-trying the
    run ends; with it the viewer waits, and the store written meanwhile lets
    the next attempt through, in the same session (no client ran in it)."""
    monkeypatch.setattr(Backoff, "REFUSED_S", 600.0)
    with FakeRelay(password=SECRET) as r:
        once = app_for(
            ["--relay", r.addr, "--json", "--out", str(tmp_path / "a")], monkeypatch, tmp_path
        )
        assert once.run() == 3
        (s,) = sessions(tmp_path / "a")
        done = done_of(s)
        assert (done["quit"], done["class"], done["rc"]) == ("precheck", "refused", 3)
        assert done["cause"] == "relay has tv_password, none given"
        out = tmp_path / "b"
        app = app_for(
            ["--relay", r.addr, "--json", "--keep-trying", "--out", str(out)], monkeypatch, tmp_path
        )

        def drive():
            assert until(lambda: wait_lines(out))
            store(tmp_path, f"{r.addr} {SECRET}\n")
            assert r.wait(lambda: r.connected() == 1)

        assert run_with(app, drive) == 0
        assert r.passwords == [SECRET]
    (w,) = wait_lines(out)
    assert (w["quit"], w["class"], w["cause"]) == (
        "precheck",
        "refused",
        "relay has tv_password, none given",
    )
    (s,) = sessions(out)
    assert done_of(s)["quit"] == "SIGTERM"


def test_keep_trying_is_for_a_relay(capsys):
    with pytest.raises(SystemExit) as e:
        cli.parse_args(["--replay", "x.tvd", "--keep-trying"])
    assert e.value.code == 2 and "--keep-trying is for --relay" in capsys.readouterr().err


def test_a_wait_shows_its_countdown_on_the_screen(tmp_path, monkeypatch):
    with FakeRelay(password=SECRET) as r:
        app = app_for(
            ["--relay", r.addr, "--keep-trying", "--plain", "--out", str(tmp_path / "o")],
            monkeypatch,
            tmp_path,
        )
        shown = []

        def drive():
            assert until(lambda: app.waiting is not None)
            shown.append("".join(t for t, _s in app.block()[1]))

        assert run_with(app, drive) == 0
    assert re.search(r"refused: relay has tv_password, none given, next try in [45]:\d\d", shown[0])


def test_the_cli_runs_keep_trying_end_to_end(tmp_path):
    """The flag through the command line, stopped by a signal while it waits."""
    with FakeRelay(password=SECRET) as r:
        p = subprocess.Popen(
            STV + ["--relay", r.addr, "--json", "--keep-trying", "--out", str(tmp_path / "o")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(ENV, XDG_CONFIG_HOME=str(tmp_path / "cfg")),
        )
        try:
            assert until(lambda: (tmp_path / "o").exists() and wait_lines(tmp_path / "o"))
            p.send_signal(signal.SIGTERM)
            out, _err = p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
    assert p.returncode == 0
    last = json.loads(out.decode().splitlines()[-1])
    assert (last["type"], last["quit"], last["class"]) == ("done", "SIGTERM", "ok")
