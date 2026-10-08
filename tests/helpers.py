"""Builders of synthetic relay traffic, written with the client's own packet and
dump writers, so the tests cannot drift from the wire format.

Player ids are Steam accounts above 4,000,000,000: far beyond any allocated
account, so no SteamID here belongs to a person.
"""

import struct

from stvwatch.net import dump, messages, netchan, wire
from stvwatch.stream import streamevents as se
from stvwatch.stream.userinfo import STEAMID64_BASE

CHALLENGE = 0x11223344
SVC_VOICEDATA = 15
T0 = 1_759_830_000 * 10**9  # 2025-10-07 09:40:00 UTC


def account(n):
    """A synthetic Steam account id."""
    return 4_000_000_000 + n


def steamid64(n):
    return STEAMID64_BASE + account(n)


def voice_payload(sid64, n=3):
    """Opaque voice bytes: the client sizes them by the message's length field
    and never reads them, so any content will do; the SteamID leads, as on the
    wire."""
    return struct.pack("<Q", sid64) + bytes(range(40)) * n


def write_voice(w, slot, payload):
    w.write_ubit(SVC_VOICEDATA, wire.NETMSG_TYPE_BITS)
    w.write_byte(slot)
    w.write_byte(0)
    w.write_ubit(len(payload) * 8, 16)
    w.write_bytes(payload)


def ones_pad(w):
    """The sender pads the last byte with 1 bits."""
    pad = (-w.nbits()) % 8
    w.write_ubit((1 << pad) - 1, pad)


def body(fill):
    w = wire.BitWriter()
    fill(w)
    return w


def usermessage(w, mtype, b, declared=None):
    w.write_ubit(se.SVC_USERMESSAGE, wire.NETMSG_TYPE_BITS)
    w.write_ubit(mtype, 8)
    w.write_ubit(b.nbits() if declared is None else declared, 11)
    w.write_bytes(b.get_bytes()[: (b.nbits() + 7) // 8])


def chat(w, nick, text, fmt="HL2MP_Chat_All", ent=1):
    """A SayText2 chat line as the game sends it."""
    usermessage(
        w,
        se.USER_MESSAGES.index("SayText2"),
        body(
            lambda b: (
                b.write_byte(ent),
                b.write_byte(1),
                b.write_string(fmt),
                b.write_string(nick),
                b.write_string(text),
                b.write_string(""),
                b.write_string(""),
            )
        ),
    )
    return w


def chat_bytes(nick, text):
    return chat(wire.BitWriter(), nick, text).get_bytes() + b"\x00"


def packet(seq, voices=(), challenge=CHALLENGE, reliable_first=False, tail=None):
    """In-band packet: svc_VoiceData messages in the unreliable stream, then
    `tail(w)` if given. reliable_first puts a single-block reliable region in
    front, so the unreliable stream starts mid-byte, as on the wire."""
    w = wire.BitWriter()
    if reliable_first:
        b = messages.signonstate_body(wire.SIGNON_SPAWN, 3)
        w.write_ubit(0, wire.SUBCHANNEL_BITS)
        w.write_one_bit(1)
        w.write_one_bit(0)
        w.write_one_bit(0)
        w.write_varint32(len(b))
        w.write_bytes(b)
        w.write_one_bit(0)
    for slot, p in voices:
        write_voice(w, slot, p)
    if tail is not None:
        tail(w)
    ones_pad(w)
    if reliable_first:
        return netchan.build_packet(seq, 1, challenge, 0, reliable_region=w.get_bytes())
    return netchan.build_packet(seq, 1, challenge, 0, unreliable=w.get_bytes())


def reliable_packet(seq, payload, voices=(), sub=0, challenge=CHALLENGE):
    """Reliable single block `payload` on subchannel `sub`, then voice in the
    unreliable tail (the stream starts mid-byte, as on the wire)."""
    w = wire.BitWriter()
    w.write_ubit(sub, wire.SUBCHANNEL_BITS)
    w.write_one_bit(1)
    w.write_one_bit(0)
    w.write_one_bit(0)
    w.write_varint32(len(payload))
    w.write_bytes(payload)
    w.write_one_bit(0)
    for slot, p in voices:
        write_voice(w, slot, p)
    ones_pad(w)
    return netchan.build_packet(seq, 1, challenge, 0, reliable_region=w.get_bytes())


def split(data, group, size=200):
    """`-2` parts of one datagram."""
    parts = [data[i : i + size] for i in range(0, len(data), size)]
    return [
        struct.pack("<iiBBH", wire.MARK_SPLIT, group, len(parts), n, size) + p
        for n, p in enumerate(parts)
    ]


def player_info(name, guid, friends_id, userid=5):
    """player_info_t as the userinfo table carries it."""
    s = name.encode().ljust(32, b"\0") + struct.pack("<i", userid)
    s += guid.encode().ljust(33, b"\0") + b"\0" * 3 + struct.pack("<I", friends_id)
    return s + b"\0" * 40


def table_update(*players):
    """svc_UpdateStringTable of the userinfo table with these player_info_t
    blobs; players are (name, guid, friends_id[, userid])."""
    blob = b"\x07" + b"".join(player_info(*p) for p in players)
    w = wire.BitWriter()
    w.write_ubit(13, wire.NETMSG_TYPE_BITS)
    w.write_ubit(7, 5)
    w.write_one_bit(0)
    w.write_ubit(len(blob) * 8, 20)
    w.write_bytes(blob)
    return w.get_bytes()


def write_recording(path, timed, endpoint="127.0.0.1:27020"):
    """timed: [(t_ns, datagram)] as DATAGRAM_IN records."""
    with dump.DumpWriter(path, endpoint, fsync_ms=0) as w:
        for t, d in timed:
            w.write(dump.DATAGRAM_IN, d, t_ns=t)
