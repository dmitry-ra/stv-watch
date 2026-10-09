"""Voice in the viewer: utterances from the segmenter, monologue pieces, the
live and final lines, the recognizer behind them, transcript.tsv and WAVs.
The recognizer runs a stand-in engine here; the real one is in test_parakeet."""

import json
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from helpers import (
    T0,
    account,
    packet,
    steamid64,
    table_update,
    userinfo_entries,
    write_create,
    write_recording,
)
from make_sample import server_info
from voicegen import demo, noise, tone, voice_plan
from voicegen import payload as voice_payload

from stvwatch import cli
from stvwatch.app import SLOT_HEAD, SR, TSV_HEAD, App, ChannelAudio, Utt
from stvwatch.asr import recognizer, vad, weights
from stvwatch.cli import parse_args
from stvwatch.model import Channel
from stvwatch.net import netchan, wire
from stvwatch.render import rows_for
from stvwatch.source import Pacer
from stvwatch.stream.userinfo import Player


def steam2(acc):
    return f"STEAM_0:{acc & 1}:{acc >> 1}"


def session_of(out):
    (session,) = list(out.iterdir())
    return session


class Lines:
    """Stand-in screen: what the app hands over, not how a terminal draws it."""

    def __init__(self):
        self.closed, self.fed = [], []

    def live_open(self, key, spans):
        pass

    def feed(self, spans):
        self.fed.append(spans)

    def live_close(self, key, spans, cont=()):
        self.closed.append(spans)


def text_of(spans):
    return "".join(t for t, _s in spans)


def bare_app(tmp_path, *extra):
    app = App(parse_args(["--replay", "x.tvd", "--out", str(tmp_path), *extra]))
    app.pacer = Pacer(False)
    app.screen = Lines()
    return app


class LoudVad:
    """Stand-in for Silero: speech wherever a window is loud."""

    def probs(self, pcm):
        n = len(pcm) // vad.VAD_WINDOW
        w = pcm[: n * vad.VAD_WINDOW].reshape(n, vad.VAD_WINDOW)
        return (np.abs(w).max(axis=1) > 0.02).astype(np.float32)


class Engine:
    """Stand-in for an utterance engine: the text names the clip's length."""

    def __init__(self, delay=0.0, text=None):
        self.delay, self.text, self.seen = delay, text, []
        self.fail = None
        self.vad = LoudVad()

    def open(self):
        eng, parts = self, []

        class Stream:
            def push(self, pcm):
                parts.append(pcm)
                return []

            def finish(self):
                pcm = np.concatenate(parts)
                eng.seen.append(len(pcm))
                time.sleep(eng.delay)
                if eng.fail is not None and eng.fail(len(pcm)):
                    raise RuntimeError("bad_alloc")
                text = eng.text if eng.text is not None else f"heard {len(pcm) / SR:.2f}s"
                speech = [("speech_ms", vad.speech_ms(eng.vad.probs(pcm)))]
                return speech + ([("final", text)] if text else [])

        return Stream()


@pytest.fixture
def engine(monkeypatch):
    e = Engine()
    monkeypatch.setattr(recognizer, "build", lambda name, threads, models_dir, min_ms: e)
    return e


def replay(rec, out, *extra):
    args = parse_args(["--replay", rec, "--speed", "0", "--json", "--out", str(out), *extra])
    rc = App(args).run()
    session = session_of(out)
    lines = [json.loads(ln) for ln in (session / "events.jsonl").read_text().splitlines()]
    rows = [ln.split("\t") for ln in (session / "transcript.tsv").read_text().splitlines()]
    return rc, session, lines, rows


def test_a_quick_repress_joins_the_utterance_and_a_pause_ends_it(tmp_path):
    a = steamid64(1)
    plan = [
        (0, voice_payload(a, 0)),
        (60, voice_payload(a, 3)),
        (300, voice_payload(a, 0)),  # new key press 0.3 s later
        (3000, voice_payload(a, 0)),  # after a 2.7 s pause
    ]
    timed = [(ms, packet(n + 1, [(1, p)])) for n, (ms, p) in enumerate(plan)]
    timed += [(ms, packet(5 + k)) for k, ms in enumerate(range(3020, 4500, 20))]
    rec = str(tmp_path / "r.tvd")
    write_recording(rec, [(T0 + ms * 1_000_000, d) for ms, d in timed])
    out = tmp_path / "out"
    args = parse_args(["--replay", rec, "--speed", "0", "--plain", "--out", str(out)])
    assert App(args).run() == 0
    session = session_of(out)
    meta = json.loads((session / "meta.json").read_text())
    assert meta["counters"]["utterances"] == 2
    assert meta["speakers"][str(a)]["frames"] == 12
    assert len(list((session / "audio").iterdir())) == 2
    # one final line per utterance, with its transport summary
    ends = [ln for ln in (session / "feed.log").read_text().splitlines() if ": asr off " in ln]
    assert len(ends) == 2
    assert ["fr 9 " in ends[0], "press 2 " in ends[0]] == [True, True]
    assert ["fr 3 " in ends[1], "press 1 " in ends[1]] == [True, True]


