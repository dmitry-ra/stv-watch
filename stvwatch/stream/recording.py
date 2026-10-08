"""Read inbound datagrams from a capture, in file order.

`.tvd` (written by the network client, magic TVDUMP) and the older `.hcap`
(magic HLTVCAP, datagrams only) share the record layout
`u64 t_ns | u8 type | u32 len | data` and differ in magic and in which record
types exist. Both store the raw datagram before any parsing, so a recording is
the complete input of everything after the socket.

Session boundaries matter to reassembly state (a split group or a reliable
transfer never spans a fresh handshake): `.tvd` marks them with SESSION_START /
RECONNECT events; `.hcap` has no events, so there the inbound connectionless
accept 'B' is taken as the boundary. One source per format, so one handshake
counts once.
"""

import os
import struct
import time
from dataclasses import dataclass

TVD_MAGIC = b"TVDUMP\n"
HCAP_MAGIC = b"HLTVCAP\n"

TVD_DATAGRAM_IN = 0x01
TVD_SESSION_START = 0x10
TVD_RECONNECT = 0x13
HCAP_DIR_IN = 0x01

RECORD_HEAD = struct.Struct("<QBI")

OOB = b"\xff\xff\xff\xff"
S2C_CONNECTION = ord("B")


@dataclass(frozen=True)
class Datagram:
    """One inbound datagram as received."""

    index: int  # ordinal among inbound datagrams of this recording
    t_ns: int  # receive time, UTC epoch ns (the client clock)
    session: int  # increments on every handshake boundary
    data: bytes


class RecordingError(Exception):
    pass


def _open(path):
    f = open(path, "rb")
    head = f.read(8)
    if head.startswith(TVD_MAGIC):
        f.seek(7)
        version, start_ns, ep_len = struct.unpack("<HQH", f.read(12))
        kind = "tvd"
    elif head == HCAP_MAGIC:
        version, start_ns, ep_len = struct.unpack("<HQH", f.read(12))
        kind = "hcap"
    else:
        f.close()
        raise RecordingError(f"{path}: unknown magic {head!r}")
    endpoint = f.read(ep_len).decode("ascii", "replace")
    return f, kind, version, start_ns, endpoint


class Recording:
    """Iterable over Datagram. A truncated tail stops iteration and is
    reported in `truncated_tail`, as both writers allow a killed run.

    follow=True is the live source: the same reader tails a journal that the
    network client is still appending to, so live and replay share one code
    path after the reader. A short read waits `poll_s` and retries from the
    same offset; iteration ends when `stop()` returns true or nothing new has
    arrived for `idle_s`.

    on_event(t_ns, rtype, data), if given, receives every non-inbound record
    (lifecycle events, outbound datagrams) in file order."""

    def __init__(self, path, follow=False, poll_s=0.05, idle_s=10.0, stop=None, on_event=None):
        self.path = path
        self.follow = follow
        self.poll_s = poll_s
        self.idle_s = idle_s
        self.stop = stop or (lambda: False)
        self.on_event = on_event
        f, self.kind, self.version, self.start_ns, self.endpoint = _open(path)
        self._data_offset = f.tell()
        f.close()
        self.size = os.path.getsize(path)
        self.truncated_tail = False

    def __iter__(self):
        index = 0
        session = 0
        boundary_pending = False
        is_tvd = self.kind == "tvd"
        with open(self.path, "rb", buffering=1 << 20) as f:
            f.seek(self._data_offset)
            read = self._follow_read(f) if self.follow else f.read
            unpack = RECORD_HEAD.unpack
            size = RECORD_HEAD.size
            while True:
                head = read(size)
                if not head:
                    return
                if len(head) < size:
                    self.truncated_tail = True
                    return
                t_ns, rtype, length = unpack(head)
                data = read(length)
                if len(data) < length:
                    self.truncated_tail = True
                    return
                if rtype != TVD_DATAGRAM_IN and self.on_event is not None:
                    self.on_event(t_ns, rtype, data)
                if is_tvd and rtype in (TVD_SESSION_START, TVD_RECONNECT):
                    boundary_pending = True
                    continue
                if rtype != TVD_DATAGRAM_IN:  # same value in both formats
                    continue
                if not is_tvd and len(data) > 4 and data[:4] == OOB and data[4] == S2C_CONNECTION:
                    boundary_pending = True
                if boundary_pending and index:
                    session += 1
                boundary_pending = False
                yield Datagram(index, t_ns, session, data)
                index += 1

    def _follow_read(self, f):
        """read(n) that waits for a growing file: returns n bytes, or what is
        there when the source went idle or stop() fired."""

        def read(n):
            buf = f.read(n)
            idle_since = time.monotonic()
            while len(buf) < n and not self.stop():
                if time.monotonic() - idle_since > self.idle_s:
                    break
                time.sleep(self.poll_s)
                more = f.read(n - len(buf))
                if more:
                    buf += more
                    idle_since = time.monotonic()
            return buf

        return read
