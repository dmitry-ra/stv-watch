#!/usr/bin/env python3
"""A2S_INFO / A2S_PLAYER queries (Valve server query protocol), stdlib only.

Asked of a SourceTV relay port, A2S_INFO reports the relay's own connected
client count in `players` (CHLTVServer::ReplyInfo puts GetNumClients() there),
so it is the spectator counter used to prove a slot was released.

Servers patched since late 2020 answer the first query with S2C_CHALLENGE
('A' + 4 bytes); the query is then repeated with that challenge appended.
"""

import socket
import struct

HEADER = b"\xff\xff\xff\xff"
INFO_QUERY = HEADER + b"TSource Engine Query\x00"
S2C_CHALLENGE = 0x41
S2A_INFO = 0x49
S2A_PLAYER = 0x44
A2S_PLAYER = 0x55

EDF_PORT = 0x80
EDF_STEAMID = 0x10
EDF_SOURCETV = 0x40
EDF_KEYWORDS = 0x20
EDF_GAMEID = 0x01


class A2SError(Exception):
    pass


class _Reader:
    def __init__(self, data, pos):
        self.data = data
        self.pos = pos

    def take(self, fmt):
        size = struct.calcsize(fmt)
        if self.pos + size > len(self.data):
            raise A2SError("truncated reply")
        val = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return val[0] if len(val) == 1 else val

    def cstr(self):
        end = self.data.find(b"\x00", self.pos)
        if end < 0:
            raise A2SError("unterminated string")
        s = self.data[self.pos : end].decode("utf-8", "replace")
        self.pos = end + 1
        return s

    def left(self):
        return len(self.data) - self.pos


def _exchange(addr, build, timeout, attempts):
    """Send build(None), follow at most one challenge round with build(chal),
    return the reply. No reply within `attempts` x `timeout` -> A2SError."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        query = build(None)
        for _ in range(attempts):
            sock.sendto(query, addr)
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            if len(data) < 5 or data[:4] != HEADER:
                raise A2SError("unexpected reply %r" % data[:8])
            if data[4] == S2C_CHALLENGE and len(data) >= 9:
                query = build(data[5:9])
                sock.sendto(query, addr)
                try:
                    data, _ = sock.recvfrom(4096)
                except socket.timeout:
                    continue
            return data
        raise A2SError("no reply from %s:%d" % addr)
    finally:
        sock.close()


def parse_info(data):
    if len(data) < 6 or data[:4] != HEADER or data[4] != S2A_INFO:
        raise A2SError("not an S2A_INFO reply")
    r = _Reader(data, 5)
    info = {
        "protocol": r.take("<B"),
        "name": r.cstr(),
        "map": r.cstr(),
        "folder": r.cstr(),
        "game": r.cstr(),
        "appid": r.take("<H"),
        "players": r.take("<B"),
        "max_players": r.take("<B"),
        "bots": r.take("<B"),
        "server_type": chr(r.take("<B")),
        "environment": chr(r.take("<B")),
        "visibility": r.take("<B"),
        "vac": r.take("<B"),
        "version": r.cstr(),
    }
    if r.left():
        edf = r.take("<B")
        if edf & EDF_PORT:
            info["port"] = r.take("<H")
        if edf & EDF_STEAMID:
            info["steamid"] = r.take("<Q")
        if edf & EDF_SOURCETV:
            info["tv_port"] = r.take("<H")
            info["tv_name"] = r.cstr()
        if edf & EDF_KEYWORDS:
            info["keywords"] = r.cstr()
        if edf & EDF_GAMEID:
            info["gameid"] = r.take("<Q")
    return info


def parse_players(data):
    if len(data) < 6 or data[:4] != HEADER or data[4] != S2A_PLAYER:
        raise A2SError("not an S2A_PLAYER reply")
    r = _Reader(data, 5)
    out = []
    for _ in range(r.take("<B")):
        out.append(
            {
                "index": r.take("<B"),
                "name": r.cstr(),
                "score": r.take("<i"),
                "duration": r.take("<f"),
            }
        )
    return out


def info(ip, port, timeout=2.0, attempts=2):
    return parse_info(_exchange((ip, port), lambda ch: INFO_QUERY + (ch or b""), timeout, attempts))


def players(ip, port, timeout=2.0, attempts=2):
    head = HEADER + bytes([A2S_PLAYER])
    return parse_players(_exchange((ip, port), lambda ch: head + (ch or HEADER), timeout, attempts))


def human_count(ip, port, timeout=2.0, attempts=2):
    """Live humans on a GAME port: players minus bots (SourceTV is a bot)."""
    i = info(ip, port, timeout, attempts)
    return max(0, i["players"] - i["bots"]), i


if __name__ == "__main__":
    import json
    import sys

    host, _, p = sys.argv[1].partition(":")
    print(json.dumps(info(host, int(p or 27015)), ensure_ascii=False))
