"""The `userinfo` string table of the stream, decoded entry by entry: SteamID
-> nick and slot -> player, no RCON.

Entries are read as Source 2013 CNetworkStringTable::ParseUpdate writes them
(index, key with a 32-string prefix history, user data); tables are numbered
in creation order and created anew after every svc_ServerInfo. The entry
index of `userinfo` is the client slot, the `from_client` of svc_VoiceData.
Each entry's user data is a player_info_t, `name[32] | i32 userID | guid[33]
| pad | u32 friendsID | ...`; an entry without user data is an empty slot.
Some servers hand the guid to SourceTV viewers hashed (`STEAM_H:x:n`), so it
cannot be the key; friendsID is the plain Steam account id, SteamID64 minus
the individual-account base. Bots have the guid `BOT`; some servers fill
their friendsID with made-up ids, so a bot's is taken as 0.
"""

import struct
from typing import NamedTuple

from ..net.wire import BitReader

STEAMID64_BASE = 76561197960265728
TABLE_CREATE, TABLE_UPDATE = 12, 13
MAX_TABLES_BITS = 5
SUBSTRING_BITS = 5
HISTORY = 32
MAX_USERDATA_BITS = 14
MAX_STRING = 1024
PLAYER_INFO_MIN = 76  # up to and including friendsID


class Player(NamedTuple):
    friends_id: int
    name: str
    userid: int


def player_info(ud):
    """player_info_t bytes -> Player, None when too short to be one."""
    if ud is None or len(ud) < PLAYER_INFO_MIN:
        return None
    # a nick of 31 bytes may end in a cut multibyte character
    name = ud[:32].split(b"\0", 1)[0].decode("utf-8", "replace").rstrip("\ufffd")
    friends_id = 0 if ud[36:40] == b"BOT\0" else struct.unpack_from("<I", ud, 72)[0]
    return Player(friends_id, name, struct.unpack_from("<i", ud, 32)[0])


def lzss(blob):
    """NET_BufferToBufferDecompress: `LZSS` u32 size, then the stream; other
    data is taken as is."""
    if blob[:4] != b"LZSS":
        return blob
    size = struct.unpack_from("<I", blob, 4)[0]
    src, out, cmd, get = 8, bytearray(), 0, 0
    while src < len(blob):
        if not get:
            cmd, src = blob[src], src + 1
        get = (get + 1) & 7
        if cmd & 1:
            pos = blob[src] << 4 | blob[src + 1] >> 4
            count = (blob[src + 1] & 0x0F) + 1
            src += 2
            if count == 1:
                break
            at = len(out) - pos - 1
            if at < 0:
                raise ValueError("lzss reference before start")
            for i in range(count):
                out.append(out[at + i])
        else:
            out.append(blob[src])
            src += 1
        cmd >>= 1
    if len(out) != size:
        raise ValueError(f"lzss size {len(out)} != {size}")
    return bytes(out)


def _read_cstring(br):
    """bf_read::ReadString: what does not fit is read and dropped."""
    out = bytearray()
    while c := br.read_ubit(8):
        if len(out) < MAX_STRING - 1:
            out.append(c)
    return bytes(out)


class Table:
    def __init__(self, name, max_entries, fixed_bytes=0, fixed_bits=0):
        self.name = name
        self.max_entries = max_entries
        self.entry_bits = max(max_entries, 1).bit_length() - 1  # Q_log2
        self.fixed_bytes, self.fixed_bits = fixed_bytes, fixed_bits
        self.keys = []
        self.data = []

    def parse(self, br, count):
        """Apply `count` entries -> indices touched, in order; all of them or,
        when the entries do not decode, none."""
        keys, data = list(self.keys), list(self.data)
        last, hist, touched = -1, [], []
        for _ in range(count):
            idx = last + 1 if br.read_one_bit() else br.read_ubit(self.entry_bits)
            last = idx
            if idx >= self.max_entries:
                raise ValueError(f"entry {idx} of {self.max_entries}")
            key = None
            if br.read_one_bit():
                if br.read_one_bit():
                    at, n = br.read_ubit(5), br.read_ubit(SUBSTRING_BITS)
                    key = hist[at][:n] + _read_cstring(br)
                else:
                    key = _read_cstring(br)
            ud = None
            if br.read_one_bit():
                if self.fixed_bytes:
                    v = br.read_ubit(self.fixed_bits)
                    ud = v.to_bytes(self.fixed_bytes, "little")
                else:
                    ud = br.read_bytes(br.read_ubit(MAX_USERDATA_BITS))
            if idx < len(keys):
                data[idx] = ud
            else:
                idx = len(keys)  # AddString: the table grows by one
                keys.append(key or b"")
                data.append(ud)
            hist.append(keys[idx])
            if len(hist) > HISTORY:
                hist.pop(0)
            touched.append(idx)
        self.keys, self.data = keys, data
        return touched


