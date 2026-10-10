"""Speech recognition engines behind one interface.

Engine.open() -> Stream; Stream.push(pcm) and Stream.finish() return events
("partial", text) / ("final", text). A partial is the whole hypothesis of the
current utterance (it replaces the previous one); a final closes it. An
engine with a voice activity detector also says ("speech_ms", n) on finish(). Input is
mono float32 at 16 kHz. An utterance engine (Parakeet) returns its final on
finish(); a streaming engine (Nemotron) returns partials from push() and
leaves the final to an utterance engine.
"""

import os

from . import weights

ENGINES = ("parakeet",)
# the weights each engine loads, names of weights.PINS
WEIGHTS = {"parakeet": ("parakeet", "silero-vad")}
# live engines show partial text while a player talks; never a --asr engine
LIVE_ENGINES = ("nemotron",)
LIVE_WEIGHTS = {"nemotron": ("nemotron",)}


def build(name, threads, models_dir, min_speech_ms):
    """The engine `name`, loaded from its weights in `models_dir` (fetched by
    weights.ensure beforehand)."""
    if name == "parakeet":
        from .parakeet import ParakeetUtterance

        vad = weights.PINS["silero-vad"]
        e = ParakeetUtterance(
            weights.model_dir(weights.PINS[name], models_dir),
            threads,
            vad_path=os.path.join(weights.model_dir(vad, models_dir), vad.files[0].name),
            min_speech_ms=min_speech_ms,
        )
        e.load()
        return e
    raise ValueError(name)


def build_live(name, threads, models_dir, language=""):
    """The live engine `name`, loaded like build() does."""
    if name == "nemotron":
        from .nemotron import NemotronStreaming

        e = NemotronStreaming(weights.model_dir(weights.PINS[name], models_dir), threads, language)
        e.load()
        return e
    raise ValueError(name)
