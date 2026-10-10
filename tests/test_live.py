"""The live recognizer's thread (asr.live): partials by key, a queue the main
loop never waits on, the drop when it falls behind, its priority, its end.
The engine is a stand-in whose calls the test holds and releases."""

import queue
import threading
import time

import numpy as np
import pytest

from stvwatch.asr import live

SR = live.SR


class Engine:
    """Each stream's partial names the samples it has had so far. A call
    waits while `gate` is clear, so the test decides what queues meanwhile."""

    def __init__(self):
        self.gate = threading.Event()
        self.gate.set()
        self.calls = []

    def open(self):
        return {"n": 0}

    def push_all(self, pairs):
        self.calls.append([len(pcm) for _st, pcm in pairs])
        assert self.gate.wait(5)
        for st, pcm in pairs:
            st["n"] += len(pcm)
        return [(st, [("partial", f"{st['n']}")]) for st, _pcm in pairs]


def pcm(seconds):
    return np.zeros(int(seconds * SR), np.float32)


def started(engine, **kw):
    out = queue.Queue()
    r = live.LiveRecognizer(lambda: engine, out, **kw)
    assert out.get(timeout=5) == ("live_loaded", "ready", "", r.load_s)
    return r, out


def until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.001)


def take(out, n):
    return [out.get(timeout=5) for _ in range(n)]


def test_partials_by_key_and_one_engine_call_for_all_that_queued():
    e = Engine()
    r, out = started(e)
    r.open("a")
    r.push("a", pcm(0.1))
    assert take(out, 1) == [("partial", "a", "1600")]
    e.gate.clear()
    r.push("a", pcm(0.1))  # the call that holds the worker
    until(lambda: len(e.calls) == 2)
    r.open("b")
    r.push("b", pcm(0.2))
    r.open("c")
    r.push("c", pcm(0.1))
    r.close("c")  # closed before its audio was read: never fed
    r.push("a", pcm(0.1))
    r.push("a", pcm(0.1))
    r.push("b", pcm(0.2))
    e.gate.set()
    assert take(out, 3) == [
        ("partial", "a", "3200"),
        ("partial", "b", "6400"),
        ("partial", "a", "6400"),
    ]
    assert e.calls[2] == [6400, 3200]
    r.close("a")
    r.push("a", pcm(0.1))  # after its close: not fed
    r.push("b", pcm(0.1))
    assert take(out, 1) == [("partial", "b", "8000")] and e.calls[3] == [1600]
    r.stop()
    assert not r.thread.is_alive() and out.empty()
    assert r.stats.total_audio == pytest.approx(0.1 * 9) and r.drops == 0


def test_falling_behind_drops_the_queue_and_the_open_utterances_partials():
    e = Engine()
    r, out = started(e, max_lag_s=0.2)
    r.open("a")
    e.gate.clear()
    r.push("a", pcm(0.1))
    until(lambda: e.calls)
    r.push("a", pcm(0.5))
    r.open("b")  # opened in what is dropped: starts without its beginning, so never
    r.push("b", pcm(0.25))
    time.sleep(0.3)
    r.push("b", pcm(0.01))  # the oldest audio decides: it goes, this one stays
    e.gate.set()
    assert take(out, 1) == [("partial", "a", "1600")]
    until(lambda: r.drops)
    assert r.dropped_s == pytest.approx(0.75)
    r.push("a", pcm(0.1))
    r.push("b", pcm(0.1))
    r.open("c")
    r.push("c", pcm(0.1))
    assert take(out, 1) == [("partial", "c", "1600")] and e.calls[1:] == [[1600]]
    assert r.stats.summary(time.monotonic_ns())["lag_last"] < 0.2
    r.stop()
    assert out.empty()


def test_the_main_loop_never_waits_on_the_engine():
    e = Engine()
    r, out = started(e)
    r.open("a")
    e.gate.clear()
    r.push("a", pcm(0.02))
    until(lambda: e.calls)
    took = []

    def main_loop():
        for i in range(200):
            t0 = time.monotonic()
            r.push("a", pcm(0.02))
            if i % 50 == 0:
                r.open(("x", i))
                r.close(("x", i))
            took.append(time.monotonic() - t0)

    t = threading.Thread(target=main_loop, daemon=True)
    t.start()
    t.join(2)
    assert not t.is_alive() and max(took) < 0.05
    e.gate.set()
    r.stop()


