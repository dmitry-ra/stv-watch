"""SteamID -> nick from the `userinfo` string table of the same stream, no
RCON.

What the relay sends (measured on live relays): each entry's user data is a
player_info_t, `name[32] | i32 userID | guid[33] | pad | u32 friendsID | ...`.
Some servers hand the guid to SourceTV viewers hashed (`STEAM_H:x:n`), so it
cannot be the key; friendsID is the plain Steam account id and equals
SteamID64 minus the individual-account base for every player seen. That equality is the key and
the guard: an entry is attributed only by friendsID, never by slot or guid.

The table bits are scanned at all 8 bit alignments for the guid pattern
instead of walking the string-table entry encoding: entries are bit-packed,
so a struct lands at an arbitrary bit offset, but its fields stay in place
relative to each other. A false match would have to produce a friendsID
equal to a live speaker's account id to be used at all.
"""

import re
import struct

STEAMID64_BASE = 76561197960265728
# Hashed `STEAM_H:x:n` on some servers, SteamID3 `[U:1:n]` on others: the
# format is the server's, the struct around it is the same.
GUID = re.compile(rb"(?:STEAM_[0-9A-Z]:[01]:\d{1,10}|\[U:1:\d{1,10}\])\x00")
NAME_BEFORE_GUID = 36  # name[32] + userID
FRIENDS_AFTER_GUID = 36  # guid[33] + 3 bytes alignment


def scan(payload, start_bit, end_bit):
    """-> [(friends_id, name)] for every player_info_t found in the bit range."""
    n = end_bit - start_bit
    if n <= 0:
        return []
    val = (int.from_bytes(payload, "little") >> start_bit) & ((1 << n) - 1)
    out = []
    for shift in range(8):
        raw = (val >> shift).to_bytes((n + 7) // 8, "little")
        for m in GUID.finditer(raw):
            g = m.start()
            if g < NAME_BEFORE_GUID or g + FRIENDS_AFTER_GUID + 4 > len(raw):
                continue
            friends_id = struct.unpack_from("<I", raw, g + FRIENDS_AFTER_GUID)[0]
            name = raw[g - NAME_BEFORE_GUID : g - 4].split(b"\x00", 1)[0]
            try:
                out.append((friends_id, name.decode("utf-8")))
            except UnicodeDecodeError:
                continue
    return out


# Same struct, bots included: their guid is the literal "BOT" and friendsID 0.
GUID_OR_BOT = re.compile(rb"(?:STEAM_[0-9A-Z]:[01]:\d{1,10}|\[U:1:\d{1,10}\]|BOT)\x00")


def scan_players(payload, start_bit, end_bit):
    """-> [(friends_id, name, userid)] like scan(), plus the server userid
    (the i32 between name and guid; game events name players by it) and bots
    (friends_id 0). A userid outside 1..65535 marks a false match."""
    n = end_bit - start_bit
    if n <= 0:
        return []
    val = (int.from_bytes(payload, "little") >> start_bit) & ((1 << n) - 1)
    out = []
    for shift in range(8):
        raw = (val >> shift).to_bytes((n + 7) // 8, "little")
        for m in GUID_OR_BOT.finditer(raw):
            g = m.start()
            if g < NAME_BEFORE_GUID or g + FRIENDS_AFTER_GUID + 4 > len(raw):
                continue
            userid = struct.unpack_from("<i", raw, g - 4)[0]
            friends_id = struct.unpack_from("<I", raw, g + FRIENDS_AFTER_GUID)[0]
            if not 0 < userid < 65536 or (m.group().startswith(b"BOT") and friends_id):
                continue
            name = raw[g - NAME_BEFORE_GUID : g - 4].split(b"\x00", 1)[0]
            try:
                out.append((friends_id, name.decode("utf-8"), userid))
            except UnicodeDecodeError:
                continue
    return out


class NickBook:
    """Latest nick per Steam account id, fed by the framer's table hook."""

    def __init__(self):
        self.by_account = {}
        self.entries = 0

    def __call__(self, payload, start_bit, end_bit):
        for friends_id, name in scan(payload, start_bit, end_bit):
            if friends_id:
                self.by_account[friends_id] = name
                self.entries += 1

    def nick(self, steamid64):
        return self.by_account.get(steamid64 - STEAMID64_BASE)
