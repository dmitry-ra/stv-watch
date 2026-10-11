"""--rotate: when a session gives way to the next, by a cron schedule read in
the recording's time (the receive time of its records), so a replay of a
recording rotates where the live run did."""

from datetime import datetime

from cronsim import CronSim, CronSimError

from .times import NS


class RotateError(ValueError):
    pass


class Schedule:
    """A standard 5-field cron expression in the zone `tz`.

    due() is fed the time of every record in order. The first call only
    starts the schedule; later calls return the start of the period a record
    opens (the last fire time at or before it), once per period that has
    records: periods without any are skipped, not given empty sessions."""

    def __init__(self, expr, tz, now_ns):
        fields = expr.split()
        if len(fields) != 5:
            # cronsim would take a sixth field as seconds and shift everything
            raise RotateError(
                f"{expr!r} has {len(fields)} fields; it wants 5: minute hour day month weekday"
            )
        self.expr, self.tz = " ".join(fields), tz
        try:
            first = self.after(now_ns)
        except CronSimError as e:
            raise RotateError(f"{expr!r}: {e}") from None
        if first is None:
            raise RotateError(f"{expr!r} never fires")
        self.next_ns = None

    def __str__(self):
        return self.expr

    def after(self, t_ns):
        """The first fire time after `t_ns`, ns; None if there is none."""
        try:
            fire = next(CronSim(self.expr, datetime.fromtimestamp(t_ns // NS, self.tz)))
        except StopIteration:
            return None
        return int(fire.timestamp()) * NS

    def due(self, t_ns):
        if self.next_ns is None:
            self.next_ns = self.after(t_ns) or -1
            return None
        if self.next_ns < 0 or t_ns < self.next_ns:
            return None
        start = self.next_ns
        nxt = self.after(start)
        while nxt is not None and nxt <= t_ns:
            start, nxt = nxt, self.after(nxt)
        self.next_ns = -1 if nxt is None else nxt
        return start
