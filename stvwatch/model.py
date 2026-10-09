"""What the status block shows, as plain state fed by the main loop.

All times are on the media clock (receive time of the recording, UTC epoch ns):
in live mode that is the wall clock, in replay the recording's own time, so
rates and "seconds ago" read the same in both.
"""

from collections import deque
from dataclasses import dataclass, field

NS = 1_000_000_000


class Window:
    """Sum of values over the last `span_s` seconds of media time."""

    def __init__(self, span_s=5.0):
        self.span = int(span_s * NS)
        self.items = deque()
        self.total = 0

    def add(self, t_ns, value=1):
        self.items.append((t_ns, value))
        self.total += value

    def sum(self, now_ns):
        cut = now_ns - self.span
        while self.items and self.items[0][0] < cut:
            self.total -= self.items.popleft()[1]
        return self.total

    def rate(self, now_ns):
        return self.sum(now_ns) * NS / self.span


class Traffic:
    """Inbound datagram rates plus the flowing/stopped state of the stream.

    The stream counts as stopped after `quiet_s` without an inbound datagram,
    or after twice the 95th percentile of recent gaps if that is longer: an
    idle relay paces snapshots ~2 s apart, a busy one ~15 ms,
    and one fixed threshold either chatters on the first or is blind on the
    second. Capped at `quiet_max_s`. `check` reports each transition once."""

    def __init__(self, quiet_s=3.0, span_s=5.0, quiet_max_s=10.0):
        self.quiet_min = int(quiet_s * NS)
        self.quiet_max = int(quiet_max_s * NS)
        self.quiet = self.quiet_min
        self.gaps = deque(maxlen=200)
        self.pkts = Window(span_s)
        self.bytes = Window(span_s)
        self.splits = Window(span_s)
        self.out_pkts = Window(span_s)
        self.total_pkts = 0
        self.total_bytes = 0
        self.total_splits = 0
        self.total_out = 0
        self.first_ns = 0
        self.last_ns = 0
        self.flowing = False
        self.since_ns = 0  # start of the current flowing/stopped state
        self.stops = 0

    def inbound(self, t_ns, nbytes, split):
        self.pkts.add(t_ns)
        self.bytes.add(t_ns, nbytes)
        self.total_pkts += 1
        self.total_bytes += nbytes
        if split:
            self.splits.add(t_ns)
            self.total_splits += 1
        if not self.first_ns:
            self.first_ns = t_ns
        if self.last_ns and t_ns - self.last_ns <= self.quiet_max:
            self.gaps.append(t_ns - self.last_ns)
            if len(self.gaps) >= 10 and len(self.gaps) % 5 == 0:
                p95 = sorted(self.gaps)[int(0.95 * (len(self.gaps) - 1))]
                self.quiet = min(self.quiet_max, max(self.quiet_min, 2 * p95))
        self.last_ns = t_ns

    def outbound(self, t_ns):
        self.out_pkts.add(t_ns)
        self.total_out += 1

    def check(self, now_ns):
        """-> ("started"|"stopped"|"resumed", gap_s) on a transition, else None."""
        if not self.total_pkts:
            return None
        if self.flowing and now_ns - self.last_ns > self.quiet:
            self.flowing = False
            self.since_ns = self.last_ns
            self.stops += 1
            return ("stopped", (now_ns - self.last_ns) / NS)
        if not self.flowing and now_ns - self.last_ns <= self.quiet:
            first = self.since_ns == 0
            gap = (self.last_ns - self.since_ns) / NS if not first else 0.0
            self.flowing = True
            self.since_ns = self.last_ns
            return ("started" if first else "resumed", gap)
        return None

    def split_share(self, now_ns):
        n = self.pkts.sum(now_ns)
        return self.splits.sum(now_ns) / n if n else 0.0


@dataclass
class Channel:
    """One speaker, keyed by SteamID64 from the voice payload."""

    sid64: int
    first_ns: int
    nick: str = ""  # nick from the stream's userinfo table
    last_ns: int = 0  # receive time of the last voice frame
    frames: int = 0  # opus frames received
    plc: int = 0  # frames concealed by the decoder
    gap_frames: int = 0  # frames of longer holes, filled with silence
    resets: int = 0
    utterances: int = 0
    finals: int = 0
    nospeech: int = 0
    opus_bytes: Window = field(default_factory=lambda: Window(3.0))
    opus_frames: Window = field(default_factory=lambda: Window(3.0))
    partial: str = ""
    partial_ns: int = 0
    talking: bool = False
    utt_start_ns: int = 0  # first frame of the current or last utterance
    stream_finals: int = 0  # stream mode: finals with text in the open utterance

    @property
    def audio_s(self):
        return self.frames * 0.020

    def kbps(self, now_ns):
        """Opus payload bit rate while frames arrive: bytes per frame x 50."""
        n = self.opus_frames.sum(now_ns)
        if not n:
            return 0.0
        return self.opus_bytes.sum(now_ns) / n * 50 * 8 / 1000


@dataclass
class Conn:
    endpoint: str = ""
    state: str = "init"  # init/connecting/signon:<NAME>/FULL/broken/left/replay
    state_ns: int = 0
    sessions: int = 0
    attempts_failed: int = 0
    full_ns: int = 0
    map: str = ""
    hostname: str = ""
    max_clients: int = 0
    players: int = -1  # A2S of the relay (live only)
    max_players: int = -1
    version: str = ""
    last_error: str = ""
    alarm: str = ""
