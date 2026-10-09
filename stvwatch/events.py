"""Game events of the stream as feed records: chat, console, connects, joins,
leaves, kills, team and nick changes, server and SourceMod texts.

Decoding is stream.streamevents (the Collector hangs on Framer.on_msg) and
stream.userinfo (the string table hook); this module only turns their
output into one self-contained record per event. Facts the rules rest on,
observed on live relays:
- a connect is a new entry of the `userinfo` table (player_connect never
  reaches a SourceTV viewer); a nick change is a changed entry;
- `TextMsg #Game_connected` is sent on every ClientActive, so after a map
  change it repeats for everyone who stayed: such a repeat is recognised by the
  player having been on the previous map, not by text or time;
- resends of the reliable stream are dropped by the receiver before any hook
  sees them, so no text window dedup is done here (it would eat real repeats).
"""

import re
from collections import Counter, deque

from .stream import streamevents as se
from .stream.userinfo import STEAMID64_BASE, scan_players

TYPES = (
    "chat",
    "console",
    "connect",
    "join",
    "leave",
    "death",
    "team",
    "name",
    "server",
    "sourcemod",
)
DEFAULT = ("chat", "console", "connect", "join", "leave", "name")
TEAMS = {0: "unassigned", 1: "spectator", 2: "combine", 3: "rebels"}
STRING_TABLE_CREATE, STRING_TABLE_UPDATE = 12, 13
# Source chat colours: \x01-\x06 single codes, \x07RRGGBB, \x08RRGGBBAA
COLOR = re.compile(r"\x07[0-9A-Fa-f]{6}|\x08[0-9A-Fa-f]{8}|[\x01-\x06]")
NETWORKID = re.compile(r"^(?:\[U:1:(\d+)\]|STEAM_[0-5]:([01]):(\d+))$")
KILL_WINDOW_NS = 60 * 10**9


GROUPS = {"all": TYPES, "default": DEFAULT, "none": ()}


def _expand(name):
    if name in GROUPS:
        return GROUPS[name]
    if name not in TYPES:
        raise ValueError(
            f"unknown event type {name!r} (types: {', '.join(TYPES)}, " "all, default, none)"
        )
    return (name,)


def parse_types(spec):
    """--events value -> tuple of types; ValueError on an unknown name.
    Items are types or groups; a leading '-' removes. All plus items are taken
    first, then every minus item is taken out, wherever it stood; with minus
    items only, the base is all."""
    spec = (spec or "").strip()
    if not spec:
        return DEFAULT
    items = [t.strip() for t in spec.split(",")]
    plus = [t for t in items if not t.startswith("-")]
    minus = [t[1:].strip() for t in items if t.startswith("-")]
    out = []
    for t in plus or ["all"]:
        out += [x for x in _expand(t) if x not in out]
    drop = {x for t in minus for x in _expand(t)}
    return tuple(t for t in out if t not in drop)


def uncolor(text):
    return COLOR.sub("", text)


def account_of(networkid):
    m = NETWORKID.match(networkid or "")
    if not m:
        return 0
    if m.group(1):
        return int(m.group(1))
    return int(m.group(3)) * 2 + int(m.group(2))


