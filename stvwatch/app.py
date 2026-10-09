"""Main loop: source -> framing -> game events, with a feed of events above
a pinned status block.

One thread owns all state; the reader thread hands it input items through a
queue. Voice messages are sized by the walk and skipped.
"""

import json
import os
import queue
import signal
import threading
import time
from datetime import datetime, timezone

from . import events as gamevents
from . import source
from .model import NS, Conn, Traffic
from .net import a2s, wire
from .net import dump as dumpfmt
from .render import Screen, clean
from .stream.framing import Framer
from .stream.userinfo import NickBook

SIGNON = {3: "NEW", 4: "PRESPAWN", 5: "SPAWN", 6: "FULL", 7: "CHANGELEVEL"}


# Every feed line type (docs/events.schema.json), or state -> style for a type
# whose lines differ. Chat leads in bold yellow; console magenta; joins and
# leaves in their own muted green and red (not the green and red of our
# connection lines); connects, team and nick changes and server text players
# read in chat dimmed; kills and other server notices dimmest. Game lines are
# painted whole, except chat, which keeps its cyan nick; our own lines keep
# the blue tag.
LINE_STYLE = {
    "chat": "boldyellow",
    "console": "magenta",
    "connect": "dim",
    "join": "joingreen",
    "leave": "leavered",
    "death": "gray",
    "team": "dim",
    "name": "dim",
    "server": {"talk": "dim", "other": "gray"},
    "sourcemod": "gray",
    "conn": {"info": "", "ok": "green", "warn": "yellow", "fail": "red"},
    "net": {"ok": "green", "warn": "yellow", "fail": "red"},
    "play": "",
    "tvd": {"info": "dim", "fail": "red"},
    "done": "",
}
HUD_PRINTTALK = 3


def line_style(typ, state=None):
    s = LINE_STYLE[typ]
    if isinstance(s, dict):
        return s[state]
    if state is not None:
        raise KeyError(state)
    return s


def game_state(r):
    if r["type"] == "server":
        return "talk" if r["extra"].get("dest") == HUD_PRINTTALK else "other"
    return None


def local(t_ns, tz, fmt=None):
    """Time in `tz`; on screen always with milliseconds (HH:MM:SS.mmm)."""
    if fmt is not None:
        return datetime.fromtimestamp(t_ns / NS, tz).strftime(fmt)
    return datetime.fromtimestamp(t_ns // NS, tz).strftime("%H:%M:%S") + ".%03d" % (
        t_ns % NS // 1_000_000
    )


def tz_label(tz):
    return getattr(tz, "key", None) or "UTC"


