"""The weights download against a local HTTP server: a file takes its name only
after its size and sha256 check out; a failed check leaves nothing behind."""

import hashlib
import http.server
import io
import os
import tarfile
import threading

import pytest

from stvwatch import cli
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
    return weights.Pin("t", "test", base, "0123456" + "0" * 33, "CC0", files)


def test_files_are_fetched_checked_and_named_once(server, tmp_path):
    base, _src, seen = server
    said = []
    p = pin(base)
    path = weights.ensure(p, str(tmp_path / "m"), log=said.append)
    assert path == str(tmp_path / "m" / "t-0123456")
    assert {n: open(os.path.join(path, n), "rb").read() for n in FILES} == FILES
    assert sorted(os.listdir(path)) == sorted(FILES)
    assert len(said) == 2 and "3.15 MB, revision 0123456 " in said[0]
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
    v = weights.PINS["silero-vad"]
    assert [(f.name, f.size, f.sha256[:12]) for f in v.files] == [
        ("silero_vad.onnx", 643854, "9e2449e10874")
    ]
    assert v.source.startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/")
    assert weights.model_dir(v, "/m") == "/m/silero-vad-v4"


def test_models_default_to_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    assert weights.default_dir() == str(tmp_path / "c" / "stv-watch" / "models")
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    assert weights.default_dir() == str(tmp_path / "h" / ".cache" / "stv-watch" / "models")


def test_a_models_dir_that_cannot_be_made_is_a_weights_error_and_exit_2(tmp_path, capsys):
    (tmp_path / "file").write_bytes(b"")
    under = str(tmp_path / "file" / "m")
    with pytest.raises(weights.WeightsError, match="Not a directory"):
        weights.ensure(pin("http://127.0.0.1:9"), under, log=print)
    assert cli.main(["--replay", "x.tvd", "--asr", "parakeet", "--models-dir", under]) == 2
    assert "parakeet weights not available" in capsys.readouterr().err


STEM = "model-2026"
MEMBERS = {f"{STEM}/{n}": d for n, d in FILES.items()}


def archive(src, members):
    """STEM.tar.bz2 in `src` from {name: bytes}; a str value is a symlink target."""
    name = STEM + ".tar.bz2"
    with tarfile.open(src / name, "w:bz2") as t:
        for n, d in members.items():
            info = tarfile.TarInfo(n)
            if isinstance(d, str):
                info.type, info.linkname = tarfile.SYMTYPE, d
                t.addfile(info)
            else:
                info.size = len(d)
                t.addfile(info, io.BytesIO(d))
    d = (src / name).read_bytes()
    return weights.WeightFile(name, len(d), hashlib.sha256(d).hexdigest())


def packed(base, arc, **wrong):
    return weights.Pin("t", "test", base, "rel", "CC0", pin(base, **wrong).files, "t", arc)


def test_an_archive_gives_its_pinned_files_only_and_is_removed(server, tmp_path):
    base, src, seen = server
    p = packed(base, archive(src, {**MEMBERS, f"{STEM}/README.md": b"x"}))
    said = []
    path = weights.ensure(p, str(tmp_path / "m"), log=said.append)
    assert {n: open(os.path.join(path, n), "rb").read() for n in FILES} == FILES
    assert sorted(os.listdir(path)) == sorted(FILES)
    assert seen == [f"/{STEM}.tar.bz2"] and "revision rel " in said[0]
    assert weights.ensure(p, str(tmp_path / "m"), log=said.append) == path
    assert len(seen) == 1


def test_names_in_an_archive_choose_nothing_outside_the_model_directory(server, tmp_path):
    base, src, _seen = server
    arc = archive(src, {**MEMBERS, f"{STEM}/../../escaped": b"x", "../escaped": b"x"})
    path = weights.ensure(packed(base, arc), str(tmp_path / "m" / "deep"), log=print)
    assert sorted(os.listdir(path)) == sorted(FILES)
    assert [f for _d, _s, fs in os.walk(tmp_path / "m") for f in fs if "escaped" in f] == []


@pytest.mark.parametrize(
    "case, why",
    [
        ("archive sha256", f"{STEM}.tar.bz2: sha256"),
        ("file sha256", "model.onnx: sha256"),
        ("symlink", "model.onnx: not a regular file"),
        ("absent", f"{STEM}.tar.bz2: no model.onnx"),
    ],
)
def test_an_archive_failing_a_check_is_refused_and_not_kept(server, tmp_path, case, why):
    base, src, _seen = server
    model = f"{STEM}/model.onnx"
    members = {
        "symlink": {**MEMBERS, model: "../../../outside"},
        "absent": {n: d for n, d in MEMBERS.items() if n != model},
    }.get(case, MEMBERS)
    arc = archive(src, members)
    if case == "archive sha256":
        arc = weights.WeightFile(arc.name, arc.size, "0" * 64)
    wrong = {"model.onnx": {"sha256": "0" * 64}} if case == "file sha256" else {}
    with pytest.raises(weights.WeightsError, match=why):
        weights.ensure(packed(base, arc, **wrong), str(tmp_path / "m"), log=print)
    left = os.listdir(tmp_path / "m" / "t")
    assert "model.onnx" not in left and not any(n.endswith((".bz2", ".part")) for n in left)
