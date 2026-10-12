"""A relay with tv_password (fakerelay.py behaves as srcds 10889068 does):
refused before connecting when we have no password, joined with the right
one, a standing refusal with a wrong one; the password reaches the relay and
nothing else: no session file, no argv, no error message."""

import json
import os
import signal
import subprocess
import sys

import pytest
from fakerelay import BAD_PASSWORD, FakeRelay

from stvwatch import cli
from stvwatch.net import client
from stvwatch.stream.recording import C2S_CONNECT

STV = [sys.executable, "-B", "-m", "stvwatch.cli"]
ENV = {k: v for k, v in os.environ.items() if k != "STV_WATCH_PASSWORD"}
ENV["PYTHONDONTWRITEBYTECODE"] = "1"
SECRET = "s3cret-Pw7"


def watch(relay, out, *extra, env=None):
    return subprocess.Popen(
        STV + ["--relay", relay.addr, "--json", "--events", "all", "--out", str(out), *extra],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(ENV, **(env or {})),
    )


def lines(out):
    return [json.loads(ln) for ln in out.decode().splitlines()]


def session_files(out):
    (session,) = list(out.iterdir())
    return session, [os.path.join(d, f) for d, _, fs in os.walk(session) for f in fs]


def holding(path, needle):
    with open(path, "rb") as f:
        return needle.encode() in f.read()


def argv_of(pid):
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        return f.read()


def test_precheck_lets_a_password_relay_through_only_with_a_password(monkeypatch):
    ri = {
        "players": 0,
        "max_players": 8,
        "version": "10889068",
        "visibility": 1,
        "map": "m",
        "folder": "hl2mp",
    }
    monkeypatch.setattr(client, "safe_info", lambda a: ri)
    monkeypatch.setattr(client, "auth_proto", lambda a: 2)
    assert client.precheck("r:1")[:2] == (False, "password")
    ok, why, facts = client.precheck("r:1", have_password=True)
    assert (ok, why, facts["password"]) == (True, "", True)


def test_no_password_for_a_password_relay_is_refused_before_anything_is_sent(tmp_path):
    with FakeRelay(password=SECRET) as r:
        p = watch(r, tmp_path)
        out, _ = p.communicate(timeout=60)
        assert p.returncode == 3
        assert (r.challenges, r.connections) == (0, 0)
    (rec,) = [x for x in lines(out) if x["text"].startswith("refused:")]
    assert "tv_password" in rec["text"] and "--password-file" in rec["text"]


@pytest.mark.parametrize("via", ["file", "env"])
def test_the_right_password_joins_and_is_in_no_file_and_no_argv(tmp_path, via):
    out = tmp_path / "out"
    pw_file = tmp_path / "pw"
    pw_file.write_text(SECRET + "\n")
    extra, env = (["--password-file", str(pw_file)], None) if via == "file" else ([], None)
    if via == "env":
        env = {"STV_WATCH_PASSWORD": SECRET}
    with FakeRelay(password=SECRET) as r:
        p = watch(r, out, *extra, env=env)
        try:
            assert r.wait(lambda: r.connected() == 1)
            with open(f"/proc/{p.pid}/task/{p.pid}/children") as f:
                (kid,) = [int(k) for k in f.read().split()]
            assert SECRET.encode() not in argv_of(p.pid) + argv_of(kid)
            p.send_signal(signal.SIGINT)
            stdout, stderr = p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
        assert p.returncode == 0
        assert r.passwords == [SECRET] and r.refusals == 0
    assert SECRET.encode() not in stdout + stderr
    texts = [x["text"] for x in lines(stdout) if x["type"] == "conn"]
    assert any(t.startswith("connecting to") and "with a password" in t for t in texts)
    session, files = session_files(out)
    assert {"meta.json", "events.jsonl", "capture.tvd", "tvdump.log"} <= {
        os.path.basename(f) for f in files
    }
    assert [f for f in files if holding(f, SECRET)] == []
    # the connect request is still in the recording, with the field empty
    with open(session / "capture.tvd", "rb") as f:
        assert C2S_CONNECT in f.read()


@pytest.mark.parametrize("via", ["file", "env"])
def test_a_given_password_is_not_sent_to_a_relay_that_asks_for_none(tmp_path, via):
    pw_file = tmp_path / "pw"
    pw_file.write_text(SECRET + "\n")
    extra = ["--password-file", str(pw_file)] if via == "file" else []
    env = {"STV_WATCH_PASSWORD": SECRET} if via == "env" else None
    with FakeRelay() as r:
        p = watch(r, tmp_path / "out", *extra, env=env)
        try:
            assert r.wait(lambda: r.connected() == 1)
            p.send_signal(signal.SIGINT)
            p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
        assert p.returncode == 0
        assert r.passwords == [""]


def test_a_wrong_password_is_a_standing_refusal_that_ends_the_viewer(tmp_path):
    with FakeRelay(password="the-right-one") as r:
        p = watch(r, tmp_path, env={"STV_WATCH_PASSWORD": SECRET})
        out, err = p.communicate(timeout=60)
        assert p.returncode == 3
        assert r.passwords[0] == SECRET and r.connected() == 0
    texts = [x["text"] for x in lines(out)]
    assert f"[alarm] permanent failure: {BAD_PASSWORD}" in texts
    session, files = session_files(tmp_path)
    assert json.loads((session / "meta.json").read_text())["quit"] == "alarm"
    assert SECRET.encode() not in out + err
    assert [f for f in files if holding(f, SECRET)] == []


@pytest.mark.parametrize(
    "argv,env,content,said",
    [
        (["--replay", "x.tvd"], {}, SECRET, "is for --relay"),
        (["--relay", "1.2.3.4:27020"], {}, "\n" + SECRET, "first line is empty"),
        (["--relay", "1.2.3.4:27020"], {}, SECRET + "\0x", "NUL"),
        (["--relay", "1.2.3.4:27020"], {}, None, "No such file"),
    ],
)
def test_a_bad_password_option_stops_before_anything_starts(
    tmp_path, monkeypatch, capsys, argv, env, content, said
):
    pw = tmp_path / "pw"
    if content is not None:
        pw.write_text(content)
    monkeypatch.delenv("STV_WATCH_PASSWORD", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cli, "App", lambda *a, **kw: pytest.fail("got as far as App"))
    try:
        rc = cli.main(argv + ["--password-file", str(pw), "--out", str(tmp_path / "o")])
    except SystemExit as e:
        rc = e.code
    assert rc == 2
    err = capsys.readouterr().err
    assert said in err and SECRET not in err


def test_an_empty_password_in_the_environment_is_an_error(monkeypatch, capsys):
    monkeypatch.setenv("STV_WATCH_PASSWORD", "")
    monkeypatch.setattr(cli, "App", lambda *a, **kw: pytest.fail("got as far as App"))
    assert cli.main(["--relay", "1.2.3.4:27020"]) == 2
    assert "STV_WATCH_PASSWORD is set but empty" in capsys.readouterr().err
