"""The screen of a headless engine over a Unix socket.

`--serve PATH` runs the engine as usual but hands what it would draw to every
client of a Unix socket at PATH; `--attach PATH` is such a client: a screen and
nothing else (no relay, no parsing, no model).

What travels is the screen's own calls, not a model of the game: feed lines,
live lines opened, updated and closed, and the status block of each draw, as
JSON lines from the engine to the client (the client sends nothing). The client
makes the same calls on a Screen of its own, batch by batch, so it draws what a
local run draws.

On connect a client gets a snapshot: hello, the last HISTORY feed lines with
the live lines still open among them, the last block. Feed and live lines carry
a sequence number, so a client that comes back to the same engine skips what it
has and closes the live lines that closed while it was away. A client that does
not read is dropped once CLIENT_BUF bytes wait for it, and reconnects: the
engine never waits for a screen.
"""

import collections
import json
import os
import select
import socket
import stat
import threading
import time

from .render import Screen

PROTO = 1
HISTORY = 200
CLIENT_BUF = 1 << 20
DRAIN_S = 1.0  # at exit, how long the last bytes may take to reach the clients


class ServeError(Exception):
    pass


def socket_path(name):
    """A name without '/' is a socket in $XDG_RUNTIME_DIR/stv-watch/."""
    if "/" in name:
        return name
    run = os.environ.get("XDG_RUNTIME_DIR")
    if not run:
        raise ServeError(f"{name!r} is a bare name and XDG_RUNTIME_DIR is unset: give a path")
    return os.path.join(run, "stv-watch", name)


def encode(msgs):
    return "".join(
        json.dumps(m, ensure_ascii=False, separators=(",", ":")) + "\n" for m in msgs
    ).encode("utf-8")


