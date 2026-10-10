"""Live recognition: partial text of the utterances being spoken, next to the
recognizer of closed utterances (recognizer.py) and never in its way.

The main loop only queues: open(key), push(key, pcm) and close(key) append
under a lock and never wait for the engine. One worker thread takes all that
is queued at once and feeds every open utterance its new audio in one engine
call (engine.push_all); partials go to `out` as ("partial", key, text).

Falling behind: when the oldest queued audio has waited longer than
max_lag_s, all of it is dropped and the utterances open at that moment get no
more partials; utterances opened later start afresh. The age is checked on
every push as well as by the worker, so audio cannot pile up behind an engine
call that does not return. An engine error ends the open utterances' partials
the same way and is reported; the worker goes on. The worker runs at the
process's priority plus `nice` and loads the engine itself, so the threads of
the engine's runtime inherit that priority too.
"""

import os
import threading
import time

import numpy as np

from .recognizer import Stats

SR = 16000
MAX_LAG_S = 1.5  # two chunks of the 560 ms model
NICE = 10


class LiveRecognizer:
    def __init__(self, load, out, nice=NICE, max_lag_s=MAX_LAG_S):
        self.load = load  # () -> a loaded engine with open() and push_all()
        self.out = out
        self.nice, self.max_lag_s = nice, max_lag_s
        self.state = "loading"  # loading/ready/failed
        self.error = ""
        self.load_s = 0.0
        self.tid = None
        self.stats = Stats()
        self.dropped_s = 0.0
        self.drops = 0
        self.ops = []
        self.reset = False  # audio was dropped: the worker's streams are stale
        self.cv = threading.Condition()
        self.stopping = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="asr-live")
        self.thread.start()

    # ---- main loop
    def _put(self, op):
        with self.cv:
            if self.stopping or self.state == "failed":
                return
            if op[0] == "push":
                self._drop_stale(op[3])
            self.ops.append(op)
            self.cv.notify()

    def open(self, key):
        self._put(("open", key))

    def push(self, key, pcm):
        self._put(("push", key, pcm, time.monotonic()))

    def close(self, key):
        self._put(("close", key))

    def stop(self):
        """End the worker once its engine call returns: a thread left inside
        the engine's runtime at interpreter exit aborts the process."""
        with self.cv:
            self.stopping = True
            self.cv.notify()
        self.thread.join()

    # ---- under cv
    def _drop_stale(self, now):
        """Queued audio older than max_lag_s goes, and the opens queued with
        it: those utterances would start without their beginning. -> the age
        of the oldest queued audio."""
        pushes = [op for op in self.ops if op[0] == "push"]
        lag = now - pushes[0][3] if pushes else 0.0
        if lag > self.max_lag_s:
            self.drops += 1
            self.dropped_s += sum(len(op[2]) for op in pushes) / SR
            self.ops = [op for op in self.ops if op[0] == "close"]
            self.reset = True
        return lag

    # ---- worker
    def _run(self):
        self.tid = threading.get_native_id()
        base = os.getpriority(os.PRIO_PROCESS, self.tid)
        os.setpriority(os.PRIO_PROCESS, self.tid, base + self.nice)
        t0 = time.monotonic()
        try:
            self.engine = self.load()
            state, error = "ready", ""
        except Exception as e:  # noqa: BLE001
            state, error = "failed", f"{type(e).__name__}: {e}"
        with self.cv:
            self.state, self.error = state, error
            if state == "failed":
                self.ops = []
        self.load_s = time.monotonic() - t0
        self.out.put(("live_loaded", self.state, self.error, self.load_s))
        streams = {}
        while self.state == "ready":
            with self.cv:
                while not self.ops and not self.stopping:
                    self.cv.wait()
                if self.stopping:
                    return
                lag = self._drop_stale(time.monotonic())
                ops, self.ops = self.ops, []
                reset, self.reset = self.reset, False
            if reset:
                streams.clear()
            try:
                self._step(ops, streams, lag)
            except Exception as e:  # noqa: BLE001
                self.out.put(("live_error", f"{type(e).__name__}: {e}"))
                streams.clear()

    def _step(self, ops, streams, lag):
        pending = {}
        for op in ops:
            kind, key = op[0], op[1]
            if kind == "open":
                streams[key] = self.engine.open()
            elif kind == "close":
                streams.pop(key, None)
                pending.pop(key, None)
            elif key in streams:
                pending.setdefault(key, []).append(op[2])
        if not pending:
            return
        pairs = [(streams[k], np.concatenate(p)) for k, p in pending.items()]
        t0 = time.monotonic()
        done = self.engine.push_all(pairs)
        audio = sum(len(pcm) for _st, pcm in pairs) / SR
        self.stats.add(time.monotonic_ns(), audio, time.monotonic() - t0, lag)
        for key, (_st, events) in zip(pending, done, strict=True):
            for kind, text in events:
                if kind == "partial":
                    self.out.put(("partial", key, text))