class StringTables:
    """The string tables of one connection, fed every svc_CreateStringTable and
    svc_UpdateStringTable in stream order (Framer.on_table) and reset on
    svc_ServerInfo; a new session starts empty too."""

    def __init__(self):
        self.session = None
        self.tables = []
        self.errors = 0

    def reset(self):
        self.tables = []

    def _at(self, session):
        if session != self.session:
            self.session = session
            self.tables = []

    def userinfo(self, session=None):
        self._at(session)
        return next((t for t in self.tables if t.name == "userinfo"), None)

    def owner(self, slot, session=None):
        """The Player in the slot now; None with no such entry or no table yet."""
        t = self.userinfo(session)
        if t is None or slot >= len(t.data):
            return None
        return player_info(t.data[slot])

    def feed(self, payload, start, end, session=None):
        """One table message (bits start..end after its 6-bit id) -> (mid,
        [(slot, Player | None)]) of the userinfo entries it touched; None is
        an emptied slot. A message that does not decode is counted in
        `errors` and changes nothing."""
        self._at(session)
        n = end - start + 6
        # a reader of this message alone: a misread runs out, not into the next message
        bits = (int.from_bytes(payload, "little") >> (start - 6)) & ((1 << n) - 1)
        br = BitReader(bits.to_bytes((n + 7) // 8, "little"))
        mid = br.read_ubit(6)
        try:
            if mid == TABLE_CREATE:
                t, touched = self._create(br)
            else:
                t, touched = self._update(br)
        except (EOFError, ValueError, IndexError):
            self.errors += 1
            return mid, []
        if t is None or t.name != "userinfo":
            return mid, []
        return mid, [(i, player_info(t.data[i])) for i in touched]

    def _create(self, br):
        save = br.pos
        if br.read_ubit(8) != ord(":"):  # ':' prefixes a filenames table
            br.pos = save
        name = _read_cstring(br).decode("utf-8", "replace")
        max_entries = br.read_ubit(16)
        t0 = Table(name, max_entries)
        count = br.read_ubit(t0.entry_bits + 1)
        br.read_varint32()  # data length in bits
        if br.read_one_bit():
            t0.fixed_bytes, t0.fixed_bits = br.read_ubit(12), br.read_ubit(4)
        compressed = br.read_one_bit()
        self.tables.append(t0)
        if name != "userinfo":
            return t0, []
        if compressed:
            br.read_ulong()  # uncompressed size, LZSS repeats it
            data = lzss(br.read_bytes(br.read_ulong()))
            return t0, t0.parse(BitReader(data), count)
        return t0, t0.parse(br, count)

    def _update(self, br):
        tid = br.read_ubit(MAX_TABLES_BITS)
        count = br.read_ubit(16) if br.read_one_bit() else 1
        br.read_ubit(20)  # length in bits
        if tid >= len(self.tables) or self.tables[tid].name != "userinfo":
            return None, []
        t = self.tables[tid]
        return t, t.parse(br, count)


class NickBook:
    """Latest nick per Steam account id, fed the decoded userinfo entries."""

    def __init__(self):
        self.by_account = {}
        self.entries = 0

    def update(self, players):
        for _slot, p in players:
            if p is not None and p.friends_id:
                self.by_account[p.friends_id] = p.name
                self.entries += 1

    def nick(self, steamid64):
        return self.by_account.get(steamid64 - STEAMID64_BASE)
