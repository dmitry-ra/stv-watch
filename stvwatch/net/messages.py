#!/usr/bin/env python3
"""Outbound message builders + the few inbound parsers the lifecycle needs.

Everything outbound is generated; no captured packet is ever replayed. The
inbound side deliberately parses only what drives the state machine - signon
state, spawncount, disconnect reason, tick. Payloads stay opaque.
"""

from . import wire
from .wire import BitWriter

# The userinfo cvar set a stock hl2mp client sends at CONNECTED, in wire order.
# Values are the engine defaults a fresh client reports; `name` is overridden per
# run. Order matters only for byte-identity with the reference capture - the
# server reads them as an unordered set.
DEFAULT_USERINFO = [
    ("cl_interp_npcs", "0"),
    ("cl_thirdperson", "0"),
    ("cl_team", "default"),
    ("cl_class", "default"),
    ("cl_predict", "1"),
    ("cl_interp_ratio", "2"),
    ("cl_interp", "0.1"),
    ("cl_showhelp", "1"),
    ("english", "1"),
    ("cl_predictweapons", "1"),
    ("cl_lagcompensation", "1"),
    ("cl_spec_mode", "1"),
    ("cl_playermodel", "none"),
    ("cl_defaultweapon", "weapon_physcannon"),
    ("cl_autowepswitch", "1"),
    ("name", "unnamed"),
    ("cl_interpolate", "1"),
    ("cl_clanid", "0"),
    ("cl_connectmethod", ""),
    ("tv_nochat", "0"),
    ("cl_language", "english"),
    ("rate", "80000"),
    ("cl_cmdrate", "30"),
    ("cl_updaterate", "20"),
    ("closecaption", "0"),
    ("net_maxroutable", "1260"),
    ("voice_loopback", "0"),
]


def userinfo_for(name):
    """DEFAULT_USERINFO with `name` substituted, order preserved."""
    return [(k, name if k == "name" else v) for k, v in DEFAULT_USERINFO]


# --- outbound message bodies (message-chain bytes, byte-padded) -------------


def setconvar(cvars):
    """net_SetConVar: type(6) | u8 count | count x (cstr key, cstr value)."""
    w = BitWriter()
    w.write_ubit(wire.NET_SETCONVAR, wire.NETMSG_TYPE_BITS)
    w.write_byte(len(cvars))
    for k, v in cvars:
        w.write_string(k)
        w.write_string(v)
    return w


def signonstate(state, spawncount, w=None):
    """net_SignonState: type(6) | u8 state | i32 spawncount."""
    w = w or BitWriter()
    w.write_ubit(wire.NET_SIGNONSTATE, wire.NETMSG_TYPE_BITS)
    w.write_byte(state)
    w.write_long(spawncount)
    return w


def connected_reply_body(name, spawncount=-1):
    """The CONNECTED-stage message stream: net_SetConVar(userinfo) then
    net_SignonState(CONNECTED, spawncount). Both messages share one stream."""
    w = setconvar(userinfo_for(name))
    signonstate(wire.SIGNON_CONNECTED, spawncount, w)
    return w.get_bytes()


def clientinfo_body(spawncount, sendtable_crc=0, is_hltv=True, replay_bit=True):
    """CLC_ClientInfo followed by net_SignonState(NEW, spawncount).

    `replay_bit` appends m_bIsReplay - present only on replay-enabled builds.
    Omit it there and the server's read misaligns; send it on a build that has
    no such field and the read misaligns the other way. Detected, not assumed.
    """
    w = BitWriter()
    w.write_ubit(wire.CLC_CLIENTINFO, wire.NETMSG_TYPE_BITS)
    w.write_long(spawncount)
    w.write_long(sendtable_crc)
    w.write_one_bit(1 if is_hltv else 0)
    w.write_long(0)  # m_nFriendsID
    w.write_string("")  # m_FriendsName
    for _ in range(wire.MAX_CUSTOM_FILES):
        w.write_one_bit(0)  # no custom files
    if replay_bit:
        w.write_one_bit(0)  # m_bIsReplay
    signonstate(wire.SIGNON_NEW, spawncount, w)
    return w.get_bytes()


def signonstate_body(state, spawncount):
    """A bare net_SignonState echo as its own message stream."""
    return signonstate(state, spawncount).get_bytes()


def disconnect_body(reason):
    """net_Disconnect: type(6) | cstr reason. Sent unreliable, alone, as
    CNetChan::Shutdown does; the peer drops the channel on reading it."""
    w = BitWriter()
    w.write_ubit(wire.NET_DISCONNECT, wire.NETMSG_TYPE_BITS)
    w.write_string(reason)
    return w.get_bytes()


def tick_body(tick):
    """net_Tick: type(6) | i32 tick | u16 frametime | u16 stddev.

    Unreliable - rides the plain message region, no subchannel. Acking the tick
    clears the server's wait-for-full-update gate.
    """
    w = BitWriter()
    w.write_ubit(wire.NET_TICK, wire.NETMSG_TYPE_BITS)
    w.write_long(tick)
    w.write_ubit(0, 16)
    w.write_ubit(0, 16)
    return w.get_bytes()


def reliable_region(body, subchannel):
    """Wrap one message stream as a single-block reliable subchannel region.

    Layout: ubit(3) subchannel | stream0: data-follows=1, single=0, compressed=0,
    varint32 nbytes, payload | stream1: data-follows=0.
    Note the asymmetry the engine uses: a SINGLE block sizes with varint32, a
    multi-fragment block with ubit(26).
    """
    w = BitWriter()
    w.write_ubit(subchannel, wire.SUBCHANNEL_BITS)
    w.write_one_bit(1)  # stream 0: data follows
    w.write_one_bit(0)  # single block
    w.write_one_bit(0)  # not compressed
    w.write_varint32(len(body))
    w.write_bytes(body)
    w.write_one_bit(0)  # stream 1: nothing
    return w.get_bytes()


# --- inbound: only what the lifecycle needs --------------------------------


def parse_serverinfo(br, replay_bit):
    """svc_ServerInfo -> dict. `replay_bit` consumes the trailing m_bIsReplay.

    Reads to the end of the message so the walker can continue; the caller
    decides which replay_bit variant walked further.
    """
    info = {}
    info["protocol"] = br.read_ubit(16)
    info["spawncount"] = br.read_long()
    info["is_hltv"] = br.read_one_bit()
    info["is_dedicated"] = br.read_one_bit()
    br.read_ulong()  # legacy client CRC
    info["max_classes"] = br.read_ubit(16)
    br.read_bytes(16)  # map MD5
    info["player_slot"] = br.read_byte()
    info["max_clients"] = br.read_byte()
    br.read_ulong()  # tick interval (float bits)
    info["os"] = br.read_byte()
    info["gamedir"] = br.read_string()
    info["map"] = br.read_string()
    info["sky"] = br.read_string()
    info["hostname"] = br.read_string()
    if replay_bit and br.bits_left() >= 1:
        info["is_replay"] = br.read_one_bit()
    return info


def parse_signonstate(br):
    return {"state": br.read_byte(), "spawncount": br.read_long()}


def parse_disconnect(br):
    return {"reason": br.read_string()}


def parse_tick(br):
    tick = br.read_long()
    br.read_ubit(16)
    br.read_ubit(16)
    return {"tick": tick}
