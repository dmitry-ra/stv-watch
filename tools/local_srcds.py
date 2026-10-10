#!/usr/bin/env python3
"""A loopback-only srcds with SourceTV, for exercising the client offline.

    uv run python tools/local_srcds.py SRCDS_DIR [LOG]

SRCDS_DIR is a dedicated server install of Half-Life 2: Deathmatch (SteamCMD
app 232370, the directory holding srcds_linux). The relay listens on
127.0.0.1:27920; connect with `stv-watch --relay 127.0.0.1:27920
--allow-last-slot`. Bots: `hl2mp_bot_add` at the srcds console, or cmd().

The process is a child of the caller and is always torn down on exit (context
manager): a stray srcds would hold ports and CPU with nobody to stop it.
Console commands go to its stdin (`-console` reads it line by line).
"""

import os
import shlex
import signal
import subprocess
import sys
import threading
import time

from stvwatch.net import a2s


def _pidfd_works():
    """CPython compiles the wrappers only when the build's kernel headers
    define the syscalls (uv's standalone builds lack them on every version),
    and the running kernel may still answer ENOSYS."""
    if not (hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")):
        return False
    try:
        os.close(os.pidfd_open(os.getpid()))
    except OSError:
        return False
    return True


PIDFD = _pidfd_works()


def _started(pid):
    """Start time in clock ticks since boot: with the PID it names one process."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except OSError:
        return None


def _pin(pid):
    """A handle that cannot come to mean whoever gets the PID next: a pidfd,
    else the PID with its start time. None once the process is gone."""
    if PIDFD:
        try:
            return os.pidfd_open(pid)
        except ProcessLookupError:
            return None
    born = _started(pid)
    return None if born is None else (pid, born)


def _kill(handle):
    if PIDFD:
        try:
            signal.pidfd_send_signal(handle, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            os.close(handle)
        return
    pid, born = handle
    if _started(pid) == born:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class LocalSrcds:
    def __init__(
        self,
        srv_dir,
        log_path="srcds.log",
        port=27915,
        tv_port=27920,
        game_map="dm_lockdown",
        maxplayers=4,
        extra=(),
    ):
        self.srv_dir = srv_dir
        self.log_path = log_path
        self.port = port
        self.tv_port = tv_port
        self.game_map = game_map
        self.maxplayers = maxplayers
        self.extra = list(extra)
        self.proc = None
        self._log = None

    def start(self):
        # SIGTERM (from `timeout`) must still run stop(): an orphaned srcds
        # would keep the ports with nobody left to quit it.
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        env = dict(os.environ, LD_LIBRARY_PATH=f"{self.srv_dir}:{self.srv_dir}/bin")
        srcds = (
            [
                f"{self.srv_dir}/srcds_linux",
                "-game",
                "hl2mp",
                "-console",
                "-norestart",
                "-nobreakpad",
                "+ip",
                "127.0.0.1",
                "-port",
                str(self.port),
                # Without it every reply to 127.0.0.1 goes to the in-process
                # loopback queue, never the socket (net_ws.cpp NET_SendPacket).
                "+net_usesocketsforloopback",
                "1",
                "+sv_lan",
                "1",
                "+maxplayers",
                str(self.maxplayers),
                "+tv_enable",
                "1",
                "+tv_relayvoice",
                "1",
                "+tv_delay",
                "0",
                "+tv_port",
                str(self.tv_port),
            ]
            + self.extra
            + ["+map", self.game_map]
        )
        # Under a pty: on a plain pipe srcds block-buffers its console, so the
        # log lags by kilobytes and a "Dropped ..." line arrives too late to
        # time anything by.
        cmd = ["script", "-qfec", shlex.join(srcds), "/dev/null"]
        self._log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            cmd,
            cwd=self.srv_dir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        return self

    def log_text(self):
        with open(self.log_path, "rb") as f:
            return f.read().decode("utf-8", "replace")

    def wait_log(self, needle, timeout=120.0, start=0):
        """Block until `needle` appears in the log past offset `start`."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise RuntimeError("srcds exited rc=%s" % self.proc.returncode)
            if needle in self.log_text()[start:]:
                return
            time.sleep(0.5)
        raise RuntimeError("%r not seen within %.0fs" % (needle, timeout))

    def wait_relay(self, timeout=120.0):
        self.wait_log("SourceTV broadcast active", timeout)

    def spectators(self):
        return a2s.info("127.0.0.1", self.tv_port, timeout=1.0, attempts=2)["players"]

    def cmd(self, line):
        self.proc.stdin.write((line + "\n").encode())
        self.proc.stdin.flush()

    def _descendants(self):
        """Handles (_pin) of every process under ours, so a signal sent after
        one is gone is dropped instead of reaching whoever got its PID next.
        (A process group would not do: `script` puts srcds in a session of
        its own.)"""
        out, todo = [], [(self.proc.pid, _started(self.proc.pid))]
        while todo:
            pid, born = todo.pop()
            try:
                with open(f"/proc/{pid}/task/{pid}/children") as f:
                    kids = [int(x) for x in f.read().split()]
            except OSError:
                kids = []
            # Read after the list: if the PID changed hands meanwhile, these
            # are a stranger's children.
            if _started(pid) != born:
                continue
            for kid in kids:
                kid_born = _started(kid)
                handle = None if kid_born is None else _pin(kid)
                if handle is not None:
                    out.append(handle)
                    todo.append((kid, kid_born))
        return out

    @staticmethod
    def _kill_all(handles):
        for handle in handles:
            _kill(handle)

    def stop(self):
        if self.proc is None:
            return
        kids = self._descendants()
        if self.proc.poll() is None:
            try:
                self.cmd("quit")
                self.proc.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.terminate()
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(10)
        self._kill_all(kids)
        self._log.close()
        self.proc = None

    def kill(self):
        """SIGKILL, as a crash would: no net_Disconnect reaches clients."""
        if self.proc is None:
            return
        self._kill_all(self._descendants())
        self.proc.kill()
        self.proc.wait(10)
        self._log.close()
        self.proc = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: local_srcds.py SRCDS_DIR [LOG]")
    with LocalSrcds(sys.argv[1], log_path=sys.argv[2] if len(sys.argv) > 2 else "srcds.log") as s:
        s.wait_relay()
        print("relay up on 127.0.0.1:%d, Ctrl-C to stop" % s.tv_port, flush=True)
        try:
            while s.proc.poll() is None:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
