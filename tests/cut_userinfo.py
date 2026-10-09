"""Cut tests/data/userinfo_cut.tvd from a relay recording: the svc_ServerInfo,
string table and voice messages of a time window, everything else dropped,
the people taken out.

    uv run python tests/cut_userinfo.py RECORDING FROM_UTC TO_UTC [OUT.tvd]

The userinfo table messages keep the server's own bits; only the player_info_t
bytes inside them are overwritten (same length, so the entry encoding around
them is untouched) with made-up names and accounts, the same made-up account
for the same real one. Other tables become empty tables of the same name, so
the table numbering stays; their updates are dropped. svc_ServerInfo keeps
the map and loses the host name. Voice messages keep their slot and carry
only a SteamID (the made-up one) and zeros: the first one of each slot after
each table message, so every slot is heard after every change.
"""

import os
import struct
import sys
from datetime import datetime, timezone

from helpers import T0, account, ones_pad, player_info, write_create, write_recording, write_voice

from stvwatch.net import netchan, wire
from stvwatch.stream import framing, recording, userinfo

HERE = os.path.dirname(os.path.abspath(__file__))


class Positions(wire.BitReader):
    """BitReader that notes where each byte run was read: the user data."""

    runs = []

    def read_bytes(self, n):
        Positions.runs.append((self.pos, n))
        return super().read_bytes(n)


def server_info(w, map_name):
    w.write_ubit(wire.SVC_SERVERINFO, wire.NETMSG_TYPE_BITS)
    w.write_ubit(wire.PROTOCOL_VERSION, 16)
    w.write_long(1)
    w.write_one_bit(1)
    w.write_one_bit(1)
    w.write_long(0)
    w.write_ubit(200, 16)
    w.write_bytes(b"\0" * 16)
    w.write_byte(0)
    w.write_byte(34)
    w.write_long(0x3C888889)
    w.write_byte(ord("l"))
    w.write_string("hl2mp")
    w.write_string(map_name)
    w.write_string("sky_day01_01")
    w.write_string("fixture")
    w.write_one_bit(0)


def utc_ns(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 10**9)


def cut(src, t_from, t_to):
    tables = userinfo.StringTables()
    accounts = {}  # real account -> made-up index

    def fake(acc):
        return account(accounts.setdefault(acc, len(accounts) + 1))

    out = []  # (t_ns, [writer function])
    msgs = []
    heard = set()  # slots heard since the last table message
    fr = None

    def on_table(payload, start, end):
        heard.clear()
        Positions.runs = []
        userinfo.BitReader = Positions
        try:
            mid, _entries = tables.feed(payload, start, end, fr.cur_session)
        finally:
            userinfo.BitReader = wire.BitReader
        t = tables.tables[-1] if mid == userinfo.TABLE_CREATE else None
        if t is not None and t.name != "userinfo":
            name, size = t.name, t.max_entries
            msgs.append(lambda w: write_create(w, [], name=name, max_entries=size))
            return
        ui = tables.userinfo(fr.cur_session)
        if ui is None or (mid == userinfo.TABLE_UPDATE and not _entries):
            return
        # positions are in the decoder's copy of this message alone
        n = end - start + wire.NETMSG_TYPE_BITS
        val = (int.from_bytes(payload, "little") >> (start - wire.NETMSG_TYPE_BITS)) & (
            (1 << n) - 1
        )
        for pos, size in Positions.runs:
            if pos + 8 * size > n:
                raise SystemExit("user data outside the message: a compressed table?")
            p = userinfo.player_info(
                ((val >> pos) & ((1 << 8 * size) - 1)).to_bytes(size, "little")
            )
            if p is None:
                continue
            bot = p.friends_id == 0
            acc = 0 if bot else fake(p.friends_id)
            name = f"bot{p.userid}" if bot else f"player{accounts[p.friends_id]}"
            new = player_info(name, "BOT" if bot else f"[U:1:{acc}]", acc, p.userid)
            new = new.ljust(size, b"\0")[:size]
            val = val & ~(((1 << (8 * size)) - 1) << pos) | int.from_bytes(new, "little") << pos
        msgs.append(lambda w, v=val, k=n: w.write_ubit(v, k))

    def slot_owner(slot, session):
        msgs.append(("voice", slot))
        return tables.owner(slot, session)

    fr = framing.Framer(
        keep_fates=False,
        on_table=on_table,
        on_info=lambda f: (tables.reset(), heard.clear(), msgs.append(("info", f["map"]))),
        slot_owner=slot_owner,
    )
    for dg in framing.frame(recording.Recording(src), fr):
        voice = iter(fr.voice)
        keep = []
        for m in msgs:
            if isinstance(m, tuple) and m[0] == "voice":
                v = next(voice)
                if v.from_client in heard or len(v.data) < 8:
                    continue
                heard.add(v.from_client)
                acc = struct.unpack_from("<Q", v.data)[0] - userinfo.STEAMID64_BASE
                sid = userinfo.STEAMID64_BASE + fake(acc)
                keep.append(
                    lambda w, s=v.from_client, p=sid: write_voice(w, s, struct.pack("<Q", p))
                )
            elif isinstance(m, tuple):
                keep.append(lambda w, name=m[1]: server_info(w, name))
            else:
                keep.append(m)
        msgs.clear()
        fr.voice.clear()
        if t_from <= dg.t_ns < t_to and keep:
            out.append((dg.t_ns, keep))
        if dg.t_ns >= t_to:
            break
    return out


def write(out, path):
    timed = []
    for seq, (t, fills) in enumerate(out, 1):
        w = wire.BitWriter()
        for f in fills:
            f(w)
        ones_pad(w)
        data = netchan.build_packet(seq, 1, 0x11223344, 0, unreliable=w.get_bytes())
        timed.append((T0 + t - out[0][0], data))
    write_recording(path, timed)


if __name__ == "__main__":
    src, a, b = sys.argv[1:4]
    path = sys.argv[4] if len(sys.argv) > 4 else os.path.join(HERE, "data", "userinfo_cut.tvd")
    write(cut(src, utc_ns(a), utc_ns(b)), path)
    print(path, os.path.getsize(path), "bytes")