def nice_of(tid):
    with open(f"/proc/self/task/{tid}/stat") as f:
        return int(f.read().rsplit(")", 1)[1].split()[16])


def test_the_worker_and_threads_its_engine_starts_run_niced_and_the_main_one_not():
    base = nice_of(threading.get_native_id())
    seen = {}

    def load():
        t = threading.Thread(target=lambda: seen.update(pool=nice_of(threading.get_native_id())))
        t.start()
        t.join()
        return Engine()

    out = queue.Queue()
    r = live.LiveRecognizer(load, out)
    assert out.get(timeout=5)[1] == "ready"
    want = min(19, base + live.NICE)
    assert nice_of(r.tid) == seen["pool"] == want != base
    assert nice_of(threading.get_native_id()) == base
    r.stop()


def test_stop_while_loading_and_a_failed_load():
    go = threading.Event()
    out = queue.Queue()

    def load():
        assert go.wait(5)
        return Engine()

    r = live.LiveRecognizer(load, out)
    r.open("a")
    threading.Timer(0.1, go.set).start()
    r.stop()
    assert not r.thread.is_alive() and out.get_nowait()[1] == "ready"
    r.push("a", pcm(0.1))
    assert r.ops == [("open", "a")]  # stopped: nothing more is queued

    def broken():
        raise OSError("no weights")

    r = live.LiveRecognizer(broken, out)
    assert out.get(timeout=5)[:3] == ("live_loaded", "failed", "OSError: no weights")
    r.thread.join(5)
    r.open("a")
    r.push("a", pcm(0.1))
    assert r.ops == []
    r.stop()


def test_an_engine_error_is_reported_and_ends_the_open_utterances_partials():
    e = Engine()
    r, out = started(e)
    r.open("a")
    r.push("a", pcm(0.1))
    take(out, 1)
    e.push_all = lambda pairs: 1 / 0
    r.push("a", pcm(0.1))
    assert take(out, 1) == [("live_error", "ZeroDivisionError: division by zero")]
    del e.push_all
    r.push("a", pcm(0.1))
    r.open("b")
    r.push("b", pcm(0.1))
    assert take(out, 1) == [("partial", "b", "1600")]
    r.stop()


def test_audio_queued_behind_a_stuck_engine_stays_bounded():
    """The engine does not return: what queues behind it is dropped by age
    when it is queued, not when the engine comes back."""
    e = Engine()
    r, out = started(e, max_lag_s=0.2)
    r.open("a")
    e.gate.clear()
    r.push("a", pcm(0.02))
    until(lambda: e.calls)
    for _ in range(100):
        r.push("a", pcm(0.02))
        time.sleep(0.01)
    queued = sum(len(op[2]) for op in r.ops if op[0] == "push") / SR
    assert queued < 0.5 and r.drops >= 3
    e.gate.set()
    r.stop()


def test_a_failed_load_lets_go_of_what_queued_while_it_loaded():
    go = threading.Event()

    def broken():
        assert go.wait(5)
        raise OSError("no weights")

    out = queue.Queue()
    r = live.LiveRecognizer(broken, out)
    r.open("a")
    r.push("a", pcm(0.1))
    go.set()
    assert out.get(timeout=5)[1] == "failed"
    r.thread.join(5)
    assert r.ops == []


def test_an_engine_that_fails_to_open_a_stream_is_reported_and_the_worker_goes_on():
    e = Engine()
    r, out = started(e)
    e.open = lambda: 1 / 0
    r.open("a")
    r.push("a", pcm(0.1))
    assert take(out, 1) == [("live_error", "ZeroDivisionError: division by zero")]
    del e.open
    r.open("b")
    r.push("b", pcm(0.1))
    assert take(out, 1) == [("partial", "b", "1600")]
    r.stop()
