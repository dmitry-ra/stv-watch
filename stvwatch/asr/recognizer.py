"""Recognition behind the viewer: one model in memory, one job per closed
utterance.

The channel's audio is collected until the utterance closes (no frames for
the close time, or a monologue piece cut at --max-utt-ms), then recognized in
one call; one final line per utterance. One worker thread, FIFO. Results go
to `out` as tuples; the main loop owns all display state. The queue holds at
most MAX_QUEUED_S of audio: past it a replay waits for room, live input drops
the utterance, as the network client must never wait for the recognizer.
"""

import queue
import threading
import time
from collections import deque

import numpy as np

from ..model import NS
from . import build

SR = 16000
MAX_QUEUED_S = 600.0  # float32 at 16 kHz: 38 MB of PCM waiting for the worker


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
    def __init__(self, model, threads, models_dir, min_speech_ms, media_now, out, wait=True):
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
        self.wait = wait  # a full queue: True waits for room (replay), False drops (live)
        self.jobs = 0  # queued or being recognized
        self.audio_s = 0.0
        self.room = threading.Condition()
        self.shared_q = queue.Queue()
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
    def _done(self, audio_s):
        with self.room:
            self.jobs -= 1
            self.audio_s -= audio_s
            self.room.notify_all()

    def queue_depth(self):
        with self.room:
            return self.jobs, self.audio_s

    def utterance(self, key, pcm, meta):
        """One closed utterance -> one job, or False when the queue already
        holds MAX_QUEUED_S of audio and this input must not wait for room."""
        audio_s = len(pcm) / SR
        with self.room:
            while self.jobs and self.audio_s + audio_s > MAX_QUEUED_S:
                if not self.wait or self.state != "ready":
                    return False
                self.room.wait()
            self.jobs += 1
            self.audio_s += audio_s
        self.shared_q.put((key, pcm, meta))
        return True

    def close(self):
        """Stop the worker: queued jobs are dropped for the caller to name, the
        one being recognized ends unread."""
        while True:
            try:
                _key, pcm, _meta = self.shared_q.get_nowait()
            except queue.Empty:
                break
            self._done(len(pcm) / SR)
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
                self._done(audio_s)
                continue
            w0 = time.monotonic()
            try:
                st = self.engine.open()
                events = st.push(pcm) + st.finish()
            except Exception as e:  # noqa: BLE001
                events, meta = [], dict(meta, result="recognition failed")
                self.out.put(("error", jkey, f"{type(e).__name__}: {e}"))
            # Wall time, not thread CPU: engine threads do the work outside this one.
            compute = time.monotonic() - w0
            now = self.media_now()
            ref = meta.get("closed_ns") or now
            self.stats.add(now, audio_s, compute, max(0.0, (now - ref) / NS))
            texts = [t.strip() for kind, t in events if kind == "final" and t.strip()]
            done = dict(meta, asr_ms=round(compute * 1000))
            done.update((kind, v) for kind, v in events if kind == "speech_ms")
            self.out.put(("final", jkey, " ".join(texts), done))
            self._done(audio_s)


def np_concat(parts):
    return np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, np.float32)
