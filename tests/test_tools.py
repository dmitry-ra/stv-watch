"""The developer tools: tools/local_srcds.py stops only what it started, and
tools/tmux_check.sh never takes over a tmux session it did not create."""

import os
import shutil
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.dirname(HERE), "tools")
sys.path.insert(0, TOOLS)
import local_srcds  # noqa: E402

# Exits on `quit` as srcds does and leaves a straggler behind.
STAND_IN = """#!/usr/bin/env python3
import subprocess, sys
subprocess.Popen(["sleep", "300"])
for line in sys.stdin:
    if line.strip() == "quit":
        sys.exit(0)
"""


def started(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except OSError:
        return None


def tree(pid):
    out, todo = [], [pid]
    while todo:
        with open(f"/proc/{todo[0]}/task/{todo[0]}/children") as f:
            kids = [int(x) for x in f.read().split()]
        out += kids
        todo = todo[1:] + kids
    return out


@pytest.mark.skipif(not shutil.which("script"), reason="needs script(1)")
def test_stopping_the_local_server_signals_only_processes_still_its_own(tmp_path, monkeypatch):
    """srcds exits on `quit`; a signal sent afterwards by its PID number
    would reach whatever process got that number next."""
    exe = tmp_path / "srcds_linux"
    exe.write_text(STAND_IN)
    exe.chmod(0o755)
    s = local_srcds.LocalSrcds(str(tmp_path), log_path=str(tmp_path / "log")).start()
    end = time.monotonic() + 10
    while len(tree(s.proc.pid)) < 2 and time.monotonic() < end:
        time.sleep(0.05)
    born = {p: started(p) for p in tree(s.proc.pid)}
    stale = []
    real = os.kill

    def kill(pid, sig):
        if pid in born and started(pid) != born[pid]:
            stale.append(pid)
            raise ProcessLookupError
        real(pid, sig)

    monkeypatch.setattr(local_srcds.os, "kill", kill)
    s.stop()
    alive = [p for p in born if started(p) == born[p]]
    assert (len(born), stale, alive) == (2, [], [])


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_tmux_check_refuses_a_session_that_already_exists(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    env.update(TMUX_TMPDIR=str(tmp_path), TMUX_CHECK_SESSION="mine", CAPDIR=str(tmp_path / "c"))
    tmux = ["tmux", "-f", "/dev/null"]
    subprocess.run(tmux + ["new-session", "-d", "-s", "mine", "sleep 60"], env=env, check=True)
    try:
        rc = subprocess.run(
            ["bash", os.path.join(TOOLS, "tmux_check.sh"), "1", "--", "--replay", "x.tvd"],
            env=env,
            capture_output=True,
            timeout=120,
        ).returncode
        alive = subprocess.run(tmux + ["has-session", "-t", "=mine"], env=env).returncode == 0
    finally:
        subprocess.run(tmux + ["kill-server"], env=env, capture_output=True)
    assert (rc, alive) == (2, True)
