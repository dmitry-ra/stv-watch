#!/usr/bin/env python3
"""Message-chain walker.

Messages in a stream carry a 6-bit id and NO length prefix, so a message that
cannot be sized cannot be skipped - the walk must stop at the first unknown id.
That is a hard property of the format, not a shortcoming here.

Only the lifecycle messages are DECODED. Everything else gets a sizer whose sole
job is to advance the cursor by the exact bit count so the walk stays aligned
long enough to reach the next `svc_SignonState`. Sizers are added as live data
proves them necessary, not speculatively.
"""

from . import messages, wire


class ChainError(Exception):
    """`kind` names the failure in fates; the exception type by default."""

    def __init__(self, text, kind=None):
        super().__init__(text)
        self.kind = kind


def _skip(br, nbits):
    if nbits < 0 or nbits > br.bits_left():
        raise ChainError(f"skip {nbits}b past end")
    br.pos += nbits


def _q_log2(n):
    """Engine Q_log2: floor(log2(n)). Used for svc_CreateStringTable's entry
    width. NOT interchangeable with _bits_for below - they differ by one, and
    one extra bit misaligns the rest of the chain."""
    r = 0
    while (1 << (r + 1)) <= n:
        r += 1
    return r


def _bits_for(n):
    """Smallest width holding values 0..n, i.e. ceil(log2(n+1)). Used for
    svc_ClassInfo's class ids."""
    bits = 1
    while (1 << bits) < n + 1:
        bits += 1
    return bits


# --- sizers: consume exactly, decode nothing -------------------------------


def _s_nop(br):
    pass


def _s_string(br):
    br.read_string()


def _s_file(br):
    br.read_ulong()
    br.read_string()
    br.read_one_bit()


def _s_tick(br):
    br.read_long()
    br.read_ubit(16)
    br.read_ubit(16)


def _s_setconvar(br):
    for _ in range(br.read_byte()):
        br.read_string()
        br.read_string()


def _s_signonstate(br):
    br.read_byte()
    br.read_long()


def _s_sendtable(br):
    br.read_one_bit()
    _skip(br, br.read_ubit(16))


def _s_classinfo(br):
    nc = br.read_ubit(16)
    if not br.read_one_bit():  # not create-on-client
        bits = _bits_for(nc)
        for _ in range(nc):
            br.read_ubit(bits)
            br.read_string()
            br.read_string()


def _s_setpause(br):
    br.read_one_bit()


def _s_createstringtable(br):
    save = br.pos
    if br.read_ubit(8) != ord(":"):  # ':' prefixes a filenames table
        br.pos = save
    br.read_string()  # table name
    max_entries = br.read_ubit(16)
    br.read_ubit(_q_log2(max_entries) + 1)  # num_entries
    length_bits = br.read_varint32()
    if br.read_one_bit():  # user-data fixed size
        br.read_ubit(12)
        br.read_ubit(4)
    br.read_one_bit()  # data compressed
    _skip(br, length_bits)


def _read_bitcoord(br):
    """CBitRead::ReadBitCoord: integer-present bit, fraction-present bit, then a
    sign bit and the present parts. Only needed to size svc_BSPDecal."""
    has_int = br.read_one_bit()
    has_frac = br.read_one_bit()
    if has_int or has_frac:
        br.read_one_bit()  # sign
        if has_int:
            br.read_ubit(14)  # COORD_INTEGER_BITS
        if has_frac:
            br.read_ubit(5)  # COORD_FRACTIONAL_BITS


def _s_bspdecal(br):
    """svc_BSPDecal - decals from bullet impacts and sprays.

    Measured cost of its absence: on maps with active shooting this message
    interleaves into the signon chain, the walk stopped here, and every
    svc_SignonState behind it was lost - 8 of 17 servers never left NEW.
    """
    # BitVec3Coord: ALL THREE present-flags first, THEN the present coords.
    # Interleaving flag/coord misaligns the chain (tried; produced impossible
    # message ids above 32 on live data).
    xf, yf, zf = br.read_one_bit(), br.read_one_bit(), br.read_one_bit()
    for flag in (xf, yf, zf):
        if flag:
            _read_bitcoord(br)
    br.read_ubit(9)  # decal texture index
    if br.read_one_bit():  # entity + model index present
        br.read_ubit(11)
        br.read_ubit(13)
    br.read_one_bit()  # low priority


def _s_setview(br):
    br.read_ubit(11)


def _s_gameevent(br):
    _skip(br, br.read_ubit(11))


def _s_prefetch(br):
    br.read_ubit(14)


def _s_voiceinit(br):
    br.read_string()  # codec
    if br.read_byte() == 255:  # quality; 255 => sample rate follows
        br.read_ubit(16)


def _s_voicedata(br):
    """Sized here and skipped: the payload is not read. Length is in BITS,
    so the cursor advances by exactly that many."""
    br.read_byte()  # from_client (session slot)
    br.read_byte()  # proximity
    nbits = br.read_ubit(16)
    if nbits > br.bits_left():
        raise ChainError(f"voice {nbits}b past end", kind="voice past end")
    br.pos += nbits


def _s_updatestringtable(br):
    br.read_ubit(5)
    if br.read_one_bit():
        br.read_ubit(16)
    _skip(br, br.read_ubit(20))


def _s_packetentities(br):
    br.read_ubit(11)  # max entries
    if br.read_one_bit():  # is delta
        br.read_ubit(32)  # delta-from tick
    br.read_one_bit()  # baseline
    br.read_ubit(11)  # updated entries
    nbits = br.read_ubit(20)
    br.read_one_bit()  # update baseline
    _skip(br, nbits)


def _s_sounds(br):
    if br.read_one_bit():  # reliable
        n = br.read_ubit(8)
    else:
        br.read_ubit(8)
        n = br.read_ubit(16)
    _skip(br, n)


