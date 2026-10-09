"""Channel frames -> speech segments, by clock.

Facts this rests on, measured on live relay recordings:

  * Opus frames are 20 ms; their u16 seq restarts at 0 on every key press and
    is otherwise continuous. So the seq, not arrival time, is the audio
    timeline, and a backwards seq is the protocol's own talk-spurt boundary.
  * The decoder-reset mark arrives at the start or the end of a spurt, never
    meaning "new phrase" on its own; it resets the decoder and nothing else.
  * Arrival is bursty (relay batches; up to seconds on a player's lag), so
    "speech ended" is decided by a clock: no packet from the channel for
    `close_s`. A frame that later continues the closed spurt's seq opens a
    continuation segment linked to it: the tail is kept, not dropped and not
    glued silently onto the wrong phrase.

Every opus frame fed comes out in exactly one segment (`frames_in` ==
sum of segment frames) -- the no-lost-tail invariant the tests check.
"""

from collections import Counter
from dataclasses import dataclass, field

from .steamvoice import Frame


@dataclass(frozen=True)
class ChannelFrame:
    """One audio unit attributed to a speaker. The key is the SteamID64 from
    the payload, never the slot."""

    steamid64: int
    t_ns: int
    tick: int
    session: int
    from_client: int
    frame: Frame


@dataclass
class Segment:
    id: int
    steamid64: int
    session: int
    from_client: int
    first_rx_ns: int
    last_rx_ns: int
    first_tick: int
    last_tick: int = 0
    items: list = field(default_factory=list)  # (seq, Frame) in arrival order
    continuation_of: int = -1
    close_ns: int = 0
    close_reason: str = ""
    first_seq: int = -1
    last_seq: int = -1

    @property
    def opus_frames(self):
        return sum(1 for _s, f in self.items if f.kind == "opus")

    @property
    def span_frames(self):
        """Frames on the seq timeline, gaps included."""
        return 0 if self.first_seq < 0 else self.last_seq - self.first_seq + 1


def _seq_delta(new, last):
    """Signed u16 distance new - last."""
    d = (new - last) & 0xFFFF
    return d - 0x10000 if d >= 0x8000 else d


class Segmenter:
    """Streaming segmenter. feed() frames in arrival order, clock() with the
    current time (every datagram's receive time when replaying, a timer when
    live), flush() at the end. Closed segments accumulate in `closed`."""

    def __init__(self, close_s=1.0, max_gap_frames=50):
        self.close_ns = int(close_s * 1e9)
        self.max_gap = max_gap_frames
        self.open = {}  # (session, steamid64) -> Segment
        self.last_closed = {}  # (session, steamid64) -> Segment
        self.closed = []
        self.counters = Counter()
        self._next_id = 0

    def _new(self, cf, continuation_of=-1):
        seg = Segment(
            self._next_id,
            cf.steamid64,
            cf.session,
            cf.from_client,
            cf.t_ns,
            cf.t_ns,
            cf.tick,
            continuation_of=continuation_of,
        )
        self._next_id += 1
        self.open[(cf.session, cf.steamid64)] = seg
        return seg

    def _close(self, key, now_ns, reason):
        seg = self.open.pop(key)
        seg.close_ns = now_ns
        seg.close_reason = reason
        self.last_closed[key] = seg
        self.closed.append(seg)
        self.counters["close_" + reason] += 1

    def clock(self, now_ns):
        for key in [k for k, s in self.open.items() if now_ns - s.last_rx_ns > self.close_ns]:
            self._close(key, now_ns, "clock")

    def feed(self, cf):
        """cf: ChannelFrame."""
        self.clock(cf.t_ns)
        for key in [k for k in self.open if k[0] != cf.session]:
            self._close(key, cf.t_ns, "session")
        key = (cf.session, cf.steamid64)
        f = cf.frame
        if f.kind == "rate":
            return
        self.counters["frames_in_" + f.kind] += 1
        seg = self.open.get(key)
        if f.kind == "opus":
            if seg is not None and seg.last_seq >= 0:
                d = _seq_delta(f.seq, seg.last_seq)
                if d <= 0 or d > self.max_gap:
                    self._close(key, cf.t_ns, "seq_restart" if d <= 0 else "seq_jump")
                    seg = None
            if seg is None:
                prev = self.last_closed.get(key)
                cont = -1
                if prev is not None and prev.last_seq >= 0:
                    d = _seq_delta(f.seq, prev.last_seq)
                    if 0 < d <= self.max_gap:
                        cont = prev.id
                        self.counters["continuations"] += 1
                seg = self._new(cf, cont)
            if seg.first_seq < 0:
                seg.first_seq = f.seq
            elif _seq_delta(f.seq, seg.last_seq) > 1:
                self.counters["seq_gap_frames"] += _seq_delta(f.seq, seg.last_seq) - 1
            seg.last_seq = f.seq
        elif seg is None:
            # A reset or silence run with no spurt open (the "R S750" that ends
            # a spurt, after the clock already closed it) carries no audio:
            # counted, not made into a segment of its own.
            self.counters["orphan_" + f.kind] += 1
            return
        seg.items.append((f.seq, f))
        seg.last_rx_ns = cf.t_ns
        seg.last_tick = cf.tick

    def flush(self, now_ns=None):
        for key in list(self.open):
            seg = self.open[key]
            self._close(key, now_ns if now_ns is not None else seg.last_rx_ns, "end")
        return self.closed