def utc_iso(t_ns):
    return (
        datetime.fromtimestamp(t_ns / NS, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )


def hms(seconds):
    s = int(max(0, seconds))
    return "%02d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def steam2(sid64):
    a = sid64 - 76561197960265728
    return f"STEAM_0:{a & 1}:{a >> 1}"


class App:
    def __init__(self, a):
        self.a = a
        self.live = a.relay is not None
        self.follow = a.follow is not None
        self.tz = a.tz
        self.quit = False
        self.quit_why = ""
        self.conn = Conn()
        self.traffic = Traffic(quiet_s=a.quiet)
        self.nicks = NickBook()
        self.game = gamevents.GameEvents(a.event_types)
        self.framer = Framer(
            keep_fates=False,
            on_table=self._on_table,
            on_info=self._on_info,
            on_packet=self._on_packet if a.debug else None,
            on_msg=self.game.on_msg,
        )
        self.game.attach(self.framer)
        self.screen = Screen(plain=a.plain, color=False if a.no_color else None)
        self.rec = None
        self.pacer = None
        self.held = None
        self.client = None
        self.client_rc = 0
        self.logtail = None
        self.cpu = (time.monotonic(), self._cpu_s(), 0.0)
        self.cpu_cores = 0.0
        self.net_cpu = 0.0
        self.last_render = 0.0
        self.last_status_line = 0.0
        self.a2s_before = None
        self.with_us = None  # relay spectators last seen while we were in
        self.started_ns = time.time_ns()
        self.first_t = 0
        self.play_t0 = 0  # first datagram after the skip
        self.now = 0
        self.dir = self._session_dir()
        self.feed_fh = open(os.path.join(self.dir, "feed.log"), "a", encoding="utf-8")
        # what --json prints, in any screen mode; errors as on the --json stdout
        self.jsonl_fh = open(
            os.path.join(self.dir, "events.jsonl"), "a", encoding="utf-8", errors="replace"
        )

    # ---------------------------------------------------------------- setup
    def _session_dir(self):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        what = (
            self.a.relay.replace(":", "_")
            if self.live
            else ("follow-" if self.follow else "replay-")
            + os.path.splitext(os.path.basename(self.a.replay))[0]
        )
        d = os.path.join(self.a.out, f"{stamp}_{what}_{os.getpid()}")
        os.makedirs(d, exist_ok=True)
        return d

    def _on_table(self, payload, start, end):
        self.nicks(payload, start, end)
        br = wire.BitReader(payload)
        br.pos = start - wire.NETMSG_TYPE_BITS
        self.game.on_table(payload, start, end, br.read_ubit(wire.NETMSG_TYPE_BITS))

    def _on_info(self, info):
        if info.get("map") and info["map"] != self.conn.map:
            self.conn.map = info["map"]
        self.conn.hostname = info.get("hostname") or self.conn.hostname
        self.conn.max_clients = info.get("max_clients") or self.conn.max_clients

    def _on_packet(self, pkt):
        """--debug: each sequence gap counted as lost, with the numbers around
        it (a gap shorter than the numbers suggest was partly declared choked)."""
        if pkt.lost:
            seq = pkt.header.sequence
            text = f"lost: seq {pkt.prev_seq} -> {seq} ({pkt.lost})"
            self.event(
                "net",
                text,
                "warn",
                pkt.t_ns,
                {
                    "type": "net",
                    "text": text,
                    "extra": {"seq_from": pkt.prev_seq, "seq_to": seq, "lost": pkt.lost},
                },
            )

    @staticmethod
    def _cpu_s():
        t = os.times()
        return t.user + t.system

    # ---------------------------------------------------------------- feed
    def feed(self, spans, t_ns=None, rec=None, full=None):
        """One feed line; with --json the record instead (type and text if
        no record is given), events.jsonl always. feed.log gets `full` if given."""
        t_ns = t_ns or self.now or time.time_ns()
        line = self.json_line(
            (
                rec
                if rec is not None
                else {
                    "type": spans[0][0].strip() if len(spans) > 1 else "info",
                    "text": "".join(t for t, _s in spans[1:] if len(spans) > 1)
                    or "".join(t for t, _s in spans),
                }
            ),
            t_ns,
        )
        self.jsonl(line)
        if self.a.json:
            self.screen.feed([(line, "")])
        else:
            self.screen.feed([(local(t_ns, self.tz) + " ", "dim")] + spans)
        self.log(full or spans, t_ns, rec.get("steamid64", 0) if rec else 0)

    def json_line(self, rec, t_ns):
        out = {"t_utc": utc_iso(t_ns)}
        if self.a.tz_given:
            out["t_local"] = local(t_ns, self.tz, "%Y-%m-%d %H:%M:%S")
        out.update(
            {
                "type": rec.get("type", ""),
                "steamid64": rec.get("steamid64", 0),
                "nick": rec.get("nick", ""),
                "text": rec.get("text", ""),
            }
        )
        out.update(rec.get("extra") or {})
        return json.dumps(out, ensure_ascii=False)

    def jsonl(self, line):
        self.jsonl_fh.write(line + "\n")
        self.jsonl_fh.flush()

    def log(self, spans, t_ns, sid=0):
        """feed.log: UTC time, the player's SteamID64 (empty if none), text."""
        text = "".join(t for t, _s in spans)
        self.feed_fh.write(f"{utc_iso(t_ns)}\t{sid or ''}\t{text}\n")
        self.feed_fh.flush()

    def event(self, tag, text, state=None, t_ns=None, rec=None):
        if (
            self.follow
            and self.pacer is not None
            and self.pacer.skipping(t_ns or self.now or time.time_ns())
        ):
            return  # the journal's past: state only
        self.feed([(f"{tag:<5} ", "blue"), (text, line_style(tag, state))], t_ns, rec)

    # ---------------------------------------------------------------- input items
    def handle_event(self, t_ns, rtype, data):
        self.framer.observe(t_ns, rtype, data)
        if rtype == dumpfmt.DATAGRAM_OUT:
            self.traffic.outbound(t_ns)
            return
        if rtype == dumpfmt.SPLIT_SEEN:
            return
        try:
            f = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            f = {}
        self.now = max(self.now, t_ns)
        c = self.conn
        if rtype == dumpfmt.SESSION_START:
            c.sessions = f.get("session", c.sessions + 1)
            c.state, c.state_ns = "signon", t_ns
            self.event("conn", f"session #{c.sessions} open ({f.get('endpoint', '')})", "info")
        elif rtype == dumpfmt.RECONNECT:
            if not f.get("ok", True):
                c.attempts_failed += 1
                c.last_error = str(f.get("error", ""))
                c.state, c.state_ns = "retry", t_ns
                self.event(
                    "conn", f"attempt {f.get('attempt')} failed: {clean(c.last_error)}", "warn"
                )
            else:
                c.state, c.state_ns = "connecting", t_ns
        elif rtype == dumpfmt.SIGNON:
            name = SIGNON.get(f.get("state"), str(f.get("state")))
            if name == "FULL":
                took = (t_ns - c.state_ns) / NS if c.state_ns else 0.0
                c.state, c.state_ns, c.full_ns = "FULL", t_ns, t_ns
                self.event("conn", f"FULL on {clean(c.map) or '?'} ({took:.1f} s after open)", "ok")
            else:
                c.state = "signon:" + name
        elif rtype == dumpfmt.BROKEN:
            c.state, c.state_ns = "broken", t_ns
            self.event("conn", f"break: {f.get('cause')}: {clean(f.get('detail', ''))}", "warn")
        elif rtype == dumpfmt.MAPCHANGE:
            self.event("conn", f"map change -> {clean(f.get('map', ''))}", "info")
        elif rtype == dumpfmt.LEAVE:
            c.state, c.state_ns = "left", t_ns
            self.event("conn", f"left: net_Disconnect x{f.get('sent')} ({f.get('why')})", "info")

    def handle_dg(self, dg):
        t = dg.t_ns
        self.now = max(self.now, t)
        if not self.first_t:
            self.first_t = t
        self.check_traffic(t)
        self.traffic.inbound(t, len(dg.data), wire.classify(dg.data) == "split")
        self.check_traffic(t)
        self.game.muted = self.pacer.skipping(t)
        self.framer.feed(dg)
        self.game_lines()
        if not self.pacer.skipping(t):
            self.play_t0 = self.play_t0 or t

    def game_lines(self):
        """Feed lines of the game events the framer just decoded."""
        recs, self.game.out = self.game.out, []
        for r in recs:
            full = self.game_spans(r, full=True)
            self.feed(full if self.a.debug else self.game_spans(r), r["t_ns"], r, full)

    @staticmethod
    def who(r):
        nick = clean(r["nick"])
        sid = r["steamid64"]
        return [nick] if nick else [steam2(sid)] if sid else []

    def game_spans(self, r, full=False):
        """Time is added by feed(); here: type tag, who, what. Each line stands
        alone: who by stream nick, SteamID where shown; `full` (feed.log,
        --debug) adds both SteamIDs to a kill."""
        typ, x = r["type"], r["extra"]
        text = clean(r["text"])
        tag = f"{typ:<5} "
        style = line_style(typ, game_state(r))
        if typ == "chat":
            ch = x.get("channel", "all")
            return (
                [(tag, "blue")]
                + [(w, "cyan") for w in self.who(r)]
                + ([(f" [{ch}]", "dim")] if ch not in ("all", "") else [])
                + [(": ", ""), (text, style)]
            )
        who = self.who(r)
        sid = r["steamid64"]
        out = [tag]
        if typ == "console":
            out += ["Console: ", text]
        elif typ in ("connect", "join", "leave", "team", "name"):
            if typ == "name":
                out += [clean(x.get("old", "")), " -> ", clean(r["nick"])]
            else:
                out += who + [" " + text if typ != "leave" else " left: " + text]
            if sid:
                out.append(f"  {steam2(sid)}")
            elif typ == "leave" and x.get("networkid"):
                out.append(f"  {clean(x['networkid'])}")
        elif typ == "death":
            out += who + [" " + text]
            victim = x.get("victim_steamid64", 0)
            if full and (sid or victim):
                ids = steam2(sid) if sid else "-"
                if "victim" in x:
                    ids += " > " + (steam2(victim) if victim else "-")
                out.append("  " + ids)
        else:
            out.append(text)
        return [(t, style) for t in out]

    # ---------------------------------------------------------------- status block
    BLOCK_LINES = 4

    def block(self):
        """Fixed height: rule, connection, traffic, hint. By default what a
        watcher acts on; transport internals with --debug."""
        now = self.now or time.time_ns()
        dbg = self.a.debug
        c, tr = self.conn, self.traffic
        L = [[(" stv-watch ", "bar")]]
        if self.live:
            head = [("LIVE ", "bold"), (self.a.relay, "")]
        elif self.follow:
            head = [("FOLLOW ", "bold"), (os.path.basename(self.a.replay), "")]
        else:
            pos = self.pacer.position_s()
            speed = "max" if self.a.speed <= 0 else f"x{self.a.speed:g}"
            head = [
                ("REPLAY ", "bold"),
                (os.path.basename(self.a.replay), ""),
                (f" {speed} +{hms(pos)}", "dim"),
            ]
        state = c.state
        st_style = {"FULL": "green", "broken": "red", "retry": "yellow", "left": "yellow"}.get(
            state, ""
        )
        if state == "FULL" and c.full_ns:
            state = f"FULL {hms((now - c.full_ns) / NS)}"
        head += [("  ", ""), (state, st_style)]
        if c.hostname:
            head += [("  ", ""), (clean(c.hostname), "")]
        if c.map:
            head += [("  ", ""), (clean(c.map), "magenta")]
        # right after the map: the end of a long head is cut at the terminal width
        head += [("  " + clean(self.game.kill_summary(now)), "dim")]
        if dbg:
            if c.players >= 0:
                head += [(f"  relay {c.players}/{c.max_players}", "dim")]
            head += [(f"  seen {len(self.nicks.by_account)} players", "dim")]
            if c.sessions > 1:
                head += [(f"  sess {c.sessions}", "dim")]
        head += [("  " + local(now, self.tz) + " " + tz_label(self.tz), "dim")]
        L.append(head)

        if tr.flowing:
            flow = [("FLOWING ", "green"), (hms((now - tr.since_ns) / NS), "")]
        elif tr.last_ns:
            flow = [("STOPPED ", "red"), (f"{(now - tr.last_ns) / NS:.1f}s", "red")]
        else:
            flow = [("no traffic yet", "dim")]
        fc = self.framer.counters
        lost = fc.get("seq_lost", 0)
        net = [("net  ", "blue")] + flow
        if dbg:
            net += [
                (f"  in {tr.pkts.rate(now):.0f}/s {tr.bytes.rate(now) / 1000:.1f}KB/s", ""),
                (f" out {tr.out_pkts.rate(now):.0f}/s" if self.live else "", ""),
                (f"  -2 {100 * tr.split_share(now):.1f}% ({tr.total_splits})", ""),
                (f"  choked {fc.get('seq_choked', 0)} lost {lost}", ""),
                (f"  total {tr.total_pkts} {tr.total_bytes / 1e6:.1f}MB", "dim"),
                (f"  stop>{tr.quiet / NS:.1f}s", "dim"),
                (f"  cpu {self.cpu_cores:.2f}", ""),
                (f" net {self.net_cpu:.2f}" if self.live else "", "dim"),
            ]
        else:
            net.append((f"  lost {lost}", "yellow" if lost else ""))
        L.append(net)

        L.append([("q quit  ", "dim"), (self.dir, "dim")])
        return L

    # ---------------------------------------------------------------- live helpers
    def a2s_poll(self):
        ip, port = self.a.relay.rsplit(":", 1)
        while not self.quit:
            try:
                i = a2s.info(ip, int(port))
                self.conn.players, self.conn.max_players = i["players"], i["max_players"]
                self.note_relay_count(i["players"], time.time_ns())
                self.conn.version = str(i.get("version", ""))
            except Exception:  # noqa: BLE001
                pass
            for _ in range(150):
                if self.quit:
                    return
                time.sleep(0.1)

    def note_relay_count(self, players, now_ns):
        """The relay's spectator count while we are in, for the slot check.
        A2S counts a new spectator with a delay: only well after FULL. And
        only while traffic flows: a relay that died and came back answers A2S
        without us before our session notices it is gone."""
        c = self.conn
        if c.state == "FULL" and now_ns - c.full_ns > 5 * NS and self.traffic.flowing:
            self.with_us = players

    def precheck(self):
        """Refuse a relay we must not take (client.precheck: silent, unknown
        build, password, Steam login, no free slot left for anyone else after
        us). -> error text or ''."""
        ok, why, facts = source.client.precheck(
            self.a.relay, allow_last_slot=self.a.allow_last_slot
        )
        self.conn.players, self.conn.max_players = (
            facts["spectators_before"],
            facts["max_spectators"],
        )
        self.conn.hostname = facts.get("hostname") or ""
        self.conn.map = facts.get("map") or ""
        if ok:
            self.a2s_before = facts  # the slot check after leaving
            return ""
        if why.startswith("unknown_build "):
            build = why.split(" ", 1)[1]
            return (
                f"relay runs server build {build}, whose SendTable CRC this version does "
                "not know; guessing it would cost the relay a connection per guess. "
                "Extract it with tools/read_crc.py and add it to stvwatch/net/builds.py "
                '(docs/protocol.md, "Server builds")'
            )
        return {
            "relay_silent": "relay does not answer A2S_INFO",
            "password": "relay has tv_password (A2S visibility=1)",
            "few_free_slots": (
                f"relay slots {facts['spectators_before']}/"
                f"{facts['max_spectators']}: no free slot would remain "
                "after us (use --allow-last-slot to take the last one)"
            ),
        }.get(why, why.replace("auth_", "relay does not take anonymous login, " "auth protocol "))

    def slot_check(self):
        """The slot rule (client.slot_freed): fewer spectators after we
        left than last seen while we were in; the count before we came only
        when no look while connected was had. -> (ok, after, feed text)."""
        if self.with_us is not None:
            ok, after = source.client.slot_freed(self.a.relay, self.with_us)
            what = f"relay viewers while in {self.with_us}, after {after}"
        else:
            before = self.a2s_before["spectators_before"]
            ok, after = source.client.slot_released(self.a.relay, before)
            what = f"relay viewers before {before}, after {after}"
        verdict = " - released" if ok else " - unknown" if ok is None else " - NOT RELEASED"
        return ok, after, "slot check: " + what + verdict

    # ---------------------------------------------------------------- run
    def run(self):
        a = self.a
        meta = {
            "args": vars(a),
            "dir": self.dir,
            "tz": tz_label(self.tz),
            "pid": os.getpid(),
            "start_utc": utc_iso(self.started_ns),
        }
        self.screen.start(os.path.join(self.dir, "stderr.log"))
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self._on_signal)
        try:
            self.pacer = source.Pacer(
                self.live or self.follow,
                a.speed,
                a.skip_s,
                time.time_ns() - NS if self.follow else 0,
            )
            if self.live:
                err = self.precheck()
                if err:
                    self.event("conn", "refused: " + err, "fail")
                    self.quit_why = "precheck"
                    return 2
            else:
                self.conn.state = "replay"
            if self.live:
                ip, port = a.relay.rsplit(":", 1)
                cap = os.path.join(self.dir, "capture.tvd")
                self.client = source.LiveClient(
                    ip,
                    int(port),
                    cap,
                    os.path.join(self.dir, "tvdump.log"),
                    name=a.name,
                    seconds=a.seconds,
                )
                self.event(
                    "conn",
                    f"connecting to {a.relay} ({clean(self.conn.hostname)}, "
                    f"relay {self.conn.players}/{self.conn.max_players})",
                    "info",
                )
                if not self.client.start():
                    self.event("conn", "network client did not start, see tvdump.log", "fail")
                    return 2
                self.logtail = source.LogTail(os.path.join(self.dir, "tvdump.log"))
                self.rec = source.Reader(cap, follow=True)
                threading.Thread(target=self.a2s_poll, daemon=True, name="a2s").start()
            elif self.follow:
                self.rec = source.Reader(a.replay, follow=True)
                self.event(
                    "play",
                    f"following {a.replay}: what it holds so far only sets "
                    "the state, lines start from now",
                )
            else:
                self.rec = source.Reader(a.replay)
                self.event(
                    "play",
                    f"replay {a.replay} at "
                    f"{'max speed' if a.speed <= 0 else 'x%g' % a.speed}"
                    + (f", skip {a.skip}" if a.skip else ""),
                )
            self.rec.start()
            self.loop()
        finally:
            self.shutdown(meta)
        # the network client gave up on its own: a standing refusal (alarm) or
        # an abnormal exit; the session holds what it saw until then
        if self.live and (self.quit_why == "alarm" or self.client_rc != 0):
            return 3
        return 0

    def _on_signal(self, sig, _frm):
        if self.quit:
            raise KeyboardInterrupt
        self.quit, self.quit_why = True, signal.Signals(sig).name

    def pump(self, budget_s=0.05):
        """Drain input items for up to budget_s. -> True if the queue ran dry."""
        end = time.monotonic() + budget_s
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return False
            item = self.held
            if item is None:
                try:
                    item = self.rec.q.get(timeout=min(left, 0.02))
                except queue.Empty:
                    return True
            t = item[1].t_ns if item[0] == "dg" else item[1]
            if self.past_end(t):
                self.held = None
                self.quit, self.quit_why = True, "seconds"
                return False
            wait = self.pacer.due(t)
            if wait > 0:
                self.held = item
                time.sleep(min(wait, left, 0.02))
                if wait > 0.02:
                    return True
                continue
            self.held = None
            self.pacer.seen(t)
            if item[0] == "dg":
                self.handle_dg(item[1])
            else:
                self.handle_event(*item[1:])

    def past_end(self, t_ns):
        """Replay with --seconds: the recording time `t_ns` is beyond the bound."""
        a = self.a
        if self.live or not a.seconds or not self.play_t0:
            return False
        return t_ns - self.play_t0 > a.seconds * NS

    def tick(self, dry):
        """Clock-driven work, done when the input queue is drained."""
        if dry and not self.pacer.skipping(self.pacer.media_now()):
            mnow = self.pacer.media_now()
            if mnow > self.now:
                self.now = mnow
        if dry:
            self.check_traffic(self.now)
        t = time.monotonic()
        t0, c0, _ = self.cpu
        if t - t0 >= 2.0:
            c1 = self._cpu_s()
            self.cpu_cores = (c1 - c0) / (t - t0)
            if self.client is not None:
                n1 = self.client.cpu_s()
                self.net_cpu = (n1 - self.cpu[2]) / (t - t0)
                self.cpu = (t, c1, n1)
            else:
                self.cpu = (t, c1, 0.0)
        if self.logtail is not None:
            for line in self.logtail.lines():
                self.event("tvd", clean(line), "fail" if line.startswith("[alarm]") else "info")
                if line.startswith("[alarm]"):
                    self.quit, self.quit_why = True, "alarm"
        k = self.screen.key()
        if k in ("q", "Q"):
            self.quit, self.quit_why = True, "key q"

    def check_traffic(self, now):
        tr = self.traffic.check(now)
        if not tr:
            return
        kind, gap = tr
        if kind == "stopped":
            self.event(
                "net",
                f"traffic stopped (no datagrams for {gap:.1f} s)",
                "fail",
                self.traffic.last_ns,
            )
        elif kind == "resumed":
            self.event("net", f"traffic resumed after {gap:.1f} s", "ok")
        else:
            self.event("net", "traffic started", "ok")

    def render(self, force=False):
        t = time.monotonic()
        if not force and t - self.last_render < 0.25 and not self.screen.pending:
            return
        self.last_render = t
        if self.screen.tty:
            self.screen.draw(self.block())
        else:
            if not self.a.monitor and t - self.last_status_line >= self.a.status_every:
                self.last_status_line = t
                for spans in self.block()[1:-1]:
                    self.screen.feed([("status ", "dim")] + spans)
            self.screen.draw(())

    def loop(self):
        a = self.a
        while not self.quit:
            dry = self.pump()
            self.tick(dry)
            self.render()
            if (
                a.seconds
                and not self.live
                and self.play_t0
                and (self.now - self.play_t0) / NS >= a.seconds
            ):
                self.quit, self.quit_why = True, "seconds"
            if self.live and not self.client.alive():
                self.quit, self.quit_why = True, "client exited"
            if (
                not self.live
                and self.rec.done.is_set()
                and self.rec.q.empty()
                and self.held is None
            ):
                self.quit, self.quit_why = True, "end of recording"

    def shutdown(self, meta):
        try:
            rc = None
            if self.client is not None:
                self.event("conn", "leaving relay ...", "info")
                self.render(force=True)
                rc = self.client_rc = self.client.stop()
                if rc is None:
                    self.event(
                        "conn", "network client killed: relay keeps the slot up to 300 s", "fail"
                    )
            if self.rec is not None and not self.live:
                self.rec.abandon.set()
                self.rec.stop_flag.set()
            elif self.rec is not None:
                self.rec.stop_flag.set()
                end = time.monotonic() + 3.0
                while time.monotonic() < end:
                    if self.pump(0.05) and self.rec.done.is_set():
                        break
                self.rec.abandon.set()
                self.tick(True)
            if self.logtail is not None:
                for line in self.logtail.lines():
                    self.event("tvd", clean(line), "info")
            if self.client is not None and self.a2s_before is not None:
                ok, after, text = self.slot_check()
                self.event("conn", text, "ok" if ok else "fail")
                meta["slot_released"] = ok
                meta["relay_before"] = self.a2s_before
                meta["relay_with_us"] = self.with_us
                meta["relay_after"] = after
            meta.update(
                {
                    "end_utc": utc_iso(time.time_ns()),
                    "quit": self.quit_why,
                    "tvdump_rc": rc,
                    "traffic": {
                        "in": self.traffic.total_pkts,
                        "bytes": self.traffic.total_bytes,
                        "split_parts": self.traffic.total_splits,
                        "stops": self.traffic.stops,
                        "out": self.traffic.total_out,
                    },
                    "framer": dict(self.framer.counters),
                    "conn": vars(self.conn),
                    "game_events": dict(self.game.counts),
                }
            )
            self.event("done", f"{self.quit_why or 'stopped'} -> {self.dir}")
        finally:
            with open(os.path.join(self.dir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=1, default=str)
            final = self.block() if self.screen.tty else []
            self.screen.stop(final[:-1])
            self.feed_fh.close()
            self.jsonl_fh.close()
