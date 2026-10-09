"""Live mode against a loopback relay (fakerelay.py): every way out sends
net_Disconnect, a relay on an unknown build is refused before anything takes
a slot, a session cut during signon reconnects."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest
from fakerelay import FakeRelay
from test_events import run_on_tty

from stvwatch.net import dump as dumpfmt
from stvwatch.net import supervisor

STV = [sys.executable, "-B", "-m", "stvwatch.cli"]
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
LEAVE = "Disconnect by user."


def watch(relay, out, *extra):
    return subprocess.Popen(
        STV + ["--relay", relay.addr, "--json", "--events", "all", "--out", str(out), *extra],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=ENV,
    )


def meta_of(out):
    (session,) = list(out.iterdir())
    return json.loads((session / "meta.json").read_text())


def child_of(pid):
    with open(f"/proc/{pid}/task/{pid}/children") as f:
        kids = [int(k) for k in f.read().split()]
    assert len(kids) == 1
    return kids[0]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_to_the_viewer_leaves_the_relay_and_frees_the_slot(tmp_path, sig):
    with FakeRelay() as r:
        p = watch(r, tmp_path)
        try:
            assert r.wait(lambda: r.connected() == 1)
            p.send_signal(sig)
            out, _ = p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
        assert p.returncode == 0
        assert r.disconnects == [(1, LEAVE), (1, LEAVE)]
        assert r.connected() == 0
    recs = [json.loads(ln) for ln in out.decode().splitlines()]
    texts = [r["text"] for r in recs if r["type"] == "conn"]
    assert "left: net_Disconnect x2 (stop)" in texts
    assert any(t.startswith("slot check:") and t.endswith(" - released") for t in texts)
    meta = meta_of(tmp_path)
    assert meta["quit"] == signal.Signals(sig).name
    assert meta["tvdump_rc"] == 0 and meta["slot_released"] is True


def test_a_killed_viewer_still_leaves_the_relay(tmp_path):
    """SIGKILL gives the viewer no chance to clean up: the child gets SIGTERM
    from the kernel (PR_SET_PDEATHSIG) and leaves by itself."""
    with FakeRelay() as r:
        p = watch(r, tmp_path)
        try:
            assert r.wait(lambda: r.connected() == 1)
            child = child_of(p.pid)
            p.kill()
            p.wait(10)
            assert r.wait(lambda: len(r.disconnects) == 2)
        finally:
            if p.poll() is None:
                p.kill()
        end = time.monotonic() + 10
        while time.monotonic() < end and os.path.exists(f"/proc/{child}"):
            time.sleep(0.05)
        assert not os.path.exists(f"/proc/{child}")
        assert r.disconnects == [(1, LEAVE), (1, LEAVE)]


def test_q_on_the_normal_screen_leaves_the_relay(tmp_path):
    with FakeRelay() as r:
        cmd = STV + ["--relay", r.addr, "--out", str(tmp_path)]
        rc, drawn = run_on_tty(cmd, ENV, keys=b"q")
        assert rc == 0 and b"LIVE" in drawn
        assert r.disconnects == [(1, LEAVE), (1, LEAVE)]
    assert meta_of(tmp_path)["quit"] == "key q"


def test_the_client_alone_leaves_on_sigterm_and_at_its_deadline(tmp_path):
    with FakeRelay() as r:
        p = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "stvwatch.net.main",
                "run",
                "--dump",
                str(tmp_path / "a.tvd"),
                "--ip",
                "127.0.0.1",
                "--port",
                str(r.port),
            ],
            stdout=subprocess.DEVNULL,
            env=ENV,
        )
        assert r.wait(lambda: r.connected() == 1)
        p.send_signal(signal.SIGTERM)
        assert p.wait(30) == 0
        rc = subprocess.run(
            [
                sys.executable,
                "-m",
                "stvwatch.net.main",
                "run",
                "--dump",
                str(tmp_path / "b.tvd"),
                "--ip",
                "127.0.0.1",
                "--port",
                str(r.port),
                "--duration-ms",
                "1500",
            ],
            stdout=subprocess.DEVNULL,
            env=ENV,
            timeout=60,
        ).returncode
        assert rc == 0
        assert r.disconnects == [(1, LEAVE)] * 2 + [(2, LEAVE)] * 2
    leaves = [
        f for _t, t, f in dumpfmt.DumpReader(str(tmp_path / "b.tvd")).events() if t == dumpfmt.LEAVE
    ]
    assert leaves == [{"sent": 2, "why": "deadline"}]


def test_an_unknown_server_build_is_refused_before_taking_a_slot(tmp_path):
    with FakeRelay(version="12345") as r:
        p = watch(r, tmp_path)
        out, _ = p.communicate(timeout=60)
        assert p.returncode == 2
        assert (r.challenges, r.connections) == (0, 0)
    (rec,) = [json.loads(ln) for ln in out.decode().splitlines() if '"refused' in ln]
    assert "server build 12345" in rec["text"] and "tools/read_crc.py" in rec["text"]
    assert "docs/protocol.md" in rec["text"]


def test_a_standing_refusal_ends_the_viewer_with_exit_code_3(tmp_path):
    """The relay refuses the connect with a reason that will not change by
    retrying: the client raises an alarm, the viewer stops and says so."""
    with FakeRelay(reject="#GameUI_ServerRejectOldVersion") as r:
        p = watch(r, tmp_path)
        out, _ = p.communicate(timeout=60)
        assert p.returncode == 3
        assert r.refusals == 1 and r.connections == 0
    texts = [json.loads(ln)["text"] for ln in out.decode().splitlines()]
    assert any(t.startswith("[alarm] permanent failure:") for t in texts)
    assert meta_of(tmp_path)["quit"] == "alarm"


def test_no_slot_is_taken_when_it_would_be_the_last_one(tmp_path):
    with FakeRelay(max_players=1) as r:
        p = watch(r, tmp_path)
        out, _ = p.communicate(timeout=60)
        assert p.returncode == 2 and r.connections == 0
    assert b"--allow-last-slot" in out


def test_a_session_cut_during_signon_reconnects_and_still_leaves(tmp_path):
    """The relay goes silent on the first connection before FULL: the silence
    breaker ends the session (with net_Disconnect, the slot may exist), the
    supervisor waits and connects again; the stop leaves the second one."""
    with FakeRelay(cut_first_after=10) as r:
        opt = supervisor.Options(ip="127.0.0.1", port=r.port, silence_signon_s=0.5, backoff_s=0.2)
        sup = supervisor.Supervisor(opt, str(tmp_path / "c.tvd"), log=lambda *_a: None)
        import threading

        th = threading.Thread(target=sup.run)
        th.start()
        try:
            assert r.wait(lambda: r.connections == 2 and r.connected() == 1)
        finally:
            sup.stop = True
            th.join(30)
        assert r.disconnects == [(1, LEAVE)] * 2 + [(2, LEAVE)] * 2
    events = [(t, f) for _t, t, f in dumpfmt.DumpReader(str(tmp_path / "c.tvd")).events()]
    kinds = [t for t, _f in events]
    assert kinds.count(dumpfmt.SESSION_START) == 2
    broken = [f for t, f in events if t == dumpfmt.BROKEN]
    assert broken[0]["cause"] == "silence"
    assert [f["why"] for t, f in events if t == dumpfmt.LEAVE] == ["silence", "stop"]
