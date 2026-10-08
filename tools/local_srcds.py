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
        """pidfds of every process under ours. A pidfd names one process, so
        a signal sent through it after that process is gone fails instead of
        reaching whoever got the PID next. (A process group would not do:
        `script` puts srcds in a session of its own.)"""
        out, todo = [], [self.proc.pid]
        while todo:
            pid = todo.pop()
            try:
                with open(f"/proc/{pid}/task/{pid}/children") as f:
                    kids = [int(x) for x in f.read().split()]
            except OSError:
                kids = []
            for kid in kids:
                try:
                    out.append(os.pidfd_open(kid))
                except ProcessLookupError:
                    continue
                todo.append(kid)
        return out

    @staticmethod
    def _kill_all(fds):
        for fd in fds:
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(fd)

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