class GameEvents:
    """Fed by the framer hooks; records of the enabled types collect in `out`
    for the caller to drain. While `muted` (replay fast-forward) state is kept
    but nothing is emitted or counted."""

    def __init__(self, types=DEFAULT):
        self.types = set(types)
        self.framer = None
        self.collector = se.Collector(self.record)
        self.out = []
        self.muted = False
        self.session = None
        self.cur = {}  # account -> nick, players of this session's map
        self.prev = set()  # accounts in game on the previous map, not yet back
        self.pending = set()  # connected in our sight, not yet in game
        self.unresolved = []  # (t_ns, nick, record or None): joins of an unknown nick
        self.userids = {}  # server userid -> (account, nick)
        self.team = {}  # account -> team, from player_team on this map
        self.alltalk = False  # sv_alltalk seen as 1 (server_cvar)
        self.counts = Counter()  # type -> events seen (shown or not), + join_repeat
        self.kills = Counter()  # attacker label -> kills of others
        self.deaths = 0
        self.death_times = deque()

    def attach(self, framer):
        self.framer = framer
        self.collector.framer = framer

    def on_msg(self, mid, payload, start, end):
        self.collector(mid, payload, start, end)

    # ------------------------------------------------------------ state
    def _session(self, session):
        if session == self.session:
            return
        if self.session is not None and self.cur:
            # a new session is a map change or a reconnect: who was in game
            # stays in game, and will be greeted with #Game_connected again;
            # who was still connecting enters for the first time
            self.prev = set(self.cur) - self.pending
        self.cur = {}
        self.team = {}  # a new map puts everyone back as unassigned
        self.session = session

    def _nick_account(self, nick):
        want = nick.replace("%", " ")
        for acc, n in self.cur.items():
            if n == nick or n.replace("%", " ") == want:
                return acc
        return 0

    def _late_join(self, fid, nick, t_ns):
        """A reconnect can greet before its table entry, in the same packet:
        give that join its account. -> the join record, or None if no join."""
        self.unresolved = [u for u in self.unresolved if u[0] == t_ns]
        want = nick.replace("%", " ")
        for u in self.unresolved:
            if u[1].replace("%", " ") == want:
                self.unresolved.remove(u)
                rec = u[2]
                if rec is not None:
                    sid = fid + STEAMID64_BASE
                    rec["steamid64"] = sid
                return rec if rec is not None else {}
        return None

    def _player(self, userid):
        return self.userids.get(userid, (0, ""))

    def emit(self, typ, t_ns, account=0, nick="", text="", **extra):
        if self.muted:
            return
        self.counts[typ] += 1
        if typ not in self.types:
            return
        sid = account + STEAMID64_BASE if account else 0
        rec = {
            "type": typ,
            "t_ns": t_ns,
            "steamid64": sid,
            "nick": nick,
            "text": text,
            "extra": extra,
        }
        self.out.append(rec)
        return rec

    # ------------------------------------------------------------ userinfo table
    def on_table(self, payload, start, end, mid):
        fr = self.framer
        self._session(fr.cur_session)
        players = scan_players(payload, start, end)
        if mid == STRING_TABLE_CREATE and any(fid for fid, _n, _u in players):
            # not in the table we join with: left during the map change while
            # we were reconnecting, so his next greeting is a first entry
            self.prev &= {fid for fid, _n, _u in players}
        for fid, nick, userid in players:
            self.userids[userid] = (fid, nick)
            if not fid:
                continue  # bots: no connect or rename lines
            old = self.cur.get(fid)
            self.cur[fid] = nick
            if mid != STRING_TABLE_UPDATE:
                continue  # the table as it was when we joined
            if old is None and fid not in self.prev:
                joined = self._late_join(fid, nick, fr.cur_t_ns)
                if joined is None:
                    self.pending.add(fid)
                rec = self.emit("connect", fr.cur_t_ns, fid, nick, "connecting", userid=userid)
                at = [i for i, r in enumerate(self.out) if r is joined]
                if rec is not None and at:
                    self.out.pop()  # the connect goes before its join
                    self.out.insert(at[0], rec)
            elif old is not None and old != nick:
                self.emit(
                    "name", fr.cur_t_ns, fid, nick, f"{old} -> {nick}", old=old, userid=userid
                )

    # ------------------------------------------------------------ messages and events
    def record(self, r):
        self._session(r["session"])
        f, t = r["f"], r["t_ns"]
        if r["kind"] == "usermsg":
            getattr(self, "_" + r["name"], lambda _t, _f: None)(t, f)
        else:
            getattr(self, "_ev_" + r["name"], lambda _t, _f: None)(t, f)

    def _SayText2(self, t, f):
        fmt, params = f["fmt"], f["params"]
        if fmt.startswith("HL2MP_Chat"):
            nick, text = uncolor(params[0]), uncolor(params[1])
            where = fmt[len("HL2MP_Chat_") :]
            self.emit(
                "chat",
                t,
                self._nick_account(nick),
                nick,
                text,
                channel=where.lower() or "all",
                ent=f["ent"],
            )
        elif ": \x01" in fmt:
            # a chat plugin's own line: nick and text glued, split at the
            # first ": \x01" (observed form, not a spec)
            nick, text = fmt.split(": \x01", 1)
            nick, text = uncolor(nick), uncolor(text)
            self.emit(
                "chat", t, self._nick_account(nick), nick, text, channel="plugin", ent=f["ent"]
            )
        else:
            self.emit("sourcemod", t, text=uncolor(fmt), via="SayText2")

    def _SayText(self, t, f):
        text = uncolor(f["text"]).rstrip("\n")
        if f["ent"] == 0:
            if text.startswith("Console: "):
                text = text[len("Console: ") :]
            self.emit("console", t, 0, "Console", text)
        else:
            self.emit("chat", t, 0, "", text, channel="saytext", ent=f["ent"])

    def _TextMsg(self, t, f):
        msg, params = f["msg"], [p for p in f["params"]]
        if msg == "#Game_connected":
            self._join(t, params[0])
            return
        for i, p in enumerate(params, 1):
            msg = msg.replace(f"%s{i}", p)
        text = uncolor(msg).strip()
        if text.startswith("[SM"):
            self.emit("sourcemod", t, text=text, via="TextMsg")
        else:
            self.emit("server", t, text=text, via="TextMsg", dest=f["dest"])

    def _HudMsg(self, t, f):
        self.emit("server", t, text=uncolor(f["text"]).strip(), via="HudMsg")

    def _join(self, t, nick):
        acc = self._nick_account(nick)
        self.pending.discard(acc)
        if acc and acc in self.prev:
            self.prev.discard(acc)
            self.counts["join_repeat"] += 0 if self.muted else 1
            return
        rec = self.emit("join", t, acc, nick, "entered the game")
        if not acc:
            self.unresolved.append((t, nick, rec))

    def _ev_player_disconnect(self, t, f):
        acc = account_of(f.get("networkid", ""))
        self.cur.pop(acc, None)
        self.prev.discard(acc)
        self.pending.discard(acc)
        self.emit(
            "leave",
            t,
            acc,
            f.get("name", ""),
            f.get("reason", "").strip(),
            userid=f.get("userid"),
            networkid=f.get("networkid", ""),
            bot=bool(f.get("bot")),
        )

    def _ev_player_death(self, t, f):
        victim, attacker = f.get("userid", 0), f.get("attacker", 0)
        vacc, vnick = self._player(victim)
        aacc, anick = self._player(attacker)
        vnick = vnick or f"#{victim}"
        weapon = f.get("weapon", "")
        if not self.muted:
            self.deaths += 1
            self.death_times.append(t)
            if attacker and attacker != victim:
                self.kills[anick or f"#{attacker}"] += 1
        if not attacker or attacker == victim:
            how = "killed self" if attacker == victim else "died"
            self.emit(
                "death",
                t,
                vacc,
                vnick,
                f"{how} ({weapon})",
                weapon=weapon,
                userid=victim,
                attacker=attacker,
            )
            return
        self.emit(
            "death",
            t,
            aacc,
            anick or f"#{attacker}",
            f"killed {vnick} ({weapon})",
            weapon=weapon,
            userid=victim,
            attacker=attacker,
            victim=vnick,
            victim_steamid64=vacc + STEAMID64_BASE if vacc else 0,
        )

    def unheard(self, sid64):
        """Voice the players may not hear: hl2mp CanPlayerHearPlayer passes
        voice only between equal teams (hl2mp_gamerules.cpp), sv_alltalk 1
        to all; a spectator talks to spectators. Unknown team: not marked."""
        return not self.alltalk and self.team.get(sid64 - STEAMID64_BASE) == 1

    def _ev_player_team(self, t, f):
        uid = f.get("userid", 0)
        acc, nick = self._player(uid)
        if acc:
            if f.get("disconnect"):
                self.team.pop(acc, None)
            else:
                self.team[acc] = f.get("team", 0)
        nick = f.get("name") or nick or f"#{uid}"
        old, new = f.get("oldteam", 0), f.get("team", 0)
        self.emit(
            "team",
            t,
            acc,
            nick,
            f"team {TEAMS.get(old, old)} -> {TEAMS.get(new, new)}"
            + (" (disconnect)" if f.get("disconnect") else ""),
            team=new,
            oldteam=old,
            userid=uid,
        )

    def _ev_server_cvar(self, t, f):
        if f.get("cvarname") == "sv_alltalk":
            self.alltalk = str(f.get("cvarvalue", "")).strip() not in ("", "0")
        self.emit(
            "server",
            t,
            text=f"cvar {f.get('cvarname', '')} = {f.get('cvarvalue', '')}",
            via="server_cvar",
            cvar=f.get("cvarname", ""),
            value=f.get("cvarvalue", ""),
        )

    # ------------------------------------------------------------ block
    def kill_summary(self, now_ns):
        while self.death_times and now_ns - self.death_times[0] > KILL_WINDOW_NS:
            self.death_times.popleft()
        out = f"kills {self.deaths} {len(self.death_times)}/min"
        if self.kills:
            who, n = self.kills.most_common(1)[0]
            out += f" top {who} {n}"
        return out