@pytest.mark.parametrize("debug", [False, True])
def test_voice_lines_and_block_are_practical_by_default_and_full_with_debug(tmp_path, debug):
    sid = steamid64(1)
    app = bare_app(tmp_path, *(["--debug"] if debug else []))
    ch = app.channels[sid] = Channel(sid, T0)
    ch.nick = "Pensioner"
    app.audio[sid] = ChannelAudio()
    utt = app.utts[(sid, 1)] = Utt((sid, 1), sid, T0)
    utt.first = True
    app.now = T0 + 2 * 10**9
    live = text_of(app.progress(utt))
    utt.details = (
        "3.1s/3.1s fr 139 plc 0 gap 0 23kb/s press 2 msg 48 -2 92% arr 11 p50 312 max 789ms +1.0s"
    )
    utt.audio_s, utt.state, utt.end_ns = 3.1, "recognizing", T0 + 3 * 10**9
    rec = text_of(app.progress(utt))
    app.finalize(utt, "a phrase as long as said in game", {})
    final = app.screen.closed[0]
    logged = (next(tmp_path.iterdir()) / "feed.log").read_text().split("\t", 1)[1]
    assert logged.startswith(f"{sid}\t")
    logged = logged.split("\t", 1)[1]
    block = [text_of(line) for line in app.block()[1:5]]
    if not debug:
        assert live == "09:40:00.000 voice Pensioner talking 2.0s"
        assert rec == "09:40:00.000 voice Pensioner recognizing 3.1s"
        assert text_of(final) == (
            "09:40:00.000 voice Pensioner: a phrase as long as said in game  3.1s"
        )
        assert rows_for(final, 80) == 1
        assert block[1:] == [
            "net  no traffic yet  lost 0",
            "voice  speakers 1 talking 0  phrases 1",
            "asr  off",
        ]
    else:
        assert live == (
            "09:40:00.000 voice Pensioner talking 2.0s 0kb/s fr 0 gap 0 msg 0"
            f" {steam2(account(1))} new"
        )
        assert rec == "09:40:00.000 voice Pensioner recognizing " + utt.details
        assert text_of(final).endswith("game  " + utt.details)
        assert rows_for(final, 120) == 2
        assert block[1].startswith("net  no traffic yet  in 0/s 0.0KB/s  -2 0.0% (0)")
        assert block[2] == ("voice msgs 0  bad 0  speakers 1 talking 0  utt 0 phrases 1 nospeech 0")
    # the session's feed.log holds everything whatever is on screen
    assert logged == "voice Pensioner: a phrase as long as said in game  " + utt.details + "\n"


class Pieces:
    """Stand-in recognizer: what reaches it, nothing comes back."""

    def __init__(self, with_vad):
        self.engine = SimpleNamespace(vad=LoudVad()) if with_vad else SimpleNamespace()
        self.lengths, self.metas = [], []

    def utterance(self, sid, pcm, meta):
        self.lengths.append(round(len(pcm) / SR, 2))
        self.metas.append(meta)
        return True


def monologue(tmp_path, seconds, pauses, with_vad=True, *extra):
    """A tone of `seconds` with silent (start, length) pauses, fed in 20 ms
    chunks; -> (piece lengths s, why each piece ended, continued marks)."""
    app = bare_app(tmp_path, *extra)
    app.asr = Pieces(with_vad)
    speak(app, seconds, pauses)
    whys = [u.details.split(" ")[-2] for u in app.utts.values()]
    conts = [u.cont for u in app.utts.values()]
    return app.asr.lengths, whys, conts


def speak(app, seconds, pauses):
    sid = steamid64(1)
    ch = app.channels[sid] = Channel(sid, T0)
    ca = app.audio[sid] = ChannelAudio()
    ca.open, ca.start_ns, ca.index = True, T0, 1
    app.speech_start(ch, ca, T0)
    step = int(0.02 * SR)
    sound = (0.3 * np.sin(np.arange(step) * 0.3)).astype(np.float32)
    for k in range(seconds * 50):
        t = k / 50
        quiet = any(p <= t < p + n for p, n in pauses)
        app.add_pcm(sid, ca, np.zeros(step, np.float32) if quiet else sound, T0 + int(t * 1e9))
    app.finish_utterance(sid, T0 + seconds * 10**9, "end")


def test_a_monologue_is_cut_at_the_longest_pause_of_the_last_40_percent(tmp_path):
    """--max-utt-ms 30000: once a piece passes 30 s it ends in the middle of
    the longest pause whose middle lies in 18-30 s of it (pauses from the
    smoothed VAD: each loses its first 4 windows to the hangover); the rest
    starts the next piece. The longest pause, at 10 s, is too early; the short
    one at 28 s loses to the longer one at 25 s."""
    pauses = ((10.0, 1.5), (20.0, 0.4), (25.0, 1.0), (28.0, 0.4), (50.0, 0.6))
    lengths, whys, conts = monologue(tmp_path, 70, pauses, True, "--max-utt-ms", "30000")
    first = 25.0 + (4 * 512 / SR + 1.0) / 2
    assert lengths[0] == pytest.approx(first, abs=0.04)
    assert sum(lengths[:2]) == pytest.approx(50.0 + (4 * 512 / SR + 0.6) / 2, abs=0.04)
    assert sum(lengths) == 70.0 and len(lengths) == 3
    assert whys == ["pause", "pause", "end"] and conts == [False, True, True]


def test_the_lag_of_a_monologue_piece_runs_from_its_cut(tmp_path, engine):
    """The piece's audio ends at the pause, 4.5 s before the limit cuts it at
    30 s; the lag tells how far the recognizer is behind, so it runs from the
    cut: in the transcript and in the block alike."""
    app = bare_app(tmp_path, "--max-utt-ms", "30000", "--asr", "parakeet")
    app.now = T0 + 31 * 10**9
    app.asr = recognizer.Recognizer("parakeet", 2, "", 0, lambda: app.now, app.q_out)
    speak(app, 31, ((25.0, 1.0),))
    first = app.q_out.get(timeout=5)
    while first[0] != "final" or first[3]["why"] != "pause":
        first = app.q_out.get(timeout=5)
    app.handle_result(first)
    row = (next(tmp_path.iterdir()) / "transcript.tsv").read_text().splitlines()[1].split("\t")
    assert (row[1][11:], row[10]) == ("09:40:25.548Z", "1000")
    assert app.asr.stats.done[0][3] == 1.0


