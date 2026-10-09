"""Recognition behind the viewer: one model in memory, one job per closed
utterance.

The channel's audio is collected until the utterance closes (no frames for
the close time, or a monologue piece cut at --max-utt-ms), then recognized in
one call; one final line per utterance. One worker thread, FIFO. Results go
to `out` as tuples; the main loop owns all display state.
"""

import queue
import threading
import time
from collections import deque

import numpy as np

from ..model import NS
from . import build

SR = 16000


class Stats:
    """Rolling recognizer load, all on the media clock except compute time."""

    def __init__(self, span_s=60.0):
        self.span = int(span_s * NS)
        self.done = deque()  # (media_t_ns, audio_s, compute_s, lag_s)
        self.lock = threading.Lock()
        self.total_audio = 0.0
        self.total_compute = 0.0
        self.jobs = 0
        self.last_lag = 0.0

    def add(self, t_ns, audio_s, compute_s, lag_s):
        with self.lock:
            self.done.append((t_ns, audio_s, compute_s, lag_s))
            self.total_audio += audio_s
            self.total_compute += compute_s
            self.jobs += 1
            self.last_lag = lag_s

    def summary(self, now_ns):
        with self.lock:
            while self.done and self.done[0][0] < now_ns - self.span:
                self.done.popleft()
            items = list(self.done)
        audio = sum(i[1] for i in items)
        comp = sum(i[2] for i in items)
        lags = sorted(i[3] for i in items)
        p90 = lags[int(0.9 * (len(lags) - 1))] if lags else 0.0
        return {
            "rtf": comp / audio if audio else 0.0,
            "lag_last": self.last_lag,
            "lag_p90": p90,
            "jobs_min": len(items) * 60 * NS / self.span,
        }


class Recognizer:
    def __init__(self, model, threads, models_dir, min_speech_ms, media_now, out):
        self.model_name = model
        self.threads = threads
        self.models_dir = models_dir
        self.min_speech_ms = min_speech_ms
        self.media_now = media_now
        self.out = out  # queue of result tuples
        self.engine = None
        self.state = "loading"  # loading/ready/failed
        self.error = ""
        self.load_s = 0.0
        self.stats = Stats()
        self.pending = {}  # key -> [jobs, audio_s] not yet processed
        self.plock = threading.Lock()
        self.shared_q = queue.Queue()
        self.busy = 0
        threading.Thread(target=self._load, daemon=True, name="asr-load").start()

    # ---- engine
    def _load(self):
        t0 = time.monotonic()
        try:
            self.engine = build(self.model_name, self.threads, self.models_dir, self.min_speech_ms)
            self.state = "ready"
        except Exception as e:  # noqa: BLE001
            self.state = "failed"
            self.error = f"{type(e).__name__}: {e}"
        self.load_s = time.monotonic() - t0
        self.out.put(("loaded", self.state, self.error, self.load_s))
        threading.Thread(
            target=self._worker, args=(self.shared_q,), daemon=True, name="asr"
        ).start()

    # ---- submission (main loop)
    def _pend(self, key, jobs, audio_s):
        with self.plock:
            p = self.pending.setdefault(key, [0, 0.0])
            p[0] += jobs
            p[1] += audio_s

    def queue_depth(self):
        with self.plock:
            return (
                sum(p[0] for p in self.pending.values()),
                sum(p[1] for p in self.pending.values()),
            )

    def utterance(self, key, pcm, meta):
        """One closed utterance -> one job."""
        self._pend(key, 1, len(pcm) / SR)
        self.shared_q.put((key, pcm, meta))

    def close(self, timeout=10.0):
        """Let queued work drain (bounded), then stop the worker."""
        end = time.monotonic() + timeout
        while time.monotonic() < end and self.state == "ready":
            jobs, _a = self.queue_depth()
            if not jobs and not self.busy:
                break
            time.sleep(0.05)
        self.shared_q.put(None)

    # ---- worker
    def _worker(self, q):
        while True:
            job = q.get()
            if job is None:
                return
            jkey, pcm, meta = job
            audio_s = len(pcm) / SR
            if self.state != "ready":
                self._pend(jkey, -1, -audio_s)
                continue
            with self.plock:
                self.busy += 1
            w0 = time.monotonic()
            try:
                st = self.engine.open()
                events = st.push(pcm) + st.finish()
            except Exception as e:  # noqa: BLE001
                events = []
                self.out.put(("error", jkey, f"{type(e).__name__}: {e}"))
            # Wall time, not thread CPU: engine threads do the work outside this one.
            compute = time.monotonic() - w0
            now = self.media_now()
            ref = meta.get("end_ns") or now
            self.stats.add(now, audio_s, compute, max(0.0, (now - ref) / NS))
            self._pend(jkey, -1, -audio_s)
            with self.plock:
                self.busy -= 1
            texts = [t.strip() for kind, t in events if kind == "final" and t.strip()]
            done = dict(meta, asr_ms=round(compute * 1000))
            done.update((kind, v) for kind, v in events if kind == "speech_ms")
            self.out.put(("final", jkey, " ".join(texts), done))


def np_concat(parts):
    return np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, np.float32)
