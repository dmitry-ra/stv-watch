"""What a SourceTV viewer receives besides voice and entities: user messages
and game events, decoded from the same stream the framer walks.

Sources for the formats (read, not recalled):
- user message ids: registration order in RegisterUserMessages()
  (source-sdk-2013 src/game/shared/hl2/hl2_usermessages.cpp, then
  RegisterHapticMessages() and RegisterScriptMessages()). The id is the index
  in CUserMessages' dictionary, i.e. the order of Register() calls. No table of
  names travels on the wire.
- SayText / SayText2 / TextMsg / HudMsg / KeyHintText bodies: UTIL_* writers in
  src/game/server/util.cpp.
- game event list and events: svc_GameEventList carries descriptors
  (u9 id, name, then (u3 type, key name)* ended by type 0); svc_GameEvent
  carries u9 id then values in descriptor order.

Every decode is checked against the declared message length: a body that does
not end exactly at the length is reported as `misparse`, not trusted.
"""

import struct
from collections import Counter

SVC_USERMESSAGE = 23
SVC_GAMEEVENT = 25
SVC_GAMEEVENTLIST = 30

USER_MESSAGES = [
    "Geiger",
    "Train",
    "HudText",
    "SayText",
    "SayText2",
    "TextMsg",
    "HudMsg",
    "ResetHUD",
    "GameTitle",
    "ItemPickup",
    "ShowMenu",
    "Shake",
    "Fade",
    "VGUIMenu",
    "Rumble",
    "Battery",
    "Damage",
    "VoiceMask",
    "RequestState",
    "CloseCaption",
    "HintText",
    "KeyHintText",
    "SquadMemberDied",
    "AmmoDenied",
    "CreditsMsg",
    "LogoTimeMsg",
    "AchievementEvent",
    "UpdateJalopyRadar",
    "SPHapWeapEvent",
    "HapDmg",
    "HapPunch",
    "HapSetDrag",
    "HapSetConst",
    "HapMeleeContact",
    "SavedConvar",
]

EVENT_TYPES = {1: "string", 2: "float", 3: "long", 4: "short", 5: "byte", 6: "bool"}


class Misparse(Exception):
    pass


class Bits:
    """Bounded reader over [pos, end) of a bit buffer."""

    def __init__(self, payload, pos, end):
        self.v = int.from_bytes(payload, "little")
        self.pos = pos
        self.end = end

    def ubit(self, n):
        if self.pos + n > self.end:
            raise Misparse("past end")
        r = (self.v >> self.pos) & ((1 << n) - 1)
        self.pos += n
        return r

    def signed(self, n):
        r = self.ubit(n)
        return r - (1 << n) if r >> (n - 1) else r

    def float(self):
        return struct.unpack("<f", struct.pack("<I", self.ubit(32)))[0]

    def string(self):
        out = bytearray()
        while True:
            c = self.ubit(8)
            if c == 0:
                return out.decode("utf-8", "replace")
            out.append(c)

    def left(self):
        return self.end - self.pos


def _strings_to_end(b, limit):
    out = []
    while b.left() >= 8 and len(out) < limit:
        out.append(b.string())
    return out


def _need(params, n, what):
    if len(params) < n:
        raise Misparse(f"{what} with {len(params)} of {n} parameters")
    return params


def decode_usermessage(name, b):
    """-> dict of fields for the message types that carry text, else None.
    The stock formats are checked for the parameters they name: a shorter
    message that still ends at its length is a misparse, not a chat line."""
    if name == "SayText":
        return {"ent": b.ubit(8), "text": b.string(), "chat": b.ubit(8)}
    if name == "SayText2":
        ent, chat, fmt = b.ubit(8), b.ubit(8), b.string()
        params = _strings_to_end(b, 4)
        if fmt.startswith("HL2MP_Chat"):
            _need(params, 2, fmt)
        return {"ent": ent, "chat": chat, "fmt": fmt, "params": params}
    if name == "TextMsg":
        dest, msg = b.ubit(8), b.string()
        params = _strings_to_end(b, 4)
        if msg == "#Game_connected":
            _need(params, 1, msg)
        return {"dest": dest, "msg": msg, "params": params}
    if name == "HudMsg":
        f = {"channel": b.ubit(8)}
        b.pos += 2 * 32 + 9 * 8 + 4 * 32  # x y, two colours, effect, times
        f["text"] = b.string()
        return f
    if name in ("HintText", "HudText"):
        return {"text": b.string()}
    if name == "KeyHintText":
        return {"text": [b.string() for _ in range(b.ubit(8))]}
    if name == "VGUIMenu":
        menu, show, n = b.string(), b.ubit(8), b.ubit(8)
        return {"menu": menu, "show": show, "kv": [(b.string(), b.string()) for _ in range(n)]}
    return None


