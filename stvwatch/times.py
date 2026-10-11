"""Clock text in a time zone. No dependencies beyond the standard library:
an --attach screen and the network client import it."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NS = 1_000_000_000


def zone(name):
    """An IANA name -> tzinfo; 'UTC' without the time zone database.
    ValueError for an unknown name."""
    if name in (None, "", "UTC"):
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {name!r}") from None


def local(t_ns, tz, fmt=None):
    """Time in `tz`; on screen always with milliseconds (HH:MM:SS.mmm)."""
    if fmt is not None:
        return datetime.fromtimestamp(t_ns / NS, tz).strftime(fmt)
    return datetime.fromtimestamp(t_ns // NS, tz).strftime("%H:%M:%S") + ".%03d" % (
        t_ns % NS // 1_000_000
    )


def tz_label(tz):
    return getattr(tz, "key", None) or "UTC"


def utc_iso(t_ns):
    return (
        datetime.fromtimestamp(t_ns / NS, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )


def stamp(t_ns):
    """A session directory's time: YYYYMMDDTHHMMSSZ."""
    return datetime.fromtimestamp(t_ns // NS, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