@pytest.mark.parametrize("with_vad", [True, False])
def test_with_no_pause_or_no_vad_the_cut_is_hard_at_the_limit(tmp_path, with_vad):
    pauses = () if with_vad else ((25.0, 1.0),)
    lengths, whys, _conts = monologue(tmp_path, 70, pauses, with_vad, "--max-utt-ms", "30000")
    assert lengths == [30.0, 30.0, 10.0] and whys == ["max_len", "max_len", "end"]


def test_by_default_a_monologue_runs_two_minutes_and_at_most_400_s(tmp_path, capsys):
    assert parse_args(["--replay", "x"]).max_utt_ms == 120_000
    lengths, _w, _c = monologue(tmp_path, 125, ((60.0, 1.0), (100.0, 1.0)))
    assert lengths[0] == pytest.approx(100.0 + (4 * 512 / SR + 1.0) / 2, abs=0.04)
    assert parse_args(["--replay", "x", "--max-utt-ms", "400000"]).max_utt_ms == 400_000
    for bad in ("400001", "999", "30.5", "-1"):
        with pytest.raises(SystemExit):
            parse_args(["--replay", "x", "--max-utt-ms", bad])
    assert "at most 400 s in one call" in capsys.readouterr().err


@pytest.mark.parametrize("debug", [False, True])
def test_recognition_lag_turns_yellow_past_2s_and_red_past_5s(tmp_path, debug):
    app = bare_app(tmp_path, "--asr", "parakeet", *(["--debug"] if debug else []))
    got = []
    for lag in (2.0, 2.1, 5.0, 5.1):
        stats = SimpleNamespace(
            summary=lambda now, lag=lag: {
                "lag_last": lag,
                "lag_p90": lag,
                "jobs_min": 0,
                "rtf": 0.0,
            }
        )
        app.asr = SimpleNamespace(queue_depth=lambda: (0, 0.0), stats=stats, state="ready")
        got.append(next(st for t, st in app.block()[4] if t.startswith("  lag ")))
    assert got == ["", "yellow", "yellow", "red"]


def test_a_spectators_voice_is_marked_on_screen_and_in_json(tmp_path):
    sid = steamid64(1)
    app = bare_app(tmp_path)
    app.channels[sid] = Channel(sid, T0)
    app.audio[sid] = ChannelAudio()
    app.game.unheard = lambda s: s == sid
    for n in (1, 2):
        app.utts[(sid, n)] = Utt((sid, n), sid, T0)
    app.finalize(app.utts[(sid, 1)], "hi", {})
    app.game.unheard = lambda s: False
    app.finalize(app.utts[(sid, 2)], "hi", {})
    assert [" [spec]" in text_of(c) for c in app.screen.closed] == [True, False]
    lines = (next(tmp_path.iterdir()) / "events.jsonl").read_text().splitlines()
    assert [json.loads(ln)["spectator"] for ln in lines] == [True, False]


def test_the_speaker_is_as_of_the_utterance_end_not_the_recognizer_result(tmp_path):
    """The result comes seconds of stream after the end; a team change or a
    rename in between belongs to the game, not to what was said."""
    sid = steamid64(1)
    app = bare_app(tmp_path)
    app.asr = Pieces(False)
    ch = app.channels[sid] = Channel(sid, T0)
    ch.nick = "Pensioner"
    ca = app.audio[sid] = ChannelAudio()
    ca.open, ca.start_ns, ca.index = True, T0, 1
    app.game.team[account(1)] = 3
    app.speech_start(ch, ca, T0)
    app.add_pcm(sid, ca, np.full(SR, 0.3, np.float32), T0)
    app.finish_utterance(sid, T0 + 10**9, "clock")
    app.game.team[account(1)] = 1
    ch.nick = "Spectating"
    app.handle_result(("final", sid, "hi", app.asr.metas[0]))
    (line,) = (next(tmp_path.iterdir()) / "events.jsonl").read_text().splitlines()
    rec = json.loads(line)
    assert (rec["nick"], rec["spectator"]) == ("Pensioner", False)
    assert text_of(app.screen.closed[0]).startswith("09:40:00.000 voice Pensioner: hi ")
    row = (next(tmp_path.iterdir()) / "transcript.tsv").read_text().splitlines()[-1]
    assert row.split("\t")[3] == "Pensioner"


