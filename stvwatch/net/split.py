"""Reassembly of `-2` split datagrams.

Header, 12 bytes, little-endian:

    i32  -2              NET_HEADER_FLAG_SPLITPACKET
    i32  group id        one id per split packet; increments per packet
    u8   part count      total parts in the group
    u8   part number     0-based
    u16  split size      payload bytes per full part (1248 on the wire seen)

Byte order of count/number: count first, number second (public reference:
xPaw/PHP-Source-Query BaseSocket.php, Source branch: ReadByte count, then
ReadByte number). Confirmed on recorded traffic: within a group the byte at offset 8 is
constant and the byte at offset 9 runs 0..count-1. The joined payload is a
complete datagram again (in practice `-3 SNAP`), so it re-enters the normal
classify path.

Several groups may be open at once: the relay paces parts (~160 ms apart
measured), so in-band datagrams and other groups can interleave. Every part's
fate is recorded, which is what the loss analysis needs.
"""

import struct
from collections import Counter
from dataclasses import dataclass, field

SPLIT_MARK = -2
HEADER = struct.Struct("<iiBBH")
HEADER_SIZE = HEADER.size
MAX_PARTS = 128  # u8 count; anything near it is corrupt anyway
MAX_JOINED = 1 << 18  # joined packet cap; a netchannel packet is ~1-30 KB

# Part fates.
USED = "split_used"
DUPLICATE = "split_duplicate"
BAD_HEADER = "split_bad_header"
TIMEOUT = "split_orphan_timeout"
SESSION_END = "split_orphan_session_end"
EOF = "split_orphan_eof"
SIZE_MISMATCH = "split_size_mismatch"
SUPERSEDED = "split_superseded"


@dataclass(frozen=True)
class Joined:
    """A reassembled datagram. Index/time are those of the completing part."""

    index: int
    t_ns: int
    data: bytes
    group: int
    parts: tuple  # datagram indices of the parts, in part order
    first_t_ns: int


@dataclass
class _Group:
    count: int
    split_size: int
    first_t_ns: int
    parts: dict = field(default_factory=dict)  # number -> (index, payload)


def parse_header(data):
    """-> (group, count, number, split_size) or None if not a sane header."""
    if len(data) < HEADER_SIZE:
        return None
    mark, group, count, number, split_size = HEADER.unpack_from(data, 0)
    if mark != SPLIT_MARK or count == 0 or number >= count or count > MAX_PARTS:
        return None
    return group, count, number, split_size


class SplitReassembler:
    """Feed split datagrams in arrival order; get Joined packets back.

    One instance per connection: a group never spans a handshake. `fates` maps
    datagram index -> fate for every split datagram fed with an index, filled
    when the fate is final (a part waiting in an open group has none yet).
    """

    def __init__(self, timeout_ns=5_000_000_000):
        self.timeout_ns = timeout_ns
        self.open = {}  # group id -> _Group
        self.fates = {}
        self.counters = Counter()
        self.max_open = 0
        self.max_span_ns = 0
        self._auto_index = 0

    def _drop_group(self, gid, fate):
        g = self.open.pop(gid)
        for idx, _payload in g.parts.values():
            self._set_fate(idx, fate)
        self.counters["groups_" + fate] += 1

    def expire(self, now_ns):
        for gid in [g for g, grp in self.open.items() if now_ns - grp.first_t_ns > self.timeout_ns]:
            self._drop_group(gid, TIMEOUT)

    def _set_fate(self, index, fate):
        self.fates[index] = fate
        self.counters[fate] += 1

    def end_session(self):
        for gid in list(self.open):
            self._drop_group(gid, SESSION_END)

    def finish(self):
        for gid in list(self.open):
            self._drop_group(gid, EOF)

    def feed(self, data, t_ns, index=None):
        """data starts with -2. -> Joined | None. `index` names the datagram in
        `fates`; live callers may omit it."""
        if index is None:
            index = self._auto_index
            self._auto_index += 1
        self.expire(t_ns)
        hdr = parse_header(data)
        if hdr is None:
            self._set_fate(index, BAD_HEADER)
            return None
        gid, count, number, split_size = hdr
        payload = data[HEADER_SIZE:]
        g = self.open.get(gid)
        if g is not None and (g.count != count or g.split_size != split_size):
            # Same id, different shape: the old group cannot complete.
            self._drop_group(gid, SUPERSEDED)
            g = None
        if g is None:
            g = self.open[gid] = _Group(count, split_size, t_ns)
            self.counters["groups_started"] += 1
            self.max_open = max(self.max_open, len(self.open))
        if number in g.parts:
            self._set_fate(index, DUPLICATE)
            return None
        if len(payload) > split_size or (number < count - 1 and len(payload) != split_size):
            self._set_fate(index, SIZE_MISMATCH)
            return None
        g.parts[number] = (index, payload)
        if len(g.parts) < count:
            return None
        del self.open[gid]
        joined = b"".join(g.parts[n][1] for n in range(count))
        indices = tuple(g.parts[n][0] for n in range(count))
        for idx in indices:
            self.fates[idx] = USED
        self.counters[USED] += count
        self.counters["groups_completed"] += 1
        self.max_span_ns = max(self.max_span_ns, t_ns - g.first_t_ns)
        if len(joined) > MAX_JOINED:
            self.counters["groups_oversize"] += 1
            return None
        return Joined(index, t_ns, joined, gid, indices, g.first_t_ns)
