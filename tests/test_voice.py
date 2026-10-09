"""Voice in the viewer: utterances from the segmenter, monologue pieces, the
live and final lines, the recognizer behind them, transcript.tsv and WAVs.
The recognizer runs a stand-in engine here; the real one is in test_parakeet."""

import json
import threading
import time
import wave
from types import SimpleNamespace

import numpy as np
import pytest
from helpers import T0, account, packet, steamid64, write_recording
from voicegen import demo, noise, tone, voice_plan
from voicegen import payload as voice_payload

from stvwatch import cli
from stvwatch.app import SR, TSV_HEAD, App, ChannelAudio, Utt
from stvwatch.asr import recognizer, vad, weights
from stvwatch.cli import parse_args
from stvwatch.model import Channel
from stvwatch.render import rows_for
from stvwatch.source import Pacer


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


def monologue(tmp_path, seconds, pauses, with_vad=True, *extra):
    """A tone of `seconds` with silent (start, length) pauses, fed in 20 ms
    chunks; -> (piece lengths s, why each piece ended, continued marks)."""
    sid = steamid64(1)
    app = bare_app(tmp_path, *extra)
    app.asr = Pieces(with_vad)
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
    whys = [u.details.split(" ")[-2] for u in app.utts.values()]
    conts = [u.cont for u in app.utts.values()]
    return app.asr.lengths, whys, conts


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
    assert rows[0] == list(TSV_HEAD) + ["t_local"]
    rows = sorted(rows[1:])
    assert [(r[3], r[4], r[5], r[6]) for r in rows] == [
        ("alice", "parakeet", "text", "heard 4.00s"),
        ("bob", "parakeet", "text", "heard 1.50s"),
        ("alice", "parakeet", "text", "heard 1.00s"),
    ]
    assert [r[0] for r in rows] == [r["t_utc"] for r in voice]
    assert [r[11] for r in rows] == [r["t_local"] for r in voice]
    assert rows[0][11] == "2025-10-07 18:40:01"
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
    assert max(engine.seen) <= 30 * SR
    assert [r[7] for r in rows[1:]] == ["", "", ""]  # --no-audio: no WAV, no path
    assert not (_session / "audio").exists()


def test_what_the_engine_has_not_done_by_the_drain_is_named(tmp_path, engine):
    engine.delay = 0.5
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    rc, _s, lines, rows = replay(rec, tmp_path / "o", "--asr", "parakeet", "--drain-ms", "0")
    assert rc == 0
    results = sorted(r["result"] for r in lines if r["type"] == "voice")
    assert "not recognized before exit" in results and len(results) == 3
    assert sorted(r[5] for r in rows[1:]) == results
    assert lines[-1]["type"] == "done"


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