def served(path):
    """Does a live engine accept on `path`? A socket file whose engine died
    refuses the connection."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(path)
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        return True  # cannot tell (not ours, backlog full): leave it alone
    finally:
        probe.close()
    return True


def listen(path):
    """A listening socket at `path`, mode 0600, its directory made 0700 if
    missing. A socket file left by a dead engine is replaced; a live one, or
    any other file, is refused."""
    if len(os.fsencode(path)) > 107:
        raise ServeError(f"{path}: too long for a Unix socket (107 bytes at most)")
    try:
        os.makedirs(os.path.dirname(path) or ".", 0o700, exist_ok=True)
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    except OSError as e:
        raise ServeError(f"{path}: {e.strerror}") from None
    if st is not None:
        if not stat.S_ISSOCK(st.st_mode):
            raise ServeError(f"{path} exists and is not a socket")
        if served(path):
            raise ServeError(f"{path} is served by another engine")
        os.unlink(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)  # no window in which the file is open to others
    try:
        s.bind(path)
        os.chmod(path, 0o600)
        s.listen(8)
    except OSError as e:
        s.close()
        raise ServeError(f"{path}: {e.strerror}") from None
    finally:
        os.umask(old)
    s.setblocking(False)
    return s


class Peer:
    def __init__(self, sock, data):
        self.sock = sock
        self.buf = bytearray(data)
        self.dead = False


class Server:
    """Clients of one engine. The engine thread publishes; an I/O thread
    accepts and writes. Only the I/O thread closes client sockets: the engine
    thread marks a slow one dead."""

    def __init__(self, path, hello):
        self.path = path
        self.sock = listen(path)
        self.ino = os.stat(path).st_ino
        self.hello = dict(hello, op="hello", proto=PROTO, path=path)
        self.hello.setdefault("engine", f"{os.getpid()}-{time.time_ns()}")
        self.lock = threading.Lock()
        self.peers = []
        self.ring = collections.deque(maxlen=HISTORY)  # snapshot messages, oldest first
        self.open = {}  # live key -> its message in the ring
        self.last = None  # the last draw or bye
        self.seq = 0
        self.dropped = 0
        self.closing = False
        self.shut = False
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)
        self.thread = threading.Thread(target=self._run, name="serve", daemon=True)

    def start(self):
        self.thread.start()

    def count(self):
        with self.lock:
            return sum(not p.dead for p in self.peers)

    # ---- engine thread
    def publish(self, msgs):
        with self.lock:
            for m in msgs:
                self._keep(m)
            data = encode(msgs)
            for p in self.peers:
                if p.dead:
                    continue
                if len(p.buf) + len(data) > CLIENT_BUF:
                    p.dead, p.buf = True, bytearray()
                    self.dropped += 1
                else:
                    p.buf += data
        self._wake()

    def _keep(self, m):
        op = m["op"]
        if op in ("line", "open"):
            self.seq += 1
            m["seq"] = self.seq
            e = dict(m)
            self.ring.append(e)
            if op == "open":
                self.open[m["key"]] = e
        elif op == "update":
            e = self.open.get(m["key"])
            if e is not None:
                e["spans"] = m["spans"]
        elif op == "close":
            e = self.open.pop(m["key"], None)
            if e is None:
                # its open left the history: a line of its own, as on a screen
                self.seq += 1
                m["seq"] = self.seq
                self.ring.append({"op": "line", "seq": self.seq, "spans": m["cont"] + m["spans"]})
            else:
                e.update(op="line", spans=m["spans"], cont=m["cont"])
        else:
            self.last = m

    def close(self, msgs):
        """Last messages, a moment for them to leave, then no more clients."""
        self.publish(msgs)
        if self.thread.is_alive():
            end = time.monotonic() + DRAIN_S
            while time.monotonic() < end:
                with self.lock:
                    if not any(p.buf for p in self.peers if not p.dead):
                        break
                time.sleep(0.01)
        with self.lock:
            self.closing = True
        self._wake()
        if self.thread.is_alive():
            self.thread.join(2.0)
        else:
            self._shut()

    def _wake(self):
        if self.shut:
            return
        try:
            os.write(self.wake_w, b"x")
        except (BlockingIOError, OSError):
            pass

    # ---- I/O thread
    def _snapshot(self):
        msgs = [self.hello] + [dict(e) for e in self.ring]
        if self.last is not None:
            msgs.append(self.last)
        return encode(msgs)

    def _run(self):
        try:
            while True:
                with self.lock:
                    for p in [p for p in self.peers if p.dead]:
                        p.sock.close()
                        self.peers.remove(p)
                    if self.closing:
                        return
                    rd = [self.sock, self.wake_r]  # a gone client shows on its next send
                    wr = [p.sock for p in self.peers if p.buf]
                r, w, _ = select.select(rd, wr, [], 1.0)
                if self.wake_r in r:
                    try:
                        os.read(self.wake_r, 4096)
                    except BlockingIOError:
                        pass
                with self.lock:
                    if self.sock in r:
                        self._accept()
                    for p in self.peers:
                        if not p.dead and p.sock in w:
                            self._send(p)
        finally:
            self._shut()

    def _accept(self):
        while True:
            try:
                c, _addr = self.sock.accept()
            except OSError:
                return
            c.setblocking(False)
            self.peers.append(Peer(c, self._snapshot()))

    def _send(self, p):
        try:
            n = p.sock.send(p.buf)
        except BlockingIOError:
            return
        except OSError:
            p.dead = True
            return
        del p.buf[:n]

    def _shut(self):
        if self.shut:
            return
        self.shut = True
        for p in self.peers:
            p.sock.close()
        self.peers = []
        try:
            if os.stat(self.path).st_ino == self.ino:
                os.unlink(self.path)
        except OSError:
            pass
        self.sock.close()
        for fd in (self.wake_r, self.wake_w):
            try:
                os.close(fd)
            except OSError:
                pass


class ServedScreen:
    """The engine's screen under --serve: each draw goes to the clients as
    one batch; stdout gets the finished lines, as --plain without status lines."""

    tty = True  # the app renders as for a terminal: live lines and the block

    def __init__(self, path, hello):
        self.server = Server(path, hello)
        self.log = Screen(plain=True, color=False)
        self.pending = []
        self.updates = {}

    def start(self, stderr_path=None):
        self.log.start(stderr_path)
        p = self.server.path
        self.log.out.write(f"serving {p} (pid {os.getpid()}): stv-watch --attach {p}\n")
        self.log.out.flush()
        self.server.start()

    def key(self):
        return None

    def feed(self, spans):
        self.pending.append({"op": "line", "spans": spans})
        self.log.feed(spans)

    def live_open(self, key, spans):
        self.pending.append({"op": "open", "key": key, "spans": spans})

    def live_update(self, key, spans):
        self.updates[key] = spans

    def live_close(self, key, spans, cont=()):
        self.updates.pop(key, None)
        self.pending.append({"op": "close", "key": key, "spans": spans, "cont": list(cont)})
        self.log.live_close(key, spans, cont)

    def _batch(self, last):
        msgs = self.pending + [
            {"op": "update", "key": k, "spans": s} for k, s in self.updates.items()
        ]
        self.pending, self.updates = [], {}
        return msgs + [last]

    def draw(self, block):
        self.server.publish(self._batch({"op": "draw", "block": list(block)}))
        self.log.draw(())

    def stop(self, final_block=()):
        self.server.close(self._batch({"op": "bye", "block": list(final_block)}))
        self.log.stop(())
