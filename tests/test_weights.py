"""The weights download against a local HTTP server: a file takes its name only
after its size and sha256 check out; a failed check leaves nothing behind."""

import hashlib
import http.server
import os
import threading

import pytest

from stvwatch.asr import weights

FILES = {"config.json": b'{"model_type": "x"}', "model.onnx": os.urandom(3 << 20)}


@pytest.fixture
def server(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name, data in FILES.items():
        (src / name).write_bytes(data)
    seen = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(src), **kw)

        def log_message(self, *a):
            seen.append(self.path)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", src, seen
    httpd.shutdown()


def pin(base, **wrong):
    files = tuple(
        weights.WeightFile(n, len(d), hashlib.sha256(d).hexdigest()) for n, d in FILES.items()
    )
    files = tuple(weights.WeightFile(**{**vars(f), **wrong.get(f.name, {})}) for f in files)
    return weights.Pin("t", "test", base, "0123456789abcdef", "CC0", files)


def test_files_are_fetched_checked_and_named_once(server, tmp_path):
    base, _src, seen = server
    said = []
    p = pin(base)
    path = weights.ensure(p, str(tmp_path / "m"), log=said.append)
    assert path == str(tmp_path / "m" / "t-0123456")
    assert {n: open(os.path.join(path, n), "rb").read() for n in FILES} == FILES
    assert sorted(os.listdir(path)) == sorted(FILES)
    assert len(said) == 2 and "0.00 GB, revision 0123456" in said[0]
    n = len(seen)
    assert weights.ensure(p, str(tmp_path / "m"), log=said.append) == path
    assert len(seen) == n and len(said) == 2  # present: no request, no line


@pytest.mark.parametrize(
    "wrong, why",
    [
        ({"sha256": "0" * 64}, "sha256"),
        ({"size": len(FILES["model.onnx"]) + 1}, "bytes"),
        ({"name": "absent.onnx"}, "404"),
    ],
)
def test_a_file_failing_its_check_is_refused_and_not_kept(server, tmp_path, wrong, why):
    base, _src, _seen = server
    with pytest.raises(weights.WeightsError, match=why):
        weights.ensure(pin(base, **{"model.onnx": wrong}), str(tmp_path / "m"), log=print)
    assert os.listdir(tmp_path / "m" / "t-0123456") == ["config.json"]


def test_a_missing_file_or_one_of_the_wrong_size_is_fetched_again(server, tmp_path):
    base, _src, _seen = server
    p = pin(base)
    path = weights.ensure(p, str(tmp_path / "m"), log=print)
    with open(os.path.join(path, "model.onnx"), "ab") as f:
        f.write(b"x")
    os.remove(os.path.join(path, "config.json"))
    assert [f.name for f in weights.missing(p, path)] == ["config.json", "model.onnx"]
    weights.ensure(p, str(tmp_path / "m"), log=print)
    assert {n: open(os.path.join(path, n), "rb").read() for n in FILES} == FILES


def test_the_parakeet_pin_is_the_measured_revision():
    p = weights.PINS["parakeet"]
    assert p.revision == "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce" and p.source.endswith(
        p.revision
    )
    assert p.size == 2_549_805_955
    assert weights.model_dir(p, "/m") == "/m/parakeet-8f23f0c"


def test_models_default_to_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    assert weights.default_dir() == str(tmp_path / "c" / "stv-watch" / "models")
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert weights.default_dir() == str(tmp_path / "h" / ".cache" / "stv-watch" / "models")
