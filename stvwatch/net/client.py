#!/usr/bin/env python3
"""The one way to put a live client on a relay.

    ok, why, facts = precheck("IP:PORT", game="IP:PORT")
    c = LiveClient("IP", PORT, "x.tvd", "x.log", name="stvwatch", seconds=0)
    c.start(); ...; rc = c.stop()
    released, after = slot_released("IP:PORT", facts["spectators_before"])

Rules enforced here rather than by each caller:
  * precheck before connecting, nothing sent that takes a slot: the relay
    answers A2S, has no tv_password, runs a build with a known CRC, accepts
    anonymous login (getchallenge auth protocol), and keeps a slot free for
    real spectators after we join (unless allow_last_slot); optionally the
    game server has humans;
  * the client is a child process (tvdump, main.py run): reconnects, refusal classes
    and net_Disconnect on every exit live there; with --parent-pid the child
    sets PR_SET_PDEATHSIG and checks the parent is still ours, so even a
    crashed caller frees the slot; stop() is SIGTERM, then SIGKILL after a
    timeout;
  * after leaving, the relay's spectator count is read again: it must not be
    above what it was before we came.
"""

import os
import signal
import socket
import subprocess
import sys
import time

from . import a2s, builds, handshake, wire

# The child imports this same package, whatever the caller's cwd.
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHILD = "stvwatch.net.main"


def split_addr(addr):
    ip, port = addr.rsplit(":", 1)
    return ip, int(port)


def safe_info(addr):
    try:
        return a2s.info(*split_addr(addr), timeout=2.0, attempts=2)
    except (a2s.A2SError, OSError):
        return None


def auth_proto(addr, timeout=2.0, attempts=2):
    """Connectionless getchallenge only: no connect, no slot. 2 = anonymous
    login works; 3 = the relay wants Steam and ignores us; None = silent.
    Retried like A2S: one lost datagram must not cost a capture."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        for _ in range(attempts):
            try:
                sock.sendto(handshake.build_getchallenge(0x5EED1234), split_addr(addr))
                return handshake.parse_challenge(sock.recvfrom(4096)[0])["auth_proto"]
            except socket.timeout:
                continue
            except (OSError, handshake.HandshakeError):
                return None
        return None
    finally:
        sock.close()


def precheck(relay, game=None, need_humans=False, allow_last_slot=False, info=None, auth=None):
    """-> (ok, why, facts). `why` is a short reason word (+ detail)."""
    info, auth = info or safe_info, auth or auth_proto
    gi = info(game) if game else None
    ri = info(relay)
    facts = {
        "humans_before": None if gi is None else max(0, gi["players"] - gi["bots"]),
        "map": (gi or ri or {}).get("map"),
        "hostname": (ri or {}).get("name"),
        "spectators_before": None if ri is None else ri["players"],
        "max_spectators": None if ri is None else ri["max_players"],
        "version": None if ri is None else ri["version"],
    }
    if ri is None:
        return False, "relay_silent", facts
    if str(ri["version"]) not in builds.CRC_BY_BUILD:
        return False, "unknown_build %s" % ri["version"], facts
    if ri["visibility"]:
        return False, "password", facts
    free_after = ri["max_players"] - ri["players"] - 1
    if free_after < 0 or (free_after < 1 and not allow_last_slot):
        return False, "few_free_slots", facts
    if need_humans and not facts["humans_before"]:
        return False, "no_humans", facts
    proto = auth(relay)
    if proto != wire.AUTH_HASHEDCDKEY:
        return False, "auth_%s" % proto, facts
    return True, "", facts


def slot_released(relay, before, settle_s=2.0, info=None):
    """-> (released or None if unknown, spectators after)."""
    time.sleep(settle_s)
    ri = (info or safe_info)(relay)
    after = None if ri is None else ri["players"]
    if after is None or before is None:
        return None, after
    return after <= before, after


def slot_freed(relay, with_us, settle_s=2.0, polls=3, every=2.0, info=None):
    """Did our leaving free a slot? `with_us` is the relay's spectator count
    last seen while we were connected (it counts us). Comparing with the
    count before we joined instead raises a false alarm whenever someone
    else joined meanwhile (another watcher among them). A2S may
    still count us for a moment after we left: up to `polls` looks,
    `every` s apart. -> (freed or None if unknown, last count seen)."""
    time.sleep(settle_s)
    after = None
    for k in range(polls):
        if k:
            time.sleep(every)
        ri = (info or safe_info)(relay)
        after = None if ri is None else ri["players"]
        if after is not None and with_us is not None and after < with_us:
            return True, after
    if after is None or with_us is None:
        return None, after
    return False, after


class LiveClient:
    """tvdump child process writing the session journal (the dump)."""

    def __init__(self, ip, port, dump_path, log_path, name="tvdump", seconds=0.0, extra=()):
        self.ip, self.port = ip, port
        self.dump_path, self.log_path = dump_path, log_path
        self.name, self.seconds, self.extra = name, seconds, list(extra)
        self.proc = None
        self.log_fh = None

    def start(self, wait_s=10.0):
        """-> True once the dump has its header (the child is running).
        PDEATHSIG follows the forking THREAD: call this from a thread that
        outlives the child (stop() it before that thread ends)."""
        self.log_fh = open(self.log_path, "ab")
        # The child arms PDEATHSIG itself (main.py --parent-pid), after exec:
        # nothing runs between fork and exec in a threaded caller.
        cmd = [
            sys.executable,
            "-u",
            "-m",
            CHILD,
            "run",
            "--dump",
            self.dump_path,
            "--ip",
            self.ip,
            "--port",
            str(self.port),
            "--name",
            self.name,
            "--seconds",
            str(self.seconds),
            "--parent-pid",
            str(os.getpid()),
        ] + self.extra
        self.proc = subprocess.Popen(
            cmd,
            stdout=self.log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=PACKAGE_ROOT,
        )
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            if os.path.exists(self.dump_path) and os.path.getsize(self.dump_path) >= 21:
                return True
            if self.proc.poll() is not None:
                return False
            time.sleep(0.05)
        return False

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout=10.0):
        """SIGTERM -> tvdump sends net_Disconnect and exits. -> exit code, or
        None if it had to be killed (the relay then holds the slot 300 s)."""
        if self.proc is None:
            return None
        rc = self.proc.poll()
        if rc is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                rc = self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
                rc = None
        if self.log_fh:
            self.log_fh.close()
            self.log_fh = None
        return rc

    def cpu_s(self):
        """User+system CPU seconds of the child, from /proc."""
        try:
            with open(f"/proc/{self.proc.pid}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
        except (OSError, IndexError, ValueError, AttributeError):
            return 0.0


class LogTail:
    """New lines of the client's log since the last call."""

    def __init__(self, path, keep=None):
        self.path, self.keep = path, keep
        self.pos = 0
        self.buf = b""

    def lines(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
        except OSError:
            return []
        self.buf += data
        *done, self.buf = self.buf.split(b"\n")
        out = [raw.decode("utf-8", "replace").strip() for raw in done]
        return [ln for ln in out if self.keep is None or ln.startswith(self.keep)]