def parse_event_list(b):
    events = {}
    count = b.ubit(9)
    b.ubit(20)
    for _ in range(count):
        eid, name, keys = b.ubit(9), b.string(), []
        while True:
            t = b.ubit(3)
            if t == 0:
                break
            if t not in EVENT_TYPES:
                raise Misparse(f"event type {t}")
            keys.append((b.string(), t))
        events[eid] = (name, keys)
    return events


def parse_event(b, descriptors):
    eid = b.ubit(9)
    if eid not in descriptors:
        return eid, None, None
    name, keys = descriptors[eid]
    vals = {}
    for key, t in keys:
        if t == 1:
            vals[key] = b.string()
        elif t == 2:
            vals[key] = b.float()
        elif t == 3:
            vals[key] = b.signed(32)
        elif t == 4:
            vals[key] = b.signed(16)
        elif t == 5:
            vals[key] = b.ubit(8)
        else:
            vals[key] = b.ubit(1)
    return eid, name, vals


def chat_kind(fmt):
    """SayText2 format class without its text: the stock localisation key, or
    a line a chat plugin formatted itself (colour codes \x01-\x08 inline)."""
    if fmt.startswith("HL2MP_Chat"):
        return fmt
    return "plugin:chat" if ": \x01" in fmt else "plugin:other"


class Collector:
    """Framer on_msg hook: counts every message id, decodes 23/25/30."""

    def __init__(self, sink=None):
        self.framer = None
        self.sink = sink  # callable(record dict) or None
        self.msg_ids = Counter()
        self.usermsg = Counter()  # name -> count
        self.usermsg_misparse = Counter()
        self.events = Counter()  # name -> count
        self.events_misparse = Counter()
        self.events_unknown_id = 0
        self.event_lists = 0
        self.descriptors = {}
        self.chat = Counter()  # SayText2 fmt / SayText chat flag

    def __call__(self, mid, payload, start, end):
        self.msg_ids[mid] += 1
        if mid == SVC_USERMESSAGE:
            self._usermessage(payload, start, end)
        elif mid == SVC_GAMEEVENT:
            self._event(payload, start, end)
        elif mid == SVC_GAMEEVENTLIST:
            b = Bits(payload, start, end)
            try:
                self.descriptors = parse_event_list(b)
                self.event_lists += 1
                if b.left():
                    raise Misparse("trailing bits")
            except Misparse:
                self.events_misparse["<list>"] += 1

    def _emit(self, kind, name, fields):
        if self.sink is None:
            return
        fr = self.framer
        self.sink(
            {
                "kind": kind,
                "name": name,
                "t_ns": fr.cur_t_ns,
                "session": fr.cur_session,
                "tick": fr.tick,
                "f": fields,
            }
        )

    def _usermessage(self, payload, start, end):
        b = Bits(payload, start, end)
        mtype, length = b.ubit(8), b.ubit(11)
        name = USER_MESSAGES[mtype] if mtype < len(USER_MESSAGES) else f"#{mtype}"
        self.usermsg[name] += 1
        body = Bits(payload, b.pos, b.pos + length)
        try:
            fields = decode_usermessage(name, body)
            if fields is not None and body.left() != 0:
                raise Misparse(f"{body.left()} bits left")
        except Misparse:
            self.usermsg_misparse[name] += 1
            return
        if fields is None:
            return
        if name == "SayText2":
            self.chat["SayText2:" + chat_kind(fields["fmt"])] += 1
        elif name == "SayText":
            self.chat[f"SayText:chat={fields['chat']}"] += 1
        self._emit("usermsg", name, fields)

    def _event(self, payload, start, end):
        b = Bits(payload, start, end)
        length = b.ubit(11)
        body = Bits(payload, b.pos, b.pos + length)
        name = None
        try:
            eid, name, vals = parse_event(body, self.descriptors)
            if name is None:
                self.events_unknown_id += 1
                return
            if body.left() != 0:
                raise Misparse(f"{body.left()} bits left")
        except Misparse:
            self.events_misparse[str(name)] += 1
            return
        self.events[name] += 1
        self._emit("event", name, vals)
