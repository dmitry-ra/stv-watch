"""Inputs of the viewer: a recording replayed at its own pace, or a live relay.

Live mode does not parse the network itself. A child process (net/client.py
launches net/main.py) holds the relay session (handshake, acks, reconnects,
net_Disconnect on exit) and writes the raw journal; the viewer tails that
journal with the same reader that replays recordings. So live and replay
share every stage after the socket, and every live session leaves a complete
recording that replays to the same picture.

The reader runs in its own thread and hands items to the main loop through a
queue: ("dg", Datagram) for inbound datagrams, ("ev", t_ns, rtype, data) for
everything else in the file.
"""

import queue
import threading
import time

from .net import client
from .stream.recording import Recording

NS = 1_000_000_000


class Pacer:
    """Media clock and replay pacing, owned by the main loop.

    Live: the wall clock. Replay: the recording's time, advancing at `speed`
    from the last skipped (or the first) item (speed 0 = as fast as possible:
    the time of the last item). Skipped items (the first `skip_s` seconds) are
    not paced. Pacing is done where items are
    consumed, not where they are read: the reader runs ahead by a queue, and a
    clock set from the reader's position would be ahead of what was shown."""

    def __init__(self, live, speed=1.0, skip_s=0.0, skip_until_ns=0):
        self.live, self.speed = live, speed
        # following a journal another process writes: the wall clock, and
        # what was written before we came only updates state
        self.skip_until = skip_until_ns
        self.skip_ns = int(skip_s * NS)
        self.t0 = None
        self.last_t = 0
        self.wall0 = self.media0 = None

    def skipping(self, t_ns):
        if self.skip_until:
            return t_ns < self.skip_until
        return not self.live and self.t0 is not None and t_ns - self.t0 < self.skip_ns

    def due(self, t_ns):
        """Seconds until the item at t_ns may be consumed (<= 0: now)."""
        if self.t0 is None:
            self.t0 = t_ns
        if self.live:
            return 0.0
        if self.skipping(t_ns) or self.speed <= 0 or self.wall0 is None:
            self.wall0, self.media0 = time.monotonic_ns(), t_ns
            return 0.0
        return (self.wall0 + (t_ns - self.media0) / self.speed - time.monotonic_ns()) / NS

    def seen(self, t_ns):
        self.last_t = max(self.last_t, t_ns)

    def media_now(self):
        if self.live:
            return time.time_ns()
        if self.wall0 is None or self.speed <= 0:
            return self.last_t
        return self.media0 + int((time.monotonic_ns() - self.wall0) * self.speed)

    def position_s(self):
        return (self.media_now() - self.t0) / NS if self.t0 else 0.0


class Reader(threading.Thread):
    """Recording -> bounded queue (~5 s of relay traffic). follow=True tails a
    journal still being written, until stop_flag is set."""

    def __init__(self, path, follow=False):
        super().__init__(daemon=True, name="reader")
        self.q = queue.Queue(maxsize=500)
        self.stop_flag = threading.Event()  # live: end of the journal
        self.abandon = threading.Event()  # drop what is left unread
        self.done = threading.Event()
        self.error = None
        self.follow = follow
        self.rec = Recording(
            path,
            follow=follow,
            idle_s=float("inf"),
            stop=self.stop_flag.is_set,
            on_event=self._event,
        )

    def _put(self, item):
        while not self.abandon.is_set():
            try:
                self.q.put(item, timeout=0.2)
                return
            except queue.Full:
                continue

    def _event(self, t_ns, rtype, data):
        self._put(("ev", t_ns, rtype, data))

    def run(self):
        try:
            for dg in self.rec:
                self._put(("dg", dg))
                if self.abandon.is_set():
                    break
        except Exception as e:  # noqa: BLE001
            self.error = e
        finally:
            self.done.set()


LiveClient = client.LiveClient


class LogTail(client.LogTail):
    """New lines of the client's log, for the lines worth a feed entry."""

    # [break], [leave], [session] are dump events already; these are not.
    KEEP = ("[alarm]", "[build]", "[handshake]", "[tvdump]")

    def __init__(self, path):
        super().__init__(path, keep=self.KEEP)
