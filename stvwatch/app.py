"""Main loop: source -> framing -> game events and voice channels -> speech
segments -> audio -> recognizer, with a feed of events and phrases above a
pinned status block.

One thread owns all state; the reader thread and the recognizer talk to it
through queues.
"""

import json
import os
import queue
import signal
import threading
import time
import wave
from datetime import datetime, timezone
from itertools import pairwise

import numpy as np

from . import events as gamevents
from . import source, version
from .asr import recognizer as asrmod
from .asr import vad as vadmod
from .asr.parakeet import level_peak
from .model import NS, Channel, Conn, Traffic
from .net import a2s, wire
from .net import dump as dumpfmt
from .render import Screen, clean
from .serve import ServedScreen
from .stream.framing import Framer
from .stream.userinfo import STEAMID64_BASE, NickBook, StringTables
from .voice import audio, steamvoice
from .voice.segments import ChannelFrame, Segmenter

SIGNON = {3: "NEW", 4: "PRESPAWN", 5: "SPAWN", 6: "FULL", 7: "CHANGELEVEL"}
SR = 16000
CLOSE_S = 1.0  # end of speech: this long without frames from the channel
# the ONNX export of Parakeet holds positions for 5000 encoder frames: 400 s
MAX_UTT_MS = 400_000
CUT_FROM = 0.6  # a monologue piece ends at a pause in the last 40 % of --max-utt-ms
TSV_HEAD = (
    "t_start_utc",
    "t_end_utc",
    "steamid64",
    "nick",
    "model",
    "result",
    "text",
    "audio",
    "speech_ms",
    "asr_ms",
    "lag_ms",
)
# after t_local, so that a reader of the columns before keeps its positions
SLOT_HEAD = ("slot", "verified", "slot_steamid64")


