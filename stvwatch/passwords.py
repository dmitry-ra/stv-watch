"""Where the tv_password of a relay comes from, read again before every
connection attempt so that a changed password needs no restart.

In order: --password-file (its first line), $STV_WATCH_PASSWORD, the store
$XDG_CONFIG_HOME/stv-watch/passwords (lines 'HOST:PORT PASSWORD'). The first
one given wins; the store is used only for a relay that announces a password.
No message here ever holds a password.
"""

import os
import stat

# A password file is read whole only up to this: a wrong path given to it
# (a recording, a log) must not be loaded into memory.
MAX_FILE = 4096
MAX_STORE = 1 << 20
ENV = "STV_WATCH_PASSWORD"


class PasswordError(ValueError):
    pass


def store_path():
    """$XDG_CONFIG_HOME/stv-watch/passwords, ~/.config when it is unset."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "stv-watch", "passwords")


def relay_key(addr):
    """'Host:0027020' -> 'host:27020'; None if it is not HOST:PORT."""
    host, sep, port = addr.rpartition(":")
    if not sep or not host or ":" in host or not (port.isascii() and port.isdigit()):
        return None
    if not 1 <= int(port) <= 65535:
        return None
    return f"{host.lower()}:{int(port)}"


def _checked(text, where):
    if "\x00" in text:
        raise PasswordError(f"{where}: holds a NUL byte, which the connect request cannot carry")
    return text


def _read(path, limit, where):
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            raw = f.read(limit + 1)
    except OSError as e:
        raise PasswordError(f"{where}: {e.strerror}: {path}") from None
    if len(raw) > limit:
        raise PasswordError(f"{where}: longer than {limit} bytes, not a password file")
    try:
        return raw.decode("utf-8"), st
    except UnicodeDecodeError:
        raise PasswordError(f"{where}: not UTF-8 text") from None


def read_file(path):
    """The first line of `path`, as it is."""
    text, _st = _read(path, MAX_FILE, "--password-file")
    lines = text.splitlines()
    if not lines or not lines[0]:
        raise PasswordError("--password-file: the first line is empty")
    return _checked(lines[0], "--password-file")


def read_store(path):
    """{relay key: password}; {} when the file does not exist. Refused when
    anyone but its owner may read or write it, as ssh refuses such a key."""
    if not os.path.lexists(path):
        return {}
    text, st = _read(path, MAX_STORE, path)
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise PasswordError(
            f"{path}: mode {mode:04o} lets other users read or change it, "
            f"not used; chmod 600 {path}"
        )
    out = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        key = relay_key(parts[0])
        if key is None or len(parts) < 2:
            raise PasswordError(f"{path}, line {n}: wants 'HOST:PORT PASSWORD'")
        if key in out:
            raise PasswordError(f"{path}, line {n}: {key} is given twice")
        out[key] = _checked(parts[1], f"{path}, line {n}")
    return out


class Source:
    """The password of one relay. `env` is the value of $STV_WATCH_PASSWORD
    taken at the start (None when unset)."""

    def __init__(self, relay=None, file=None, env=None, store=None):
        self.relay, self.file, self.env = relay, file, env
        self.store = store if store is not None else store_path()

    def get(self):
        """-> (password, origin): origin 'file', 'env', 'store' or None (no
        password, ''). Raises PasswordError."""
        if self.relay is None:
            return "", None
        if self.file is not None:
            return read_file(self.file), "file"
        if self.env is not None:
            if not self.env:
                raise PasswordError(f"${ENV} is set but empty")
            return _checked(self.env, "$" + ENV), "env"
        pw = read_store(self.store).get(relay_key(self.relay) or "")
        return (pw, "store") if pw else ("", None)

    def watched(self):
        """The file a password may change in while we run, or None."""
        if self.relay is None:
            return None
        if self.file is not None:
            return self.file
        return None if self.env is not None else self.store

    def stamp(self):
        """Changes whenever the watched file is written, made or removed."""
        path = self.watched()
        if path is None:
            return None
        try:
            st = os.stat(path)
        except OSError:
            return ()
        return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_mode)
