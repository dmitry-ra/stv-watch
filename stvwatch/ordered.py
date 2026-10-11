"""The session stream in time order: what goes to events.jsonl, feed.log and
--json, by `t_utc` on the recording's clock.

Lines are put with their time in the order they are made, which is the order
of time but for a few made a little after their moment; each is written once
the stream clock has passed it and every line before it is written. A voice
line holds its place from the moment its utterance closes until its text is
ready: lines after it wait, nothing before it does.
"""

import bisect


def _key(item):
    return (item[0] or 0, item[1])


class Ordered:
    def __init__(self):
        self.items = []  # [t_ns or None, n, writers, ready]; sorted by (t, n) from `head`
        self.head = 0
        self.n = 0
        self.last = 0  # time of the last line written

    def __len__(self):
        return len(self.items) - self.head

    def put(self, t_ns, write=None):
        """A place at `t_ns` (None: the start of the stream, see start()) with
        `write(t_ns)` to call when it is due; without `write` the place is
        held until ready(). -> the place."""
        item = [t_ns, self.n, [write] if write else [], write is not None]
        self.n += 1
        bisect.insort(self.items, item, lo=self.head, key=_key)
        return item

    @staticmethod
    def add(item, write):
        item[2].append(write)

    @staticmethod
    def ready(item):
        item[3] = True

    def start(self, t_ns):
        """Lines made before the stream had a clock take its first moment."""
        for item in self.items[self.head :]:
            if item[0] is None:
                item[0] = t_ns

    def release(self, upto=None):
        """Write what is due by `upto` (None: everything that is ready)."""
        items = self.items
        while self.head < len(items):
            t, _n, writers, ready = items[self.head]
            if not ready or (upto is not None and (t is None or t > upto)):
                break
            items[self.head] = None
            self.head += 1
            # a recording whose clock stepped back keeps the order of the stream
            t = self.last = max(self.last, t or 0)
            for w in writers:
                w(t)
        if self.head > 1024 and self.head * 2 > len(items):
            del items[: self.head]
            self.head = 0