# Every feed line type (docs/events.schema.json), or state -> style for a type
# whose lines differ. What people say leads in bold: voice white, chat yellow;
# a voice line is green while the player talks, yellow while it is recognized,
# a result without text dim. Console magenta; joins and leaves in their own
# muted green and red (not the green and red of our connection lines);
# connects, team and nick changes and server text players read in chat dimmed;
# kills and other server notices dimmest. Game lines are painted whole, except
# chat and voice, which keep their cyan nick; our own lines keep the blue tag.
LINE_STYLE = {
    "voice": {
        "talking": "green",
        "recognizing": "yellow",
        "text": "bold",
        "no text": "dim",
        "slot mismatch": "red",
    },
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
    "asr": {"ready": "", "failed": "red", "error": "red"},
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


def owner_sid(player):
    return STEAMID64_BASE + player.friends_id if player.friends_id else 0


def lag_style(seconds):
    """Recognition lag: text over 2 s behind the speech is late, over 5 s the
    recognizer does not keep up."""
    return "red" if seconds > 5.0 else "yellow" if seconds > 2.0 else ""


STATS = ("frames", "plc", "gap", "opus_bytes", "presses", "via_split", "slot_ok", "slot_bad")


class ChannelAudio:
    """Per-speaker audio of the utterance being built."""

    def __init__(self):
        self.seg_id = -1
        self.dec = None
        self.parts = []
        self.nsamp = 0
        self.start_ns = 0
        self.last_ns = 0
        self.open = False
        self.index = 0
        self.key = None  # (sid, index) of the open utterance
        self.reset_stats()

    def reset_stats(self):
        """Per-utterance transport facts for the speaking event in the feed."""
        self.frames = self.plc = self.gap = self.opus_bytes = 0
        self.presses = 0
        self.arrivals = []  # receive time of each voice message
        self.via_split = 0
        # voice messages whose slot holds / does not hold the payload's player
        self.slot_ok = self.slot_bad = 0
        self.slot, self.slot_owner = None, None  # the last slot; the first mismatch freezes both
        self.marks = []  # (nsamp, t_ns, msgs, STATS) after each voice message

    def mark(self, t_ns):
        self.marks.append((self.nsamp, t_ns, len(self.arrivals), [getattr(self, k) for k in STATS]))

    def split(self, cut):
        """The transport facts of the voice messages wholly in the first `cut`
        samples go to a copy, for the piece that ends there; this one keeps
        the rest, counted from the cut."""
        n = sum(1 for m in self.marks if m[0] <= cut)
        _s, last, msgs, head = self.marks[n - 1] if n else (0, self.start_ns, 0, [0] * len(STATS))
        piece = ChannelAudio()
        piece.start_ns, piece.last_ns = self.start_ns, last
        piece.slot, piece.slot_owner = self.slot, self.slot_owner
        piece.arrivals, self.arrivals = self.arrivals[:msgs], self.arrivals[msgs:]
        for k, v in zip(STATS, head, strict=True):
            setattr(piece, k, v)
            setattr(self, k, getattr(self, k) - v)
        self.marks = [
            (s - cut, t, m - msgs, [x - y for x, y in zip(c, head, strict=True)])
            for s, t, m, c in self.marks[n:]
        ]
        return piece

    def check_slot(self, m, steamid64):
        """Count one voice message against its slot's userinfo entry."""
        bad = m.owner is not None and m.owner.friends_id != steamid64 - STEAMID64_BASE
        if not self.slot_bad:
            self.slot, self.slot_owner = m.from_client, m.owner if bad else None
        self.slot_bad += bad
        self.slot_ok += m.owner is not None and not bad

    def verified(self):
        """False if any message's slot belonged to someone else, None if no
        message could be checked, else True."""
        return False if self.slot_bad else True if self.slot_ok else None


class Utt:
    """One utterance = one live line in the feed: talking -> recognizing ->
    final text (or no speech)."""

    def __init__(self, key, sid, start_ns):
        self.key, self.sid, self.start_ns = key, sid, start_ns
        self.end_ns = 0
        self.state = "talking"
        self.details = ""  # transport summary, set when it closes
        self.audio_s = 0.0
        self.cont = False  # a piece of a monologue after the first
        self.nick, self.spectator = "", None  # as of the end, see App.speaker
        self.slot, self.verified, self.slot_owner = None, None, None  # as of the end
        self.meta = {}  # the recognizer job, once closed


class App:
    def __init__(self, a):
        self.a = a
        self.live = a.relay is not None
        self.follow = a.follow is not None
        self.tz = a.tz
        self.model = a.asr
        self.q_out = queue.Queue()
        self.asr = None
        self.quit = False
        self.quit_why = ""
        self.conn = Conn()
        self.traffic = Traffic(quiet_s=a.quiet_ms / 1000)
        self.tables = StringTables()
        self.nicks = NickBook()
        self.channels = {}
        self.utts = {}  # key -> Utt with a live line
        self.audio = {}
        self.seg = Segmenter(close_s=CLOSE_S)
        self.counters = {"payload_bad": 0, "phrases": 0, "nospeech": 0, "utterances": 0, "wav": 0}
        self.game = gamevents.GameEvents(a.event_types)
        self.framer = Framer(
            keep_fates=False,
            on_table=self._on_table,
            on_info=self._on_info,
            on_packet=self._on_packet if a.debug else None,
            on_msg=self.game.on_msg,
            slot_owner=self.tables.owner,
        )
        self.game.attach(self.framer)
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
        self.build = version.build()
        self.version_due = False  # run(): the session's first line names the build
        self.screen = self._screen()
        self.dir = self._session_dir()
        self.feed_fh = open(os.path.join(self.dir, "feed.log"), "a", encoding="utf-8")
        # what --json prints, in any screen mode; errors as on the --json stdout
        self.jsonl_fh = open(
            os.path.join(self.dir, "events.jsonl"), "a", encoding="utf-8", errors="replace"
        )
        self.tsv_fh = open(os.path.join(self.dir, "transcript.tsv"), "a", encoding="utf-8")
        if self.tsv_fh.tell() == 0:
            head = TSV_HEAD + (("t_local",) if a.tz_given else ()) + SLOT_HEAD
            self.tsv_fh.write("\t".join(head) + "\n")

    # ---------------------------------------------------------------- setup
    def _screen(self):
        """The terminal, or with --serve the clients of a socket."""
        a = self.a
        if a.serve:
            hello = {"pid": os.getpid(), "version": self.build["version"], "tz": tz_label(self.tz)}
            return ServedScreen(a.serve, hello)
        return Screen(plain=a.plain, color=False if a.no_color else None)

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
        mid, entries = self.tables.feed(payload, start, end, self.framer.cur_session)
        self.nicks.update(entries)
        self.game.on_table(mid, entries)

    def _on_info(self, info):
        self.tables.reset()
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
        if rec is None:
            rec = {
                "type": spans[0][0].strip() if len(spans) > 1 else "info",
                "text": "".join(t for t, _s in spans[1:] if len(spans) > 1)
                or "".join(t for t, _s in spans),
            }
        if self.version_due:
            self.version_due = False
            v = self.build["version"]
            rec = dict(rec, extra=dict(rec.get("extra") or {}, version=v))
            tail = [(f"  [stv-watch {v}]", "dim")]
            spans, full = spans + tail, full and full + tail
        line = self.json_line(rec, t_ns)
        self.jsonl(line)
        if self.a.json:
            self.screen.feed([(line, "")])
        else:
            self.screen.feed([(local(t_ns, self.tz) + " ", "dim")] + spans)
        self.log(full or spans, t_ns, rec.get("steamid64", 0))

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
        msgs = self.framer.voice[:]
        del self.framer.voice[:]
        self.game_lines()
        if self.pacer.skipping(t):
            return
        self.play_t0 = self.play_t0 or t
        self.seg.clock(t)
        self.process_closed()
        for m in msgs:
            p = steamvoice.parse(m.data)
            if not p.crc_ok or p.error:
                self.counters["payload_bad"] += 1
                continue
            for fr in p.frames:
                self.on_frame(
                    ChannelFrame(p.steamid64, m.t_ns, m.tick, m.session, m.from_client, fr)
                )
            ca = self.audio.get(p.steamid64)
            if ca is not None and ca.open:
                ca.check_slot(m, p.steamid64)
                ca.arrivals.append(m.t_ns)
                ca.via_split += m.via_split
                ca.mark(m.t_ns)

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

    # ---------------------------------------------------------------- channels
    def channel(self, sid, t_ns):
        ch = self.channels.get(sid)
        if ch is None:
            ch = self.channels[sid] = Channel(sid, t_ns)
            self.audio[sid] = ChannelAudio()
            self.refresh_name(ch)
        return ch

    def refresh_name(self, ch):
        """The player's current nick, from the stream's userinfo table; the
        key stays the SteamID."""
        nick = self.nicks.nick(ch.sid64)
        if nick:
            ch.nick = clean(nick)

    def speaker(self, utt, ch):
        """(nick, spectator) of an utterance: live while it is open, as of its
        end once closed - the recognizer answers seconds of stream later, and
        by then the player may have changed team or name."""
        if utt.spectator is None:
            return ch.nick, self.game.unheard(ch.sid64)
        return utt.nick, utt.spectator

    def name_spans(self, nick, sid):
        return [(nick or steam2(sid), "cyan")]

    def spec_mark(self, spectator):
        """A spectator's voice reaches only spectators unless sv_alltalk."""
        return [(" [spec]", "dim")] if spectator else []

    def slot_state(self, utt):
        """(slot, verified, owner) of an utterance: the open piece's so far
        while talking, as of its end once closed."""
        if utt.state == "talking":
            ca = self.audio[utt.sid]
            return ca.slot, ca.verified(), ca.slot_owner
        return utt.slot, utt.verified, utt.slot_owner

    def slot_mark(self, utt):
        """The server put someone else in the slot this voice came from: the
        SteamID in the payload is the sender's claim, the slot the server's."""
        slot, verified, owner = self.slot_state(utt)
        if verified is not False:
            return []
        who = clean(owner.name) or steam2(owner_sid(owner))
        return [(f" [slot {slot}: {who}]", line_style("voice", "slot mismatch"))]

    def on_frame(self, cf):
        f = cf.frame
        sid = cf.steamid64
        ch = self.channel(sid, cf.t_ns)
        if f.kind == "opus":
            ch.frames += 1
            ch.opus_bytes.add(cf.t_ns, len(f.data))
            ch.opus_frames.add(cf.t_ns)
            ch.last_ns = cf.t_ns
        self.seg.feed(cf)
        self.process_closed()
        s = self.seg.open.get((cf.session, sid))
        if s is None or not s.items or s.items[-1][1] is not f:
            return
        ca = self.audio[sid]
        if not ca.open:
            ca.open, ca.start_ns, ca.index = True, cf.t_ns, ca.index + 1
            ch.talking = True
            ch.utt_start_ns = cf.t_ns
            self.speech_start(ch, ca, cf.t_ns)
        if ca.seg_id != s.id:
            if ca.seg_id >= 0 and ca.nsamp:
                self.add_pcm(sid, ca, np.zeros(int(0.2 * SR), np.float32), cf.t_ns)
            ca.seg_id = s.id
            ca.dec = audio.StreamDecoder(SR)
            ca.presses += 1
        st0 = dict(ca.dec.stats)
        pieces = ca.dec.feed(f.seq, f)
        d_plc = ca.dec.stats["plc"] - st0["plc"]
        d_gap = ca.dec.stats["gap_silence_frames"] - st0["gap_silence_frames"]
        ch.plc += d_plc
        ch.gap_frames += d_gap
        ca.plc += d_plc
        ca.gap += d_gap
        if f.kind == "opus":
            ca.frames += 1
            ca.opus_bytes += len(f.data)
        ch.resets += ca.dec.stats["resets"] - st0["resets"]
        for pcm in pieces:
            self.add_pcm(sid, ca, audio.limit(pcm), cf.t_ns)
        ca.last_ns = cf.t_ns

    def add_pcm(self, sid, ca, pcm, t_ns):
        ca.parts.append(pcm)
        ca.nsamp += len(pcm)
        cut = self.cut_point(ca)
        if cut is not None:
            self.finish_utterance(sid, t_ns, cut[1], keep_open=True, cut=cut[0])

    def cut_point(self, ca):
        """Where to end a piece of a monologue once it is longer than
        --max-utt-ms, in samples: the middle of the longest pause in the last
        40 % of the limit, pauses read from smoothed Silero decisions on the
        leveled piece; with no pause there, or no recognizer to read them, the
        limit itself (a hard cut). -> (sample, why) or None."""
        limit = self.a.max_utt_ms * SR // 1000
        if ca.nsamp <= limit:
            return None
        vad = getattr(self.asr.engine, "vad", None) if self.asr is not None else None
        if vad is not None:
            pcm = level_peak(asrmod.np_concat(ca.parts))
            dec = vadmod.smooth(vad.probs(pcm) > vadmod.THRESHOLD)
            cut = vadmod.longest_pause(dec, int(CUT_FROM * limit), limit)
            if cut is not None:
                return cut, "pause"
        return limit, "max_len"

    def process_closed(self):
        if not self.seg.closed:
            return
        for s in self.seg.closed:
            if s.close_reason in ("clock", "session", "end"):
                ca = self.audio.get(s.steamid64)
                if ca is not None and ca.open and ca.seg_id == s.id:
                    self.finish_utterance(s.steamid64, s.close_ns, s.close_reason)
        del self.seg.closed[:]

    def finish_utterance(self, sid, t_ns, why, keep_open=False, cut=None):
        ca = self.audio[sid]
        ch = self.channels[sid]
        pcm = asrmod.np_concat(ca.parts)
        rest, facts, closed = None, ca, t_ns
        if cut is not None and cut < len(pcm):
            pcm, rest = pcm[:cut], pcm[cut:]
            t_ns -= len(rest) * NS // SR
            facts = ca.split(cut)
        meta = {
            "sid": sid,
            "start_ns": ca.start_ns,
            "end_ns": t_ns,
            "closed_ns": closed,  # a monologue piece ends before it is cut: lag from the cut
            "why": why,
            "audio_s": len(pcm) / SR,
            "wav": self.write_wav(sid, ca, pcm),
        }
        self.counters["utterances"] += 1
        ch.utterances += 1
        utt = self.utts.get(ca.key)
        meta["key"] = ca.key
        if utt is not None:
            utt.details = self.details(facts, t_ns, why, len(pcm) / SR)
            utt.audio_s = len(pcm) / SR
            utt.end_ns, utt.state = t_ns, "recognizing"
            self.refresh_name(ch)
            utt.nick, utt.spectator = ch.nick, self.game.unheard(sid)
            utt.slot, utt.verified, utt.slot_owner = facts.slot, facts.verified(), facts.slot_owner
            utt.meta = meta
        if self.asr is None or not len(pcm):
            self.finalize(utt, "", meta)
        elif not self.asr.utterance(sid, pcm, meta):
            self.finalize(utt, "", dict(meta, result="not recognized, queue full"))
        ca.parts, ca.nsamp = [], 0
        if rest is None:
            ca.reset_stats()
        if keep_open:
            ca.start_ns = t_ns
            ca.index += 1
            self.speech_start(ch, ca, ca.start_ns)
            self.utts[ca.key].cont = True
            if rest is not None:
                ca.parts, ca.nsamp = [rest], len(rest)
            return
        ca.open = False
        ca.seg_id = -1
        ch.talking = False

    def speech_start(self, ch, ca, t_ns):
        """A live line for the new utterance."""
        ca.key = (ch.sid64, ca.index)
        utt = self.utts[ca.key] = Utt(ca.key, ch.sid64, t_ns)
        utt.first = ch.utterances == 0
        self.screen.live_open(ca.key, self.progress(utt))

    def details(self, ca, t_ns, why, audio_s):
        """Transport summary of a closed utterance: audio s / arrival span s,
        opus frames, concealed, silence-filled, bit rate, key presses, voice
        messages and their share in -2, distinct arrivals with the median and
        largest gap, how long after the last frame it closed (and why, unless
        by the clock)."""
        arr = ca.arrivals
        # several voice messages ride one datagram (or one joined -2 packet):
        # the arrival rhythm is that of distinct receive times
        times = sorted(set(arr))
        gaps = sorted(b - a for a, b in pairwise(times))
        p50 = gaps[len(gaps) // 2] / 1e6 if gaps else 0.0
        mx = gaps[-1] / 1e6 if gaps else 0.0
        span = (ca.last_ns - ca.start_ns) / NS
        kbps = ca.opus_bytes * 8 / (ca.frames * 0.020) / 1000 if ca.frames else 0.0
        via = 100 * ca.via_split / len(arr) if arr else 0.0
        close = (t_ns - ca.last_ns) / NS
        return (
            f"{audio_s:.1f}s/{span:.1f}s fr {ca.frames} plc {ca.plc} gap {ca.gap}"
            f" {kbps:.0f}kb/s press {ca.presses} msg {len(arr)} -2 {via:.0f}%"
            f" arr {len(times)} p50 {p50:.0f} max {mx:.0f}ms"
            f" {'' if why == 'clock' else why + ' '}+{close:.1f}s"
        )

    VOICE_TAG = ("voice ", "blue")

    def progress(self, utt):
        """Live line of an utterance: who, state, seconds so far (transport
        numbers with --debug)."""
        ch = self.channels[utt.sid]
        nick, spectator = self.speaker(utt, ch)
        out = (
            [(local(utt.start_ns, self.tz) + " ", "dim"), self.VOICE_TAG]
            + self.name_spans(nick, utt.sid)
            + self.slot_mark(utt)
            + self.spec_mark(spectator)
            + ([(" (cont)", "dim")] if utt.cont else [])
        )
        if utt.state == "talking":
            ca = self.audio[utt.sid]
            secs = max(0.0, (self.now - utt.start_ns) / NS)
            out.append((f" talking {secs:.1f}s", line_style("voice", "talking")))
            if self.a.debug:
                kbps = ca.opus_bytes * 8 / (ca.frames * 0.020) / 1000 if ca.frames else 0.0
                out.append(
                    (f" {kbps:.0f}kb/s fr {ca.frames} gap {ca.gap} msg {len(ca.arrivals)}", "dim")
                )
                if utt.first:
                    out.append((f" {steam2(utt.sid)} new", "dim"))
        else:
            out.append((" recognizing", line_style("voice", "recognizing")))
            out.append((" " + utt.details if self.a.debug else f" {utt.audio_s:.1f}s", "dim"))
        return out

    def write_wav(self, sid, ca, pcm):
        if self.a.no_audio or not len(pcm):
            return ""
        name = f"{local(ca.start_ns, self.tz, '%H%M%S')}_{sid}_{ca.index}.wav"
        path = os.path.join(self.dir, "audio", name)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(audio.to_int16(pcm).tobytes())
        except OSError:
            return ""
        self.counters["wav"] += 1
        return os.path.join("audio", name)

    # ---------------------------------------------------------------- recognizer results
    def handle_result(self, r):
        kind = r[0]
        if kind == "loaded":
            _k, state, err, load_s = r
            if state == "ready":
                self.event(
                    "asr",
                    f"{self.model} ready in {load_s:.1f} s ({self.a.threads} threads)",
                    "ready",
                )
            else:
                self.event("asr", f"{self.model} failed: {clean(err)}", "failed")
            return
        if kind == "error":
            self.event("asr", f"error on {r[1]}: {clean(r[2])}", "error")
            return
        if kind == "final":
            meta = r[3]
            self.finalize(self.utts.get(meta.get("key")), clean(r[2]), meta)

    def finalize(self, utt, text, meta):
        """The utterance's live line becomes its final line: who and what was
        said, bright; transport and recognition numbers, dim."""
        if utt is None:
            return
        del self.utts[utt.key]
        ch = self.channels[utt.sid]
        t_end = utt.end_ns or meta.get("end_ns") or self.now
        lag = max(0.0, (self.now - (meta.get("closed_ns") or t_end)) / NS)
        head = [(local(utt.start_ns, self.tz) + " ", "dim")]
        tail = utt.details + (
            "" if self.asr is None else f" asr {meta.get('asr_ms', 0) / 1000:.2f}s +{lag:.1f}s"
        )
        speech = meta.get("speech_ms")
        label = ""
        if not text:
            label = meta.get("result") or ("no speech" if self.asr is not None else "asr off")
            if label == "no speech":
                ch.nospeech += 1
                self.counters["nospeech"] += 1
            said = [(": " + label, line_style("voice", "no text"))]
            self.voice_close(utt, ch, head, said, "", label, tail, speech)
        else:
            ch.finals += 1
            self.counters["phrases"] += 1
            said = [(": ", ""), (text, line_style("voice", "text"))]
            self.voice_close(utt, ch, head, said, text, "", tail, speech)
        row = [
            utc_iso(utt.start_ns),
            utc_iso(t_end),
            str(ch.sid64),
            self.speaker(utt, ch)[0],
            self.model or "",
            label or "text",
            text,
            meta.get("wav", ""),
            "" if speech is None else str(speech),
            str(meta.get("asr_ms", 0)),
            str(round(lag * 1000)),
        ]
        if self.a.tz_given:
            row.append(local(utt.start_ns, self.tz, "%Y-%m-%d %H:%M:%S"))
        row += [
            "" if utt.slot is None else str(utt.slot),
            {True: "true", False: "false", None: ""}[utt.verified],
            str(owner_sid(utt.slot_owner)) if utt.verified is False else "",
        ]
        self.tsv_fh.write("\t".join(f.replace("\t", " ").replace("\n", " ") for f in row) + "\n")
        self.tsv_fh.flush()

    def voice_close(self, utt, ch, head, said, text, label, tail, speech=None):
        """Final line of an utterance. On screen: who, what and how long, the
        transport and recognition numbers only with --debug; feed.log and
        --json always get them all."""
        nick, spectator = self.speaker(utt, ch)
        who = (
            [self.VOICE_TAG]
            + self.name_spans(nick, ch.sid64)
            + self.slot_mark(utt)
            + self.spec_mark(spectator)
            + ([(" (cont)", "dim")] if utt.cont else [])
            + said
        )
        full = who + [("  " + tail + ("" if utt.slot is None else f" slot {utt.slot}"), "dim")]
        spans = full if self.a.debug else who + [(f"  {utt.audio_s:.1f}s", "dim")]
        extra = {
            "result": label or "text",
            "continued": utt.cont,
            "spectator": spectator,
            "details": tail.strip(),
            "t_end_utc": utc_iso(utt.end_ns or self.now),
        }
        if speech is not None:
            extra["speech_ms"] = speech
        extra["slot"], extra["verified"] = utt.slot, utt.verified
        if utt.verified is False:
            extra["slot_steamid64"] = owner_sid(utt.slot_owner)
        line = self.json_line(
            {"type": "voice", "steamid64": ch.sid64, "nick": nick, "text": text, "extra": extra},
            utt.start_ns,
        )
        self.jsonl(line)
        if self.a.json:
            self.screen.live_close(utt.key, [(line, "")])
        else:
            self.screen.live_close(utt.key, head + spans, cont=[("^ ", "dim")])
        self.log(full, utt.start_ns, ch.sid64)

    # ---------------------------------------------------------------- status block
    BLOCK_LINES = 6

    def block(self):
        """Fixed height: rule, connection, traffic, voice, recognizer, hint.
        By default what a watcher acts on; transport and recognizer internals
        with --debug. Per-speaker details live in the speaker's line."""
        now = self.now or time.time_ns()
        dbg = self.a.debug
        c, tr = self.conn, self.traffic
        L = [[(f" stv-watch {self.build['version']} ", "bar")]]
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

        talking = sum(1 for ch in self.channels.values() if ch.talking)
        speakers = [
            (f"  speakers {len(self.channels)} talking {talking}", "green" if talking else "")
        ]
        if dbg:
            vm = fc.get("voice_msgs", 0)
            via = fc.get("voice_msgs_via_split", 0)
            L.append(
                [
                    ("voice", "blue"),
                    (f" msgs {vm}", ""),
                    (f" (via -2 {100 * via / vm:.0f}%)" if vm else "", "dim"),
                    (f"  bad {self.counters['payload_bad']}", ""),
                ]
                + speakers
                + [
                    (
                        f"  utt {self.counters['utterances']} phrases {self.counters['phrases']}"
                        f" nospeech {self.counters['nospeech']}",
                        "",
                    )
                ]
            )
        else:
            L.append(
                [("voice", "blue")] + speakers + [(f"  phrases {self.counters['phrases']}", "")]
            )

        if self.asr is None:
            L.append([("asr  ", "blue"), ("off", "dim")])
        else:
            jobs, pend_s = self.asr.queue_depth()
            s = self.asr.stats.summary(now)
            st = self.asr.state
            st_span = (
                "  " + st,
                "green" if st == "ready" else "yellow" if st == "loading" else "red",
            )
            lag = (
                f"  lag {s['lag_last']:.1f}s"
                + (f" p90 {s['lag_p90']:.1f}s" if s["jobs_min"] else ""),
                lag_style(s["lag_last"]),
            )
            if dbg:
                L.append(
                    [
                        ("asr  ", "blue"),
                        (f"{self.model} t{self.a.threads}", ""),
                        st_span,
                        (
                            f"  queue {max(0, jobs)} ({max(0.0, pend_s):.1f}s)",
                            "yellow" if pend_s > 5 else "",
                        ),
                        lag,
                        (f"  RTF {s['rtf']:.2f}" if s["jobs_min"] else "  RTF -", ""),
                    ]
                )
            else:
                L.append([("asr  ", "blue"), (self.model, ""), st_span, lag])

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
            "version": self.build["version"],
            "build": self.build,
            "args": vars(a),
            "model": self.model,
            "dir": self.dir,
            "tz": tz_label(self.tz),
            "pid": os.getpid(),
            "start_utc": utc_iso(self.started_ns),
        }
        self.version_due = True
        self.screen.start(os.path.join(self.dir, "stderr.log"))
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self._on_signal)
        try:
            self.pacer = source.Pacer(
                self.live or self.follow,
                a.speed,
                a.skip_ms / 1000,
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
            if self.model:
                self.asr = asrmod.Recognizer(
                    self.model,
                    a.threads,
                    a.models_dir,
                    a.min_speech_ms,
                    self.pacer.media_now,
                    self.q_out,
                    wait=not self.live,
                )
                # Load before reading or connecting: the load holds the GIL for
                # seconds, which would stall the reader (a fake traffic stop in
                # a replay) and miss the first phrases live.
                while self.asr.state == "loading" and not self.quit:
                    self.tick(False)
                    self.render()
                    time.sleep(0.1)
                self.tick(False)
                if self.quit:
                    return 0
                if self.asr.state == "failed":
                    self.quit_why = "asr failed"
                    return 2
            if self.live:
                ip, port = a.relay.rsplit(":", 1)
                cap = os.path.join(self.dir, "capture.tvd")
                self.client = source.LiveClient(
                    ip,
                    int(port),
                    cap,
                    os.path.join(self.dir, "tvdump.log"),
                    name=a.name,
                    duration_ms=a.duration_ms,
                    extra=["--stv-version", self.build["version"]],
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
                    + (f", skip {a.skip_ms / 1000:g} s" if a.skip_ms else ""),
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
                self.quit, self.quit_why = True, "duration"
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
        """Replay with --duration-ms: the recording time `t_ns` is beyond the bound."""
        a = self.a
        if self.live or not a.duration_ms or not self.play_t0:
            return False
        return t_ns - self.play_t0 > a.duration_ms * 1_000_000

    def tick(self, dry):
        """Clock-driven work, done when the input queue is drained."""
        if dry and not self.pacer.skipping(self.pacer.media_now()):
            mnow = self.pacer.media_now()
            if mnow > self.now:
                self.now = mnow
            self.seg.clock(self.now)
            self.process_closed()
        if dry:
            self.check_traffic(self.now)
        while True:
            try:
                self.handle_result(self.q_out.get_nowait())
            except queue.Empty:
                break
        for ch in self.channels.values():
            self.refresh_name(ch)
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
            for utt in self.utts.values():
                self.screen.live_update(utt.key, self.progress(utt))
            self.screen.draw(self.block())
        else:
            if not self.a.monitor and t - self.last_status_line >= self.a.status_every_ms / 1000:
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
                a.duration_ms
                and not self.live
                and self.play_t0
                and self.now - self.play_t0 >= a.duration_ms * 1_000_000
            ):
                self.quit, self.quit_why = True, "duration"
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
            self.seg.flush(self.now)
            self.process_closed()
            if self.asr is not None:
                end = time.monotonic() + self.a.drain_ms / 1000
                while time.monotonic() < end and self.asr.state != "failed":
                    jobs, _p = self.asr.queue_depth()
                    if self.asr.state == "ready" and not jobs:
                        break
                    self.tick(False)
                    self.render()
                    time.sleep(0.05)
                self.asr.close()
                self.tick(False)
            for utt in list(self.utts.values()):
                self.finalize(utt, "", dict(utt.meta, result="not recognized before exit"))
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
                    "segments": dict(self.seg.counters),
                    "counters": self.counters,
                    "conn": {
                        k.removesuffix("_ns") + "_utc" if k.endswith("_ns") else k: (
                            (utc_iso(v) if v else None) if k.endswith("_ns") else v
                        )
                        for k, v in vars(self.conn).items()
                    },
                    "game_events": dict(self.game.counts),
                    "speakers": {
                        str(s): {
                            "nick": ch.nick,
                            "audio_ms": ch.frames * 20,
                            "frames": ch.frames,
                            "utterances": ch.utterances,
                            "phrases": ch.finals,
                        }
                        for s, ch in self.channels.items()
                    },
                }
            )
            if self.asr is not None:
                meta["asr"] = {
                    "state": self.asr.state,
                    "error": self.asr.error,
                    "load_ms": round(self.asr.load_s * 1000),
                    "audio_ms": round(self.asr.stats.total_audio * 1000),
                    "compute_ms": round(self.asr.stats.total_compute * 1000),
                    "jobs": self.asr.stats.jobs,
                }
            self.event("done", f"{self.quit_why or 'stopped'} -> {self.dir}")
        finally:
            with open(os.path.join(self.dir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=1, default=str)
            final = self.block() if self.screen.tty else []
            self.screen.stop(final[:-1])
            self.feed_fh.close()
            self.jsonl_fh.close()
            self.tsv_fh.close()
