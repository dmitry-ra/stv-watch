"""The safety gate (client.precheck): which relays the client refuses to
join; the child process leaving when its launcher dies; the slot check."""

import os
import subprocess
import sys
import time

import pytest

from stvwatch.net import client, wire

RELAY, GAME = "relay.invalid:27020", "game.invalid:27015"


def relay(**kw):
    base = {
        "players": 0,
        "max_players": 128,
        "version": "10889068",
        "visibility": 0,
        "map": "dm_x",
        "bots": 0,
    }
    base.update(kw)
    return base


GAME_INFO = {"players": 5, "bots": 1, "map": "dm_x"}


@pytest.mark.parametrize(
    "ri,gi,empty_ok,last,auth,why",
    [
        (relay(), GAME_INFO, False, False, 2, ""),
        (None, GAME_INFO, False, False, 2, "relay_silent"),
        (relay(version="1"), GAME_INFO, False, False, 2, "unknown_build 1"),
        (relay(visibility=1), GAME_INFO, False, False, 2, "password"),
        (relay(players=1, max_players=2), GAME_INFO, False, False, 2, "few_free_slots"),
        (relay(players=1, max_players=2), GAME_INFO, False, True, 2, ""),
        (relay(players=0, max_players=1), GAME_INFO, False, False, 2, "few_free_slots"),
        (relay(players=1, max_players=1), GAME_INFO, False, True, 2, "few_free_slots"),
        (relay(), {"players": 1, "bots": 1, "map": "dm_x"}, False, False, 2, "no_humans"),
        (relay(), {"players": 1, "bots": 1, "map": "dm_x"}, True, False, 2, ""),
        (relay(), GAME_INFO, False, False, 3, "auth_3"),
        (relay(), GAME_INFO, False, False, None, "auth_None"),
    ],
)
def test_precheck(monkeypatch, ri, gi, empty_ok, last, auth, why):
    answers = {RELAY: ri, GAME: gi}
    monkeypatch.setattr(client, "safe_info", lambda a: answers[a])
    monkeypatch.setattr(client, "auth_proto", lambda a: auth)
    ok, got, _facts = client.precheck(RELAY, GAME, need_humans=not empty_ok, allow_last_slot=last)
    assert (ok, got) == (why == "", why)
    assert wire.AUTH_HASHEDCDKEY == 2


SLEEPER = """
import os, signal, sys, time
from stvwatch.net import main
dump = sys.argv[sys.argv.index("--dump") + 1]
parent = int(sys.argv[sys.argv.index("--parent-pid") + 1])
def bye(*_):
    with open(dump + ".term.part", "w") as f:
        f.write("sigterm")
    os.replace(dump + ".term.part", dump + ".term")   # seen whole or not at all
    sys.exit(0)
signal.signal(signal.SIGTERM, bye)          # before the dump: start() waits on it
assert main._arm_parent_death(parent)
open(dump, "wb").write(b"x" * 32)
time.sleep(60)
"""

PARENT = """
from stvwatch.net import client
client.CHILD = "sleeper"
c = client.LiveClient("127.0.0.1", 1, {dump!r}, {log!r})
assert c.start(wait_s=10)
print(c.pid, flush=True)
import time; time.sleep(60)
"""


def test_client_leaves_when_its_parent_is_killed(tmp_path):
    """PDEATHSIG: the launcher dies without cleanup (SIGKILL), the client
    still gets SIGTERM -- the path on which it sends net_Disconnect. The stub
    arms it with the client's own main._arm_parent_death from the
    --parent-pid the launcher passes."""
    (tmp_path / "sleeper.py").write_text(SLEEPER)
    dump = str(tmp_path / "d.tvd")
    code = PARENT.format(dump=dump, log=str(tmp_path / "d.log"))
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    parent = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env
    )
    child = int(parent.stdout.readline())
    parent.kill()
    parent.wait(10)
    end = time.monotonic() + 30
    while time.monotonic() < end and not os.path.exists(dump + ".term"):
        time.sleep(0.05)
    assert open(dump + ".term").read() == "sigterm"
    end = time.monotonic() + 5
    while time.monotonic() < end and not exited(child):
        time.sleep(0.05)
    assert exited(child)


def exited(pid):
    """Gone, or a zombie: reaping is up to whoever adopted the orphan."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] in ("Z", "X")
    except FileNotFoundError:
        return True


def test_slot_freed_looks_again_while_a2s_still_counts_us():
    """A2S may count us for a moment after we left: 3 with us, then 3 (late),
    then 2. A count that stays at 3 is an alarm; no answer is unknown."""

    def seq(*v):
        it = iter(v)
        return lambda relay: (lambda x: None if x is None else {"players": x})(next(it))

    kw = {"settle_s": 0, "every": 0}
    assert client.slot_freed("r", 3, info=seq(3, 3, 2), **kw) == (True, 2)
    assert client.slot_freed("r", 3, info=seq(3, 3, 3), **kw) == (False, 3)
    assert client.slot_freed("r", 3, info=seq(None, None, None), **kw) == (None, None)
    assert client.slot_freed("r", None, info=seq(2, 2, 2), **kw) == (None, 2)
