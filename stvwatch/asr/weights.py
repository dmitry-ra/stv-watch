"""Recognition weights: fetched once from their source at a pinned revision,
each file checked by size and sha256 before it takes its name.

A file is written to NAME.part and renamed only after both checks pass; a
failed check removes it. So a file under its own name has passed the check
once, and a start only looks that every file is there with its size.
"""

import hashlib
import os
import sys
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class WeightFile:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Pin:
    engine: str
    title: str
    source: str  # base URL of the files at `revision`
    revision: str
    license: str
    files: tuple
    dirname: str = ""  # under the models directory; ENGINE-REVISION[:7] if empty

    @property
    def size(self):
        return sum(f.size for f in self.files)


_PARAKEET_REV = "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce"
_NEMOTRON_REV = "ab43d895f5985b1bbab8b6eac8607fcdc05343f3"
PINS = {
    "parakeet": Pin(
        engine="parakeet",
        title="Parakeet TDT 0.6B v3 (ONNX export of nvidia/parakeet-tdt-0.6b-v3)",
        source="https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx/resolve/"
        + _PARAKEET_REV,
        revision=_PARAKEET_REV,
        license="CC-BY-4.0",
        files=(
            WeightFile(
                "config.json",
                97,
                "666903c76b9798caf2c210afd4f6cd60b08a8dbf9800ec8d7a3bc0d2148ac466",
            ),
            WeightFile(
                "vocab.txt",
                93939,
                "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d",
            ),
            WeightFile(
                "decoder_joint-model.onnx",
                72520893,
                "e978ddf6688527182c10fde2eb4b83068421648985ef23f7a86be732be8706c1",
            ),
            WeightFile(
                "encoder-model.onnx",
                41770866,
                "98a74b21b4cc0017c1e7030319a4a96f4a9506e50f0708f3a516d02a77c96bb1",
            ),
            WeightFile(
                "encoder-model.onnx.data",
                2435420160,
                "9a22d372c51455c34f13405da2520baefb7125bd16981397561423ed32d24f36",
            ),
        ),
    ),
    "silero-vad": Pin(
        engine="silero-vad",
        title="Silero VAD v4 (snakers4/silero-vad, ONNX export by k2-fsa)",
        source="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models",
        revision="asr-models",
        license="MIT",
        files=(
            WeightFile(
                "silero_vad.onnx",
                643854,
                "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
            ),
        ),
        dirname="silero-vad-v4",
    ),
    "nemotron": Pin(
        engine="nemotron",
        title="Nemotron 3.5 ASR Streaming 0.6B, 560 ms chunks (ONNX int8 export of "
        "nvidia/nemotron-3.5-asr-streaming-0.6b by the sherpa-onnx author)",
        source="https://huggingface.co/csukuangfj2/"
        "sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-560ms-int8-2026-06-11/resolve/"
        + _NEMOTRON_REV,
        revision=_NEMOTRON_REV,
        license="OpenMDW-1.1",
        files=(
            WeightFile(
                "tokens.txt",
                131440,
                "729cc103155bafa785f9cd45746cd41cabe97eab7182fc04d594129587958f8a",
            ),
            WeightFile(
                "encoder.int8.onnx",
                657601403,
                "012e9321373af99021415e0b0eb3ec827b4be3153be6f30d9b448fe65e896e68",
            ),
            WeightFile(
                "decoder.int8.onnx",
                14978075,
                "19f9c98fc6d0a2c33a65a43b36fdb2e914c26c0aa9764be3aebc502a1e982fb0",
            ),
            WeightFile(
                "joiner.int8.onnx",
                9504438,
                "4101c7c679a0bc30483794b27a059e34e79232aa2068d78d51231a22c8b0d7ce",
            ),
        ),
    ),
}


class WeightsError(Exception):
    pass


def default_dir():
    """$XDG_CACHE_HOME/stv-watch/models, ~/.cache when it is unset."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "stv-watch", "models")


def model_dir(pin, models_dir):
    return os.path.join(models_dir, pin.dirname or f"{pin.engine}-{pin.revision[:7]}")


def missing(pin, path):
    out = []
    for f in pin.files:
        p = os.path.join(path, f.name)
        if not os.path.isfile(p) or os.path.getsize(p) != f.size:
            out.append(f)
    return out


def ensure(pin, models_dir, log=None, timeout=60.0):
    """-> the directory holding every file of `pin`, fetching what is not
    there. Raises WeightsError when a file cannot be fetched or fails a check."""
    log = log or (lambda text: print(text, file=sys.stderr, flush=True))
    path = model_dir(pin, models_dir)
    need = missing(pin, path)
    if not need:
        return path
    size = sum(f.size for f in need)
    amount = f"{size / 1e9:.2f} GB" if size >= 1e8 else f"{size / 1e6:.2f} MB"
    rev = pin.revision[:7] if len(pin.revision) == 40 else pin.revision
    log(
        f"stv-watch: downloading {pin.engine} weights, {amount}, revision {rev} "
        f"({pin.title}, {pin.license}) into {path}"
    )
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        raise WeightsError(f"cannot create {path}: {e.strerror}") from e
    for f in need:
        fetch(f"{pin.source}/{f.name}", os.path.join(path, f.name), f, timeout)
    log(f"stv-watch: {pin.engine} weights verified ({len(need)} files)")
    return path


def fetch(url, dest, want, timeout=60.0):
    part = dest + ".part"
    h = hashlib.sha256()
    got = 0
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r, open(part, "wb") as out:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                got += len(chunk)
                out.write(chunk)
        if got != want.size:
            raise WeightsError(f"{want.name}: {got} bytes, want {want.size}")
        if h.hexdigest() != want.sha256:
            raise WeightsError(f"{want.name}: sha256 {h.hexdigest()}, want {want.sha256}")
        os.replace(part, dest)
    except BaseException as e:
        if os.path.exists(part):
            os.remove(part)
        if isinstance(e, OSError):
            raise WeightsError(f"{want.name}: {e}") from e
        raise