def test_recognized_replay_texts_transcript_and_wavs(tmp_path, engine):
    """The demo recording with a stand-in engine: every utterance gets its
    text, a transcript row and a WAV of the audio the engine heard; with
    --tz the WAV names and an extra transcript column are in that zone."""
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    rc, session, lines, rows = replay(
        rec, tmp_path / "o", "--asr", "parakeet", "--tz", "Asia/Tokyo"
    )
    assert rc == 0
    voice = sorted((r for r in lines if r["type"] == "voice"), key=lambda r: r["t_utc"])
    assert [(r["nick"], r["text"], r["result"], r["continued"]) for r in voice] == [
        ("alice", "heard 4.00s", "text", False),
        ("bob", "heard 1.50s", "text", False),
        ("alice", "heard 1.00s", "text", False),
    ]
    assert all(" asr " in r["details"] for r in voice)
    assert rows[0] == list(TSV_HEAD) + ["t_local", *SLOT_HEAD]
    rows = sorted(rows[1:])
    assert [(r[3], r[4], r[5], r[6]) for r in rows] == [
        ("alice", "parakeet", "text", "heard 4.00s"),
        ("bob", "parakeet", "text", "heard 1.50s"),
        ("alice", "parakeet", "text", "heard 1.00s"),
    ]
    assert [r[0] for r in rows] == [r["t_utc"] for r in voice]
    assert [r[11] for r in rows] == [r["t_local"] for r in voice]
    assert rows[0][11] == "2025-10-07 18:40:01"
    # every voice message came from its speaker's own slot
    assert [(r["slot"], r["verified"], "slot_steamid64" in r) for r in voice] == [
        (1, True, False),
        (2, True, False),
        (1, True, False),
    ]
    assert [r[12:] for r in rows] == [["1", "true", ""], ["2", "true", ""], ["1", "true", ""]]
    # Silero speech of each utterance (the stand-in's: loud windows), in the
    # JSON line and the transcript alike, in whole milliseconds
    assert [r["speech_ms"] for r in voice] == [3360, 1472, 992]
    assert [int(r[8]) for r in rows] == [r["speech_ms"] for r in voice]
    assert all(r[9].isdigit() and r[10].isdigit() for r in rows)
    alice = steamid64(1)
    assert rows[0][7] == f"audio/184001_{alice}_1.wav"
    with wave.open(str(session / rows[0][7])) as w:
        assert (w.getframerate(), w.getsampwidth(), w.getnframes()) == (SR, 2, 4 * SR)
    assert sorted(engine.seen) == [SR, 3 * SR // 2, 4 * SR]
    meta = json.loads((session / "meta.json").read_text())
    assert meta["asr"]["state"] == "ready" and meta["asr"]["jobs"] == 3
    assert meta["counters"]["phrases"] == 3 and meta["model"] == "parakeet"
    assert {k for k in meta["asr"] if k.endswith(("_s", "_ms"))} == {
        "load_ms",
        "audio_ms",
        "compute_ms",
    }
    assert meta["asr"]["audio_ms"] == 6500 and meta["speakers"][str(alice)]["audio_ms"] == 5000


def test_a_long_monologue_reaches_the_engine_in_pieces_marked_continued(tmp_path, engine):
    a = steamid64(1)
    plan = voice_plan([(0.0, a, tone(36.0, pauses=((27.0, 27.6),))), (40.0, a, noise(0.5))])
    timed = [(t, packet(n + 1, [(1, p)])) for n, (t, _sid, p) in enumerate(plan)]
    timed.append((42.0, packet(len(timed) + 1)))
    rec = str(tmp_path / "r.tvd")
    write_recording(rec, [(T0 + int(t * 1e9), d) for t, d in timed])
    rc, _session, lines, rows = replay(
        rec, tmp_path / "o", "--asr", "parakeet", "--no-audio", "--max-utt-ms", "30000"
    )
    assert rc == 0
    voice = sorted((r for r in lines if r["type"] == "voice"), key=lambda r: r["t_utc"])
    assert [(r["text"], r["continued"]) for r in voice] == [
        ("heard 27.38s", False),
        ("heard 8.62s", True),
        ("heard 0.50s", False),
    ]
    # the piece ends where the next begins, and each holds the frames and
    # messages of its own audio: 3 frames per message of 60 ms
    assert voice[0]["t_end_utc"] == voice[1]["t_utc"] == "2025-10-07T09:40:27.416Z"
    assert [r["details"].split(" arr ")[0].split("fr ")[1] for r in voice[:2]] == [
        "1368 plc 0 gap 0 32kb/s press 1 msg 456 -2 0%",
        "432 plc 0 gap 0 32kb/s press 0 msg 144 -2 0%",
    ]
    assert max(engine.seen) <= 30 * SR
    assert [r[7] for r in rows[1:]] == ["", "", ""]  # --no-audio: no WAV, no path
    assert not (_session / "audio").exists()


def test_what_the_engine_has_not_done_by_the_drain_is_named(tmp_path, engine, monkeypatch):
    """The engine holds every job until the exit starts, then takes 0.3 s:
    --drain-ms 0 waits for the queue no longer, the job in the engine is
    finished, and the queued two keep their WAV."""
    gate = threading.Event()
    stream = engine.open

    def held():
        gate.wait()
        return stream()

    engine.open, engine.delay = held, 0.3
    shutdown = App.shutdown
    monkeypatch.setattr(App, "shutdown", lambda app, meta: (gate.set(), shutdown(app, meta)))
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    rc, session, lines, rows = replay(rec, tmp_path / "o", "--asr", "parakeet", "--drain-ms", "0")
    assert rc == 0
    assert sorted(r["result"] for r in lines if r["type"] == "voice") == [
        "not recognized before exit"
    ] * 2 + ["text"]
    assert sorted((r[5], (session / r[7]).is_file()) for r in rows[1:]) == [
        ("not recognized before exit", True)
    ] * 2 + [("text", True)]
    assert lines[-1]["type"] == "done"


class StubScreen(Lines):
    tty, pending = False, []

    def draw(self, block):
        pass

    def stop(self, final=()):
        pass

    def key(self):
        return None


def held_until_exit(engine, monkeypatch, delay):
    """The engine holds every job until the exit starts, then takes `delay` s each."""
    gate = threading.Event()
    stream = engine.open

    def held():
        gate.wait()
        return stream()

    engine.open, engine.delay = held, delay
    shutdown = App.shutdown
    monkeypatch.setattr(App, "shutdown", lambda app, meta: (gate.set(), shutdown(app, meta)))


def voice_rows(session):
    rows = (session / "transcript.tsv").read_text().splitlines()[1:]
    return sorted((r.split("\t")[0], r.split("\t")[5]) for r in rows)


def test_a_replay_that_ends_by_itself_recognizes_every_utterance(tmp_path, engine, monkeypatch):
    """No --drain-ms: the exit waits for the whole queue, however short the
    live bound, and returns 0 with the worker stopped."""
    monkeypatch.setattr("stvwatch.app.LIVE_DRAIN_MS", 0)
    held_until_exit(engine, monkeypatch, 0.3)
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    app = App(
        parse_args(
            ["--replay", rec, "--speed", "0", "--json", "--out", str(tmp_path / "o")]
            + ["--asr", "parakeet"]
        )
    )
    assert app.run() == 0
    assert [r for _t, r in voice_rows(session_of(tmp_path / "o"))] == ["text"] * 3
    assert not app.asr.worker.is_alive()


def test_a_stop_during_the_drain_ends_it_and_finishes_the_utterance_in_the_engine(
    tmp_path, engine, monkeypatch
):
    """A replay waits for its queue until a signal comes: then the job in the
    engine is finished and the rest are named, quit stays the end of recording."""
    held_until_exit(engine, monkeypatch, 0.5)
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    app = App(
        parse_args(
            ["--replay", rec, "--speed", "0", "--json", "--out", str(tmp_path / "o")]
            + ["--asr", "parakeet"]
        )
    )
    drain = App.shutdown
    monkeypatch.setattr(
        App,
        "shutdown",
        lambda app, meta: (
            threading.Timer(0.2, app._on_signal, (signal.SIGTERM, None)).start(),
            drain(app, meta),
        ),
    )
    t0 = time.monotonic()
    assert app.run() == 0
    assert time.monotonic() - t0 < 1.4
    session = session_of(tmp_path / "o")
    assert sorted(r for _t, r in voice_rows(session)) == ["not recognized before exit"] * 2 + [
        "text"
    ]
    assert json.loads((session / "meta.json").read_text())["quit"] == "end of recording"
    assert not app.asr.worker.is_alive()


def test_sigterm_twice_as_systemd_sends_it_through_uv_exits_0_with_every_line(tmp_path):
    """systemd signals the whole cgroup and `uv run` forwards its own copy: the
    second SIGTERM must end the wait like the first, never raise out of it."""
    driver = (
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "from test_voice import Engine\n"
        "from stvwatch import cli\n"
        "from stvwatch.asr import recognizer, weights\n"
        "e = Engine(delay=1.0)\n"
        "recognizer.build = lambda *a: e\n"
        "weights.ensure = lambda *a: None\n"
        "sys.exit(cli.main(sys.argv[1:]))\n"
    )
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    argv = [
        "--replay",
        rec,
        "--speed",
        "0",
        "--json",
        "--asr",
        "parakeet",
        "--out",
        str(tmp_path / "o"),
    ]
    p = subprocess.Popen(
        [sys.executable, "-B", "-c", driver, *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert json.loads(p.stdout.readline())["type"] == "asr"
        assert json.loads(p.stdout.readline())["type"] == "play"
        time.sleep(0.5)
        t0 = time.monotonic()
        p.send_signal(signal.SIGTERM)
        time.sleep(0.2)
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert (p.returncode, err) == (0, "")
    assert time.monotonic() - t0 < 2.5
    voice = [json.loads(ln)["result"] for ln in out.splitlines() if '"voice"' in ln]
    assert sorted(voice) == ["not recognized before exit"] * 2 + ["text"]
    assert json.loads(out.splitlines()[-1])["type"] == "done"


def test_live_a_signal_waits_the_live_bound_then_the_utterance_in_the_engine(
    tmp_path, engine, monkeypatch
):
    """SIGTERM live with a backlog: the exit waits LIVE_DRAIN_MS for the
    queue, finishes the job in the engine and names the rest."""
    monkeypatch.setattr("stvwatch.app.LIVE_DRAIN_MS", 300)
    engine.delay = 1.0
    app = App(
        parse_args(
            ["--relay", "127.0.0.1:9", "--json", "--out", str(tmp_path)] + ["--asr", "parakeet"]
        )
    )
    app.pacer, app.screen = Pacer(False), StubScreen()
    app.asr = recognizer.Recognizer("parakeet", 2, "", 0, lambda: 0, app.q_out, wait=False)
    sid = steamid64(1)
    ch = app.channels[sid] = Channel(sid, T0)
    ca = app.audio[sid] = ChannelAudio()
    for k in range(4):
        ca.open, ca.start_ns, ca.index = True, T0 + k * 10 * 10**9, k + 1
        app.speech_start(ch, ca, ca.start_ns)
        app.add_pcm(sid, ca, np.full(SR, 0.3, np.float32), ca.start_ns)
        app.finish_utterance(sid, ca.start_ns + 10**9, "clock")
    app._on_signal(signal.SIGTERM, None)
    t0 = time.monotonic()
    app.shutdown({})
    assert time.monotonic() - t0 < 1.8
    assert [r for _t, r in voice_rows(next(tmp_path.iterdir()))] == ["text"] + [
        "not recognized before exit"
    ] * 3
    assert len(engine.seen) == 1 and not app.asr.worker.is_alive()


def test_the_exit_waits_for_the_recognizer_by_how_the_run_ends(tmp_path):
    """Unbounded only for a replay that ended by itself; --drain-ms bounds all."""

    def end(*argv, stops=0):
        app = App(parse_args([*argv, "--out", str(tmp_path / str(len(list(tmp_path.iterdir()))))]))
        app.stops = stops
        return app.drain_end(100.0)

    assert end("--replay", "x.tvd") is None
    assert end("--replay", "x.tvd", stops=1) == 120.0
    assert end("--relay", "127.0.0.1:9") == 120.0
    assert end("--replay", "x.tvd", "--drain-ms", "5000") == 105.0
    assert end("--relay", "127.0.0.1:9", "--drain-ms", "0") == 100.0


def test_a_stop_while_the_model_loads_waits_for_the_load(tmp_path, monkeypatch):
    """An exit leaves no thread inside the engine, the loading one included."""
    e = Engine()

    def slow(name, threads, models_dir, min_ms):
        time.sleep(0.5)
        return e

    monkeypatch.setattr(recognizer, "build", slow)
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    app = App(
        parse_args(
            ["--replay", rec, "--speed", "0", "--json", "--out", str(tmp_path / "o")]
            + ["--asr", "parakeet", "--drain-ms", "0"]
        )
    )
    threading.Timer(0.1, app._on_signal, (signal.SIGTERM, None)).start()
    assert app.run() == 0
    assert not app.asr.loader.is_alive() and not app.asr.worker.is_alive()


def test_a_replay_waits_for_the_recognizer_once_its_queue_is_full(tmp_path, engine, monkeypatch):
    """Bound 2 s of queued audio: the 4 s utterance goes in alone, each later
    one waits until it fits."""
    monkeypatch.setattr(recognizer, "MAX_QUEUED_S", 2.0)
    engine.delay = 0.2
    depth = []
    submit = recognizer.Recognizer.utterance

    def watched(self, *job):
        took = submit(self, *job)
        depth.append(self.queue_depth()[1])
        return took

    monkeypatch.setattr(recognizer.Recognizer, "utterance", watched)
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    _rc, _s, lines, _rows = replay(rec, tmp_path / "o", "--asr", "parakeet")
    assert sorted(r["text"] for r in lines if r["type"] == "voice") == [
        "heard 1.00s",
        "heard 1.50s",
        "heard 4.00s",
    ]
    assert max(depth) <= 4.0


def test_live_audio_over_the_queue_bound_is_named_not_waited_for(tmp_path, engine, monkeypatch):
    """Live input never waits for the recognizer: past the bound an utterance
    is not queued, its line says so and its WAV stays. The engine is held 1 s."""
    monkeypatch.setattr(recognizer, "MAX_QUEUED_S", 2.0)
    gate = threading.Event()
    threading.Timer(1.0, gate.set).start()
    engine.open = lambda stream=engine.open: (gate.wait(), stream())[1]
    sid = steamid64(1)
    app = bare_app(tmp_path)
    app.asr = recognizer.Recognizer("parakeet", 2, "", 0, lambda: 0, app.q_out, wait=False)
    while app.asr.state == "loading":
        time.sleep(0.01)
    ch = app.channels[sid] = Channel(sid, T0)
    ca = app.audio[sid] = ChannelAudio()
    t0 = time.monotonic()
    for k, seconds in enumerate((1.5, 1.0)):
        ca.open, ca.start_ns, ca.index = True, T0 + k * 10 * 10**9, k + 1
        app.speech_start(ch, ca, ca.start_ns)
        app.add_pcm(sid, ca, np.full(int(seconds * SR), 0.3, np.float32), ca.start_ns)
        app.finish_utterance(sid, ca.start_ns + int(seconds * 10**9), "clock")
    assert time.monotonic() - t0 < 0.5
    app.asr.close()
    rows = (next(tmp_path.iterdir()) / "transcript.tsv").read_text().splitlines()[1:]
    assert [(r.split("\t")[5], r.split("\t")[7] != "") for r in rows] == [
        ("not recognized, queue full", True)
    ]


def test_close_drops_the_queued_jobs_instead_of_recognizing_them(engine):
    engine.delay = 0.1
    out = recognizer.queue.Queue()
    asr = recognizer.Recognizer("parakeet", 2, "", 0, lambda: 0, out)
    while asr.state == "loading":
        time.sleep(0.01)
    for k in range(5):
        asr.utterance(k, np.full(SR, 0.3, np.float32), {})
    asr.close()
    time.sleep(0.6)
    assert len(engine.seen) <= 1 and asr.queue_depth() == (0, 0.0)


def test_an_utterance_the_engine_fails_on_is_named_and_not_counted_as_no_speech(tmp_path, engine):
    engine.fail = lambda n: n == 3 * SR // 2
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    rc, session, lines, rows = replay(rec, tmp_path / "o", "--asr", "parakeet")
    assert rc == 0
    voice = sorted((r for r in lines if r["type"] == "voice"), key=lambda r: r["t_utc"])
    assert [(r["nick"], r["result"], r["text"]) for r in voice] == [
        ("alice", "text", "heard 4.00s"),
        ("bob", "recognition failed", ""),
        ("alice", "text", "heard 1.00s"),
    ]
    assert sorted(r[5] for r in rows[1:]) == sorted(r["result"] for r in voice)
    assert [r["text"] for r in lines if r["type"] == "asr"][-1].endswith(
        ": RuntimeError: bad_alloc"
    )
    meta = json.loads((session / "meta.json").read_text())
    assert (meta["counters"]["phrases"], meta["counters"]["nospeech"]) == (2, 0)


def test_an_engine_that_fails_to_load_ends_the_run_before_reading(tmp_path, monkeypatch):
    def broken(name, threads, models_dir, min_ms):
        raise RuntimeError("no such model")

    monkeypatch.setattr(recognizer, "build", broken)
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    rc, _s, lines, _rows = replay(rec, tmp_path / "o", "--asr", "parakeet")
    assert rc == 2
    assert [(r["type"], r["text"]) for r in lines] == [
        ("asr", "parakeet failed: RuntimeError: no such model"),
        ("done", lines[-1]["text"]),
    ]
    assert lines[-1]["text"].startswith("asr failed -> ")


def test_weights_are_fetched_before_the_run_and_a_failure_exits_2(tmp_path, monkeypatch, capsys):
    calls = []

    def ensure(pin, models_dir):
        calls.append((pin.engine, models_dir))
        if pin.engine == "silero-vad":
            raise weights.WeightsError("silero_vad.onnx: sha256 x, want y")

    monkeypatch.setattr(weights, "ensure", ensure)
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    argv = ["--replay", rec, "--out", str(tmp_path / "o"), "--asr", "parakeet"]
    assert cli.main(argv + ["--models-dir", str(tmp_path / "m")]) == 2
    assert calls == [("parakeet", str(tmp_path / "m")), ("silero-vad", str(tmp_path / "m"))]
    assert "parakeet weights not available: silero_vad.onnx: sha256" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_the_recognizer_loads_while_nothing_is_read(tmp_path, monkeypatch):
    """The load holds the GIL for seconds: reading starts only after it."""
    started = threading.Event()
    e = Engine()

    def slow(name, threads, models_dir, min_ms):
        started.set()
        time.sleep(0.5)
        return e

    monkeypatch.setattr(recognizer, "build", slow)
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    _rc, _s, lines, _rows = replay(rec, tmp_path / "o", "--asr", "parakeet")
    assert started.is_set()
    assert [r["type"] for r in lines[:2]] == ["asr", "play"]


def forged_recording(path):
    """alice and bob in slots 1 and 2. alice talks from her slot; alice's
    SteamID comes from her slot, then from bob's (a forged payload); carol
    talks from slot 5 before and after the server puts her there; dave
    talks from slot 6, empty all along. Then the map changes: new tables,
    bob alone in slot 1, and he talks."""
    names = ("alice", "bob", "carol", "dave")
    sid = {k: steamid64(n) for n, k in enumerate(names, 1)}

    def who(name, slot, userid):
        acc = account(names.index(name) + 1)
        return (name, f"[U:1:{acc}]", acc, userid, slot)

    def table(seq, *players, create=False):
        return netchan.build_packet(
            seq, 1, 0x11223344, 0, unreliable=table_update(*players, create=create)
        )

    plan = [(0, lambda s: table(s, who("alice", 1, 11), who("bob", 2, 12), create=True))]
    talks = [
        (100, "alice", [1] * 6),
        (2000, "alice", [1, 1, 1, 2, 2, 2]),
        (4000, "carol", [5] * 6),
        (6000, "dave", [6] * 4),
    ]
    for t0, name, slots in talks:
        for k, slot in enumerate(slots):
            p = voice_payload(sid[name], 3 * k)
            plan.append((t0 + 60 * k, lambda s, slot=slot, p=p: packet(s, [(slot, p)])))
    plan.append((4150, lambda s: table(s, who("carol", 5, 13))))

    def new_map(seq):
        slots = [(i, str(i).encode(), None) for i in range(16)]
        slots[1] = userinfo_entries([who("bob", 1, 12)])[0]
        w = wire.BitWriter()
        server_info(w)
        write_create(w, [(0, b"x", None)], name="downloadables")
        write_create(w, slots)
        return netchan.build_packet(seq, 1, 0x11223344, 0, unreliable=w.get_bytes())

    plan.append((8000, new_map))
    for k in range(6):
        p = voice_payload(sid["bob"], 3 * k)
        plan.append((8100 + 60 * k, lambda s, p=p: packet(s, [(1, p)])))
    plan += [(ms, lambda s: packet(s)) for ms in range(10, 10000, 20)]
    plan.sort(key=lambda e: e[0])
    write_recording(path, [(T0 + ms * 1_000_000, f(n + 1)) for n, (ms, f) in enumerate(plan)])
    return sid


def test_voice_lines_say_whether_the_slot_is_the_speakers(tmp_path):
    """The SteamID in a voice payload is what the sending client wrote; the
    slot it came from is the server's. verified: true when the server had
    that player in the slot, false when any message came from someone
    else's slot (slot_steamid64 names him, the screen marks it in red), null
    when no message could be checked."""
    rec = str(tmp_path / "r.tvd")
    sid = forged_recording(rec)
    rc, _session, lines, rows = replay(rec, tmp_path / "o")
    assert rc == 0
    voice = sorted((r for r in lines if r["type"] == "voice"), key=lambda r: r["t_utc"])
    assert [(r["steamid64"], r["slot"], r["verified"], r.get("slot_steamid64")) for r in voice] == [
        (sid["alice"], 1, True, None),
        (sid["alice"], 2, False, sid["bob"]),
        (sid["carol"], 5, True, None),
        (sid["dave"], 6, None, None),
        (sid["bob"], 1, True, None),
    ]
    rows = sorted(rows[1:])
    assert [r[11:] for r in rows] == [
        ["1", "true", ""],
        ["2", "false", str(sid["bob"])],
        ["5", "true", ""],
        ["6", "", ""],
        ["1", "true", ""],
    ]
    cmd = [sys.executable, "-B", "-m", "stvwatch.cli", "--replay", rec, "--speed", "0"]
    cmd += ["--monitor", "--status-every-ms", "0", "--out", str(tmp_path / "m")]
    for debug in ([], ["--debug"]):
        out = subprocess.run(
            cmd + debug, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60, check=True
        ).stdout.decode()
        shown = [ln[13:] for ln in out.splitlines() if " voice " in ln]
        assert [ln.split(": asr off")[0] for ln in shown] == [
            "voice alice",
            "voice alice [slot 2: bob]",
            "voice carol",
            f"voice {steam2(account(4))}",
            "voice bob",
        ]
        assert [
            ln.endswith(f" slot {n}") for ln, n in zip(shown, (1, 2, 5, 6, 1), strict=True)
        ] == [bool(debug)] * 5
    painted = App(parse_args(["--replay", rec, "--out", str(tmp_path / "p")]))
    utt = Utt(("k", 1), sid["alice"], 0)
    utt.state, utt.slot, utt.verified = "recognizing", 2, False
    utt.slot_owner = Player(account(2), "bob", 12)
    assert painted.slot_mark(utt) == [(" [slot 2: bob]", "red")]


class Live:
    """Stand-in live recognizer: the calls the app makes, by key."""

    def __init__(self):
        self.calls, self.state = [], "ready"

    def open(self, key):
        self.calls.append(("open", key))

    def push(self, key, pcm):
        self.calls.append(("push", key, len(pcm)))

    def close(self, key):
        self.calls.append(("close", key))


def test_live_text_follows_the_talking_line_and_the_final_replaces_it(tmp_path):
    """Every piece of a monologue is opened, fed and closed in the live
    recognizer; what follows a cut is fed from the cut on, never the rest
    before it. Its partial text ends the talking line; the final line has
    the --asr text only."""
    app = bare_app(tmp_path, "--max-utt-ms", "10000", "--asr", "parakeet")
    app.asr = Pieces(True)
    app.live_asr = Live()
    sid = steamid64(1)
    speak(app, 14, ((9.0, 0.5),))
    k1, k2 = (sid, 1), (sid, 2)
    assert [c for c in app.live_asr.calls if c[0] != "push"] == [
        ("open", k1),
        ("close", k1),
        ("open", k2),
        ("close", k2),
    ]
    fed = {
        k: sum(c[2] for c in app.live_asr.calls if c[0] == "push" and c[1] == k) / SR
        for k in (k1, k2)
    }
    assert app.asr.lengths[0] < fed[k1] == 10.02 and fed[k1] + fed[k2] == 14.0
    assert fed[k2] < app.asr.lengths[1]  # the rest before the cut is not fed again
    utt = app.utts[k2]
    utt.state, app.now = "talking", utt.start_ns + 2 * 10**9
    app.handle_result(("partial", k2, "they took\x1b[2J the flag"))
    app.handle_result(("partial", (sid, 9), "no such utterance"))
    line = app.progress(utt)
    assert line[-2:] == [(": ", ""), ("they took?[2J the flag", "partial")]
    assert text_of(line).endswith("(cont) talking 2.0s: they took?[2J the flag")
    utt.state = "recognizing"
    app.handle_result(("partial", k2, "late"))
    assert utt.partial == "they took?[2J the flag" and "flag" not in text_of(app.progress(utt))
    app.finalize(utt, "they took the flag", {})
    assert text_of(app.screen.closed[-1]).endswith("(cont): they took the flag  4.7s")


@pytest.mark.parametrize("debug", [False, True])
def test_the_live_recognizer_ends_the_asr_line_of_the_block(tmp_path, debug):
    app = bare_app(
        tmp_path, "--asr", "parakeet", "--live-asr", "nemotron", *(["--debug"] if debug else [])
    )
    stats = SimpleNamespace(
        summary=lambda now: {"lag_last": 0.4, "lag_p90": 0.4, "jobs_min": 1, "rtf": 0.5}
    )
    app.asr = SimpleNamespace(queue_depth=lambda: (0, 0.0), stats=stats, state="ready")
    app.live_asr = SimpleNamespace(state="loading", stats=stats, dropped_s=0.0, drops=0)
    assert text_of(app.block()[4]).endswith("  live nemotron loading")
    app.live_asr.state, app.live_asr.dropped_s, app.live_asr.drops = "ready", 3.5, 2
    tail = app.block()[4][-3:] if debug else app.block()[4][-2:]
    if debug:
        assert tail == [("ready", "green"), (" lag 0.4s", ""), (" RTF 0.50 dropped 3.5s", "yellow")]
    else:
        assert tail == [("ready", "green"), (" lag 0.4s", "")]


class LiveEngine:
    """Stand-in streaming engine: each partial names the audio heard so far."""

    def __init__(self):
        self.heard = 0

    def open(self):
        return {"n": 0}

    def push_all(self, pairs):
        for st, pcm in pairs:
            st["n"] += len(pcm)
            self.heard += len(pcm)
        return [(st, [("partial", f"said {st['n'] / SR:.2f}s")]) for st, _pcm in pairs]


def test_live_asr_changes_no_session_file_and_needs_a_screen(tmp_path, engine, monkeypatch):
    """With --serve (a screen) the live recognizer hears every utterance and
    the voice lines, transcript and WAVs are those of a run without it; with
    --json it is not loaded and its weights are not fetched; without --asr
    it is a usage error."""
    from stvwatch import app as appmod

    live = LiveEngine()
    built = []
    monkeypatch.setattr(appmod, "build_live", lambda *a: built.append(a) or live)
    fetched = []
    monkeypatch.setattr(weights, "ensure", lambda pin, d: fetched.append(pin.engine))
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    runs = {}
    for name, extra in (("off", []), ("on", ["--live-asr", "nemotron"])):
        out = tmp_path / name
        with tempfile.TemporaryDirectory(prefix="sw", dir="/tmp") as d:
            argv = ["--replay", rec, "--speed", "0", "--out", str(out), "--serve", d + "/s"]
            assert cli.main(argv + ["--asr", "parakeet", *extra]) == 0
        s = session_of(out)
        voice = [json.loads(ln) for ln in (s / "events.jsonl").read_text().splitlines()]
        voice = [(r["t_utc"], r["text"], r["result"]) for r in voice if r["type"] == "voice"]
        rows = [r.split("\t")[:8] for r in (s / "transcript.tsv").read_text().splitlines()]
        wavs = {p.name: p.read_bytes() for p in (s / "audio").iterdir()}
        runs[name] = (voice, rows, wavs, json.loads((s / "meta.json").read_text()))
    assert runs["on"][:3] == runs["off"][:3] and len(runs["on"][0]) == 3
    meta = runs["on"][3]
    assert "live_asr" not in runs["off"][3] and meta["live_asr"]["state"] == "ready"
    assert meta["live_asr"]["audio_ms"] == live.heard * 1000 // SR == 6500
    assert built == [("nemotron", 1, meta["args"]["models_dir"])]
    assert fetched == ["parakeet", "silero-vad"] * 2 + ["nemotron"]
    fetched.clear()
    argv = ["--replay", rec, "--speed", "0", "--json", "--out", str(tmp_path / "json")]
    assert cli.main(argv + ["--asr", "parakeet", "--live-asr", "nemotron"]) == 0
    lines = (session_of(tmp_path / "json") / "events.jsonl").read_text()
    assert '"nemotron live off: partial text shows on a screen only"' in lines
    assert fetched == ["parakeet", "silero-vad"] and len(built) == 1
    for bad in (["--live-asr", "nemotron"], ["--asr", "parakeet", "--live-asr-threads", "0"]):
        with pytest.raises(SystemExit):
            parse_args(["--replay", rec, *bad])
