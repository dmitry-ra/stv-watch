"""Voice in the viewer: utterances from the segmenter, monologue pieces, the
live and final lines, the recognizer behind them, transcript.tsv and WAVs.
The recognizer runs a stand-in engine here; the real one is in test_parakeet."""

import json
import threading
import time
import wave

import numpy as np
import pytest
from helpers import T0, account, packet, steamid64, write_recording
from voicegen import demo, noise, tone, voice_plan
from voicegen import payload as voice_payload

from stvwatch import cli
from stvwatch.app import SR, TSV_HEAD, App, ChannelAudio, Utt
from stvwatch.asr import recognizer, weights
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


class Engine:
    """Stand-in for an utterance engine: the text names the clip's length."""

    def __init__(self, delay=0.0, text=None):
        self.delay, self.text, self.seen = delay, text, []

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
                return [("final", text)] if text else []

        return Stream()


@pytest.fixture
def engine(monkeypatch):
    e = Engine()
    monkeypatch.setattr(recognizer, "build", lambda name, threads, models_dir: e)
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


@pytest.mark.parametrize(
    "seconds,pauses,cuts",
    [
        # 26 s: the first pause past 25 s, not the later one at 28; 52 s likewise;
        # 67 s: at the 30 s limit, the last pause past 10 s of the piece; then a
        # hard cut, no pause in reach
        (
            100,
            (15.0, 26.0, 28.0, 52.0, 67.0),
            [(26.1, "pause"), (26.0, "pause"), (15.0, "pause"), (30.0, "max_len"), (2.9, None)],
        ),
        (70, (), [(30.0, "max_len"), (30.0, "max_len"), (10.0, None)]),
    ],
)
def test_a_monologue_is_cut_at_a_pause_and_hard_only_without_one(tmp_path, seconds, pauses, cuts):
    """--max-utt 30: from 25 s the first pause ends the piece; at 30 s the last
    pause past 10 s does; with no pause the cut is hard at 30 s. Pieces after
    the first are marked as continuations."""
    sid = steamid64(1)
    app = bare_app(tmp_path)
    ch = app.channels[sid] = Channel(sid, T0)
    ca = app.audio[sid] = ChannelAudio()
    ca.open, ca.start_ns, ca.index = True, T0, 1
    app.speech_start(ch, ca, T0)
    step = int(0.02 * SR)
    sound = (0.3 * np.sin(np.arange(step) * 0.3)).astype(np.float32)
    for k in range(seconds * 50):
        t = k / 50
        quiet = any(p <= t < p + 0.5 for p in pauses)
        app.add_pcm(sid, ca, np.zeros(step, np.float32) if quiet else sound, T0 + int(t * 1e9))
    app.finish_utterance(sid, T0 + seconds * 10**9, "end")
    got = [text_of(c) for c in app.screen.closed]
    secs = [float(g.rsplit("  ", 1)[1].rstrip("s")) for g in got]
    assert secs == [c for c, _w in cuts]
    assert [" (cont)" in g for g in got] == [False] + [True] * (len(cuts) - 1)
    log = (next(tmp_path.iterdir()) / "feed.log").read_text()
    assert [w for _c, w in cuts] == [ln.split(" ")[-2] for ln in log.splitlines()][:-1] + [None]


@pytest.mark.parametrize("debug", [False, True])
def test_recognition_lag_turns_yellow_past_2s_and_red_past_5s(tmp_path, debug):
    from types import SimpleNamespace

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
    assert [r[10] for r in rows] == [r["t_local"] for r in voice]
    assert rows[0][10] == "2025-10-07 18:40:01"
    alice = steamid64(1)
    assert rows[0][7] == f"audio/184001_{alice}_1.wav"
    with wave.open(str(session / rows[0][7])) as w:
        assert (w.getframerate(), w.getsampwidth(), w.getnframes()) == (SR, 2, 4 * SR)
    assert sorted(engine.seen) == [SR, 3 * SR // 2, 4 * SR]
    meta = json.loads((session / "meta.json").read_text())
    assert meta["asr"]["state"] == "ready" and meta["asr"]["jobs"] == 3
    assert meta["counters"]["phrases"] == 3 and meta["model"] == "parakeet"


def test_a_long_monologue_reaches_the_engine_in_pieces_marked_continued(tmp_path, engine):
    a = steamid64(1)
    plan = voice_plan([(0.0, a, tone(36.0, pauses=((27.0, 27.6),))), (40.0, a, noise(0.5))])
    timed = [(t, packet(n + 1, [(1, p)])) for n, (t, _sid, p) in enumerate(plan)]
    timed.append((42.0, packet(len(timed) + 1)))
    rec = str(tmp_path / "r.tvd")
    write_recording(rec, [(T0 + int(t * 1e9), d) for t, d in timed])
    rc, _session, lines, rows = replay(rec, tmp_path / "o", "--asr", "parakeet", "--no-audio")
    assert rc == 0
    voice = sorted((r for r in lines if r["type"] == "voice"), key=lambda r: r["t_utc"])
    assert [(r["text"], r["continued"]) for r in voice] == [
        ("heard 27.16s", False),
        ("heard 8.84s", True),
        ("heard 0.50s", False),
    ]
    assert max(engine.seen) <= 30 * SR
    assert [r[7] for r in rows[1:]] == ["", "", ""]  # --no-audio: no WAV, no path
    assert not (_session / "audio").exists()


def test_what_the_engine_has_not_done_by_the_drain_is_named(tmp_path, engine):
    engine.delay = 0.5
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    rc, _s, lines, rows = replay(rec, tmp_path / "o", "--asr", "parakeet", "--drain", "0")
    assert rc == 0
    results = sorted(r["result"] for r in lines if r["type"] == "voice")
    assert "not recognized before exit" in results and len(results) == 3
    assert sorted(r[5] for r in rows[1:]) == results
    assert lines[-1]["type"] == "done"


def test_an_engine_that_fails_to_load_ends_the_run_before_reading(tmp_path, monkeypatch):
    def broken(name, threads, models_dir):
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

    def slow(name, threads, models_dir):
        started.set()
        time.sleep(0.5)
        return e

    monkeypatch.setattr(recognizer, "build", slow)
    rec = str(tmp_path / "demo.tvd")
    demo(rec)
    _rc, _s, lines, _rows = replay(rec, tmp_path / "o", "--asr", "parakeet")
    assert started.is_set()
    assert [r["type"] for r in lines[:2]] == ["asr", "play"]
