"""--serve and --attach: a headless engine's screen on a Unix socket, drawn by
any number of clients the way a local run draws it."""

import os
import shutil
import socket
import stat
import tempfile

import pytest

from stvwatch import serve


@pytest.fixture
def sock():
    """A socket path short enough for AF_UNIX, whatever pytest's tmp_path."""
    d = tempfile.mkdtemp(prefix="sw", dir="/tmp")
    yield os.path.join(d, "s")
    shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize("left", ["nothing", "stale", "live", "file"])
def test_serve_takes_a_path_only_from_a_dead_engine(sock, left):
    other = None
    if left == "stale":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(sock)
        s.close()
    elif left == "live":
        other = serve.listen(sock)
    elif left == "file":
        with open(sock, "w") as f:
            f.write("keep")
    try:
        if left in ("live", "file"):
            with pytest.raises(serve.ServeError):
                serve.listen(sock)
            assert left == "live" or open(sock).read() == "keep"
            return
        serve.listen(sock).close()
        st = os.stat(sock)
        assert stat.S_ISSOCK(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600
    finally:
        if other is not None:
            other.close()


def test_a_bare_name_is_a_socket_in_the_runtime_dir_made_private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = serve.socket_path("noob")
    assert path == str(tmp_path / "stv-watch" / "noob")
    serve.listen(path).close()
    assert stat.S_IMODE(os.stat(tmp_path / "stv-watch").st_mode) == 0o700
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    with pytest.raises(serve.ServeError):
        serve.socket_path("noob")
