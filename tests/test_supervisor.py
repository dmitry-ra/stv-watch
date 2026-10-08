"""Supervisor reconnect/backoff tests.

The reconnect loop is the reliability core: it must never busy-loop against a
server that drops us instantly (a connect flood), must not accumulate delay across
ordinary map changes, and must poll a standing refusal slowly. None of that is
observable from a 30-minute live run without deliberately breaking a server, so
it is driven here with scripted attempt outcomes and a recorded (never slept)
wait.
"""

import os
import tempfile

from stvwatch.net import session as sess
from stvwatch.net import supervisor, wire


class FakeConn:
    def __init__(self):
        self.challenge = 0x1234

    def close(self):
        pass


class FakeSession:
    """Stands in for a run session: only .state and .broke are read by the loop."""

    def __init__(self, state, broke):
        self.state = state
        self.broke = broke


class DrivenSupervisor(supervisor.Supervisor):
    """Supervisor with the network and the clock removed. `script` is a list of
    (conn_or_None, final_state, broke) tuples, one per attempt; the loop stops
    when the script is exhausted. Every backoff it would sleep is recorded."""

    def __init__(self, script):
        path = os.path.join(tempfile.mkdtemp(), "t.tvd")
        super().__init__(supervisor.Options(seconds=0, build="10889068"), path, log=lambda *a: None)
        self.script = list(script)
        self.step = 0
        self.waits = []

    def _handshake(self, attempt):
        conn, _state, _broke = self.script[self.step]
        return conn

    def _run_session(self, conn, deadline):
        _conn, state, broke = self.script[self.step]
        return FakeSession(state, broke)

    def _wait(self, seconds, deadline):
        self.waits.append(round(seconds, 3))
        self.step += 1
        return self.step < len(self.script)  # False stops the loop


def full_session():
    return (FakeConn(), wire.SIGNON_FULL, (sess.BREAK_CHANGELEVEL, "changelevel"))


def dropped_session():
    # Handshake succeeded, session died below FULL -> the connect flood scenario.
    return (FakeConn(), wire.SIGNON_CONNECTED, (sess.BREAK_DISCONNECT, "nope"))


def failed_handshake():
    return (None, None, None)


# --- standing-refusal classifier -------------------------------------------


def test_standing_refusal_matches_config_level_rejections():
    sup = DrivenSupervisor([full_session()])
    for reason in (
        "Bad spectator password",
        "Server uses different class tables",
        "different version",
        "STEAM ticket",
        "You are banned",
    ):
        assert sup._is_standing_refusal((sess.BREAK_DISCONNECT, reason)), reason


def test_transient_breaks_are_not_standing_refusals():
    sup = DrivenSupervisor([full_session()])
    assert not sup._is_standing_refusal((sess.BREAK_DISCONNECT, "Connection error"))
    assert not sup._is_standing_refusal((sess.BREAK_CHANGELEVEL, "changelevel"))
    assert not sup._is_standing_refusal((sess.BREAK_SILENCE, "no inbound"))
    assert not sup._is_standing_refusal(None)


# --- backoff across iterations (B1) ----------------------------------------


def _expected(opt, productivity, standing=()):
    """Mirror the loop's backoff arithmetic exactly, so the test documents the
    algorithm rather than a guessed sequence. Note the loop doubles BEFORE the
    first wait, so an unproductive streak starts at backoff_s*2, not backoff_s;
    the bare floor is only ever waited right after a productive attempt."""
    backoff = opt.backoff_s
    out = []
    for i, productive in enumerate(productivity):
        cap = opt.reject_backoff_max_s if i in standing else opt.backoff_max_s
        backoff = opt.backoff_s if productive else min(backoff * 2, cap)
        out.append(round(backoff, 3))
    return out


def test_a_productive_session_never_accrues_delay():
    # Every attempt reaches FULL (normal life: connect, hold, map change, repeat).
    sup = DrivenSupervisor([full_session()] * 5)
    sup.run()
    assert sup.waits == [sup.opt.backoff_s] * 5  # flat, never escalates


def test_instant_drops_escalate_backoff_geometrically():
    # The B1 case: handshake succeeds, session dies below FULL, every time.
    sup = DrivenSupervisor([dropped_session()] * 6)
    sup.run()
    assert sup.waits == _expected(sup.opt, [False] * 6)
    assert sup.waits == sorted(sup.waits)  # monotonic, never dips
    assert sup.waits[-1] <= sup.opt.backoff_max_s  # bounded, not runaway


def test_failed_handshake_also_escalates():
    sup = DrivenSupervisor([failed_handshake()] * 4)
    sup.run()
    assert sup.waits == _expected(sup.opt, [False] * 4)
    assert sup.waits[1] > sup.waits[0]  # escalates without a session


def test_full_resets_backoff_after_a_bad_streak():
    # Two instant drops build delay, then a good session resets it to the floor.
    script = [dropped_session(), dropped_session(), full_session(), dropped_session()]
    sup = DrivenSupervisor(script)
    sup.run()
    assert sup.waits == _expected(sup.opt, [False, False, True, False])
    assert sup.waits[2] == sup.opt.backoff_s  # the FULL attempt reset it


def test_standing_refusal_uses_the_slow_cap():
    # A password wall: backoff must be allowed to climb to the 300s reject cap,
    # far past the ordinary 30s cap, so we poll rather than hammer.
    sup = DrivenSupervisor(
        [(FakeConn(), wire.SIGNON_CONNECTED, (sess.BREAK_DISCONNECT, "Bad spectator password"))]
        * 12
    )
    sup.run()
    assert max(sup.waits) > sup.opt.backoff_max_s
    assert max(sup.waits) <= sup.opt.reject_backoff_max_s
