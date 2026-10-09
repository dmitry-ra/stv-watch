"""Speech recognition engines behind one interface.

Engine.open() -> Stream; Stream.push(pcm) and Stream.finish() return events
("partial", text) / ("final", text). A partial is the whole hypothesis of the
current utterance (it replaces the previous one); a final closes it. Input is
mono float32 at 16 kHz. An utterance engine (Parakeet) returns its final on
finish(); a streaming engine would return partials from push().
"""

import os

from . import weights

ENGINES = ("parakeet",)
# the weights each engine loads, names of weights.PINS
WEIGHTS = {"parakeet": ("parakeet", "silero-vad")}


def build(name, threads, models_dir):
    """The engine `name`, loaded from its weights in `models_dir` (fetched by
    weights.ensure beforehand)."""
    if name == "parakeet":
        from .parakeet import ParakeetUtterance

        vad = weights.PINS["silero-vad"]
        e = ParakeetUtterance(
            weights.model_dir(weights.PINS[name], models_dir),
            threads,
            vad_path=os.path.join(weights.model_dir(vad, models_dir), vad.files[0].name),
        )
        e.load()
        return e
    raise ValueError(name)