def _s_tempentities(br):
    br.read_ubit(8)
    _skip(br, br.read_varint32())


def _s_usermessage(br):
    br.read_ubit(8)
    _skip(br, br.read_ubit(11))


def _s_gameeventlist(br):
    br.read_ubit(9)
    _skip(br, br.read_ubit(20))


def _s_menu(br):
    br.read_ubit(16)
    _skip(br, br.read_ubit(16) * 8)


def _s_getcvarvalue(br):
    br.read_ulong()
    br.read_string()


def _s_fixangle(br):
    br.read_one_bit()
    br.read_ubit(16)
    br.read_ubit(16)
    br.read_ubit(16)


def _s_crosshairangle(br):
    br.read_ubit(16)
    br.read_ubit(16)
    br.read_ubit(16)


def _s_cmdkeyvalues(br):
    _skip(br, br.read_ulong() * 8)


def _s_entitymessage(br):
    br.read_ubit(11)
    br.read_ubit(9)
    _skip(br, br.read_ubit(11))


SIZERS = {
    wire.NET_NOP: _s_nop,
    wire.NET_DISCONNECT: _s_string,
    2: _s_file,
    wire.NET_TICK: _s_tick,
    4: _s_string,  # net_StringCmd
    wire.NET_SETCONVAR: _s_setconvar,
    wire.NET_SIGNONSTATE: _s_signonstate,
    7: _s_string,  # svc_Print
    9: _s_sendtable,
    10: _s_classinfo,
    11: _s_setpause,
    12: _s_createstringtable,
    13: _s_updatestringtable,
    14: _s_voiceinit,
    15: _s_voicedata,
    17: _s_sounds,
    21: _s_bspdecal,
    18: _s_setview,
    23: _s_usermessage,
    24: _s_entitymessage,
    25: _s_gameevent,
    26: _s_packetentities,
    27: _s_tempentities,
    28: _s_prefetch,
    29: _s_menu,
    30: _s_gameeventlist,
    31: _s_getcvarvalue,
    32: _s_cmdkeyvalues,
    19: _s_fixangle,
    20: _s_crosshairangle,
}

MAX_MESSAGES = 4096  # runaway backstop only; the walk
# already terminates on bits_left.


DECODED = {
    wire.NET_SIGNONSTATE: messages.parse_signonstate,
    wire.NET_DISCONNECT: messages.parse_disconnect,
    wire.NET_TICK: messages.parse_tick,
}


def walk(payload, replay_bit=True, max_messages=MAX_MESSAGES, start_bit=0):
    """Walk a message stream; the one walker of the live client and of every
    offline reader.

    Returns (msgs, stop). `msgs` is [(id, start, end, fields)] in stream
    order: start/end bracket the body after the 6-bit id; fields is the
    decoded dict for the lifecycle ids (net_Tick, net_Disconnect,
    net_SignonState, svc_ServerInfo), else None. `stop` is None for a clean
    end, else {"id", "bit", "reason", "kind"}; messages before it are sized
    correctly and kept.

    `start_bit` walks a stream that begins mid-buffer in place. Never walk a
    byte-rounded copy instead: the sender pads the last byte with 1 bits, and
    the copy's zero fill turns 1-5 of them into a phantom id 1/3/7/15/31.
    """
    br = wire.BitReader(payload)
    br.pos = start_bit
    msgs = []
    for _ in range(max_messages):
        if br.bits_left() < wire.NETMSG_TYPE_BITS:
            return msgs, None
        head = br.pos
        mid = br.read_ubit(wire.NETMSG_TYPE_BITS)
        start = br.pos
        try:
            if mid == wire.SVC_SERVERINFO:
                fields = messages.parse_serverinfo(br, replay_bit)
            else:
                dec = DECODED.get(mid)
                if dec is not None:
                    fields = dec(br)
                else:
                    sizer = SIZERS.get(mid)
                    if sizer is None:
                        return msgs, {
                            "id": mid,
                            "bit": head,
                            "reason": "no sizer",
                            "kind": "no sizer",
                        }
                    sizer(br)
                    fields = None
        except (EOFError, ChainError, ValueError) as e:
            return msgs, {
                "id": mid,
                "bit": head,
                "reason": str(e),
                "kind": getattr(e, "kind", None) or type(e).__name__,
            }
        msgs.append((mid, start, br.pos, fields))
    return msgs, {"id": None, "bit": br.pos, "reason": "message cap", "kind": "message cap"}


def walk_detect(payload, start_bit=0, replay_bit=True):
    """walk() with the trailing `m_bIsReplay` bit of svc_ServerInfo measured,
    not assumed (its presence is a build property).

    The current variant stands while it walks clean; when it stops early and
    the walk met a svc_ServerInfo, the other variant is tried and wins if it
    reaches further. Returns (msgs, stop, bit): bit is the variant used, or
    None when the stream carried no svc_ServerInfo to discriminate on. Without
    svc_ServerInfo the variants are byte-identical, so the steady-state packet
    is walked exactly once.
    """
    msgs, stop = walk(payload, replay_bit, start_bit=start_bit)
    if not any(m[0] == wire.SVC_SERVERINFO for m in msgs):
        return msgs, stop, None
    if stop is None:
        return msgs, stop, replay_bit

    def reach(st):
        return len(payload) * 8 if st is None else st["bit"]

    alt_msgs, alt_stop = walk(payload, not replay_bit, start_bit=start_bit)
    if reach(alt_stop) > reach(stop):
        return alt_msgs, alt_stop, not replay_bit
    return msgs, stop, replay_bit


def events(msgs):
    """[(id, fields)] of the decoded messages of a walk."""
    return [(m[0], m[3]) for m in msgs if m[3] is not None]
