#!/usr/bin/env python3
"""TVDUMP capture file: byte-exact traffic plus typed lifecycle events.

Format (little-endian, version 1):

  header:  magic b"TVDUMP\\n" | u16 version | u64 start_epoch_ns
           | u16 endpoint_len | endpoint (ASCII)
  record:  u64 t_ns | u8 type | u32 len | data

Times are UTC Unix-epoch nanoseconds (`time.time_ns()`), absolute. Datagram
records carry raw bytes; event records carry UTF-8 JSON - events are rare, so
the size cost is nil and the reader stays trivial.

The point of the event records: a session boundary must be IN the file. A dump
without them looks complete while silently splicing two different sessions.
"""

import json
import os
import struct
import time

MAGIC = b"TVDUMP\n"
VERSION = 1

HEADER_HEAD = "<7sHQH"  # magic, version, start_ns, ep_len
HEADER_HEAD_SIZE = struct.calcsize(HEADER_HEAD)
RECORD_HEAD = "<QBI"  # t_ns, type, len
RECORD_HEAD_SIZE = struct.calcsize(RECORD_HEAD)

DATAGRAM_IN = 0x01
DATAGRAM_OUT = 0x02
SESSION_START = 0x10
SIGNON = 0x11
BROKEN = 0x12
RECONNECT = 0x13
MAPCHANGE = 0x14
SPLIT_SEEN = 0x15
LEAVE = 0x16

TYPE_NAMES = {
    DATAGRAM_IN: "DATAGRAM_IN",
    DATAGRAM_OUT: "DATAGRAM_OUT",
    SESSION_START: "SESSION_START",
    SIGNON: "SIGNON",
    BROKEN: "BROKEN",
    RECONNECT: "RECONNECT",
    MAPCHANGE: "MAPCHANGE",
    SPLIT_SEEN: "SPLIT_SEEN",
    LEAVE: "LEAVE",
}
EVENT_TYPES = {SESSION_START, SIGNON, BROKEN, RECONNECT, MAPCHANGE, SPLIT_SEEN, LEAVE}


class DumpError(Exception):
    """Dump I/O failed. Fatal by policy: a run that cannot write has no product."""


class DumpWriter:
    """Append-only writer.

    Durability is two-tier on purpose: flush() every record (survives kill -9 at
    ~zero cost, the data is already in the page cache) but fsync() throttled
    (a per-record fsync on a mechanical disk stalls recvfrom long enough to drop
    live datagrams). Worst case loss is `fsync_ms` of tail, and only on power
    loss.
    """

    def __init__(self, path, endpoint, fsync_ms=1000):
        self.path = path
        self.fsync_interval_ns = max(0, int(fsync_ms)) * 1_000_000
        self._last_fsync = time.monotonic_ns()  # monotonic: an NTP step must
        self.records = 0  # never wedge the gate
        try:
            d = os.path.dirname(os.path.abspath(path))
            os.makedirs(d, exist_ok=True)
            self._f = open(path, "wb")
            ep = endpoint.encode("ascii", "replace")
            self._f.write(struct.pack(HEADER_HEAD, MAGIC, VERSION, time.time_ns(), len(ep)) + ep)
            self._f.flush()
            os.fsync(self._f.fileno())
        except OSError as e:
            raise DumpError(f"cannot open dump {path!r}: {e}") from e

    def write(self, rtype, data, t_ns=None):
        if t_ns is None:
            t_ns = time.time_ns()
        try:
            self._f.write(struct.pack(RECORD_HEAD, t_ns, rtype, len(data)))
            self._f.write(data)
            self._f.flush()
        except OSError as e:
            raise DumpError(f"dump write failed: {e}") from e
        self.records += 1
        now = time.monotonic_ns()
        if self.fsync_interval_ns and now - self._last_fsync >= self.fsync_interval_ns:
            self._sync()
            self._last_fsync = now
        return t_ns

    def event(self, rtype, **fields):
        """Write a lifecycle event; fields are JSON-encoded."""
        return self.write(rtype, json.dumps(fields, sort_keys=True).encode("utf-8"))

    def _sync(self):
        try:
            os.fsync(self._f.fileno())
        except OSError as e:
            raise DumpError(f"dump fsync failed: {e}") from e

    def close(self):
        if getattr(self, "_f", None) is None:
            return
        try:
            self._f.flush()
            self._sync()
        finally:
            self._f.close()
            self._f = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class DumpReader:
    """Read + validate a dump. Tolerates a truncated tail (kill -9 mid-write):
    `truncated_tail` reports it instead of raising, so a killed run is still
    fully analysable up to the cut."""

    def __init__(self, path):
        self.path = path
        self.truncated_tail = False
        with open(path, "rb") as f:
            head = f.read(HEADER_HEAD_SIZE)
            if len(head) < HEADER_HEAD_SIZE:
                raise DumpError("truncated header")
            magic, version, start_ns, ep_len = struct.unpack(HEADER_HEAD, head)
            if magic != MAGIC:
                raise DumpError(f"bad magic {magic!r} (expected {MAGIC!r})")
            if version != VERSION:
                raise DumpError(f"unsupported version {version}")
            ep = f.read(ep_len)
            if len(ep) < ep_len:
                raise DumpError("truncated endpoint")
        self.version = version
        self.start_epoch_ns = start_ns
        self.endpoint = ep.decode("ascii", "replace")
        self._data_offset = HEADER_HEAD_SIZE + ep_len

    def __iter__(self):
        """Yield (t_ns, rtype, data). A short tail sets truncated_tail and stops."""
        with open(self.path, "rb") as f:
            f.seek(self._data_offset)
            while True:
                head = f.read(RECORD_HEAD_SIZE)
                if not head:
                    return
                if len(head) < RECORD_HEAD_SIZE:
                    self.truncated_tail = True
                    return
                t_ns, rtype, length = struct.unpack(RECORD_HEAD, head)
                data = f.read(length)
                if len(data) < length:
                    self.truncated_tail = True
                    return
                yield t_ns, rtype, data

    def events(self):
        """Yield (t_ns, rtype, fields_dict) for event records only."""
        for t_ns, rtype, data in self:
            if rtype in EVENT_TYPES:
                try:
                    yield t_ns, rtype, json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    yield t_ns, rtype, {"_unparsed": data.hex()}
