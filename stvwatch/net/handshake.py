#!/usr/bin/env python3
"""Connectionless SourceTV handshake: getchallenge -> challenge -> connect.

    C->S  'q'  A2S_GETCHALLENGE   nonce + steam2 key placeholder
    S->C  'A'  S2C_CHALLENGE      magic, server challenge, echoed nonce, authproto
    C->S  'k'  C2S_CONNECT        proto, authproto, challenge, nonce, name,
                                  password, build, cdkey
    S->C  'B'  S2C_CONNECTION     accept -> the reliable netchannel exists

Auth is the anonymous HASHEDCDKEY path (a relay with an empty `tv_password`
needs no Steam ticket); the cdkey is a dummy MD5 of the client name.

The relay may start streaming in-band datagrams BEFORE or INSTEAD OF the 'B'
accept. That is not an error: an in-band packet is itself proof the channel is
up, so it is returned as `pending` rather than discarded.
"""

import hashlib
import os
import socket
import struct
import time

from . import messages, netchan, wire

LEAVE = "Disconnect by user."


class HandshakeError(Exception):
    pass


class Connection:
    __slots__ = ("sock", "server", "nonce", "challenge", "name", "pending")

    def __init__(self, sock, server, nonce, challenge, name):
        self.sock = sock
        self.server = server
        self.nonce = nonce
        self.challenge = challenge
        self.name = name
        self.pending = None  # in-band datagram seen during handshake

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _oob(*parts):
    return b"\xff\xff\xff\xff" + b"".join(parts)


def _cstr(s):
    return s.encode("utf-8", "replace") + b"\x00"


def build_getchallenge(nonce):
    return _oob(
        bytes([wire.A2S_GETCHALLENGE]), struct.pack("<I", nonce), _cstr("0" * 10)
    )  # steam2 key placeholder


def build_connect(nonce, challenge, name, build, password=""):
    cdkey = hashlib.md5(name.encode()).hexdigest()
    return _oob(
        bytes([wire.C2S_CONNECT]),
        struct.pack("<i", wire.PROTOCOL_VERSION),
        struct.pack("<i", wire.AUTH_HASHEDCDKEY),
        struct.pack("<I", challenge),
        struct.pack("<I", nonce),
        _cstr(name),
        _cstr(password),
        _cstr(build),
        _cstr(cdkey),
    )


def reject_reason(data):
    """S2C_CONNREJECT: i32 -1 | 'A'+... | i32 client challenge | cstr reason
    (CBaseServer::RejectConnection, protocol 24)."""
    return data[9:].split(b"\x00", 1)[0].decode("utf-8", "replace")


CHALLENGE_MIN = 21  # 4 marker + 1 cmd + 16 (magic, challenge, nonce, auth)


def parse_challenge(data):
    """Parse an S2C_CHALLENGE reply. Raises only HandshakeError.

    Every length is checked before unpacking: a stub reply from a relay
    mid-restart, or a stray A2S response landing on our ephemeral port, must
    fail this one connect attempt - not escape as struct.error past the
    supervisor's (HandshakeError, OSError) catch and kill the process.
    """
    if len(data) < 5 or struct.unpack_from("<i", data, 0)[0] != -1:
        raise HandshakeError(f"not connectionless: {data[:8].hex()}")
    cmd = data[4]
    if cmd == wire.S2C_CONNREJECT:
        raise HandshakeError(f"refused: {reject_reason(data)!r}")
    if cmd != wire.S2C_CHALLENGE:
        raise HandshakeError(f"unexpected reply {chr(cmd)!r} ({cmd:#x})")
    if len(data) < CHALLENGE_MIN:
        raise HandshakeError(f"truncated challenge ({len(data)}B < {CHALLENGE_MIN})")
    magic, challenge, echo_nonce, auth = struct.unpack_from("<IiiI", data, 5)
    return {
        "magic": magic & 0xFFFFFFFF,
        "challenge": challenge & 0xFFFFFFFF,
        "echo_nonce": echo_nonce & 0xFFFFFFFF,
        "auth_proto": auth,
    }


def connect(ip, port, name, build, password="", timeout=6.0, on_packet=None):
    """Run the full handshake. Returns a Connection.

    `on_packet(direction, data)` is called for every datagram so the caller can
    dump the handshake itself - the handshake is where a connect failure lives,
    so it must be in the dump like everything else. direction: 'in' | 'out'.
    """
    server = (ip, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    nonce = struct.unpack("<I", os.urandom(4))[0]

    def emit(direction, data):
        if on_packet is not None:
            on_packet(direction, data)

    try:
        gc = build_getchallenge(nonce)
        sock.sendto(gc, server)
        emit("out", gc)
        data, _ = sock.recvfrom(65535)
        emit("in", data)
        ch = parse_challenge(data)
        if ch["magic"] != wire.S2C_MAGICVERSION:
            raise HandshakeError(f"bad magic {ch['magic']:#x}")
        if ch["auth_proto"] != wire.AUTH_HASHEDCDKEY:
            # A relay asking for Steam auth ignores an anonymous connect
            # silently; sending it would only cost the relay a lookup.
            raise HandshakeError(
                f"relay wants auth protocol {ch['auth_proto']}"
                f" (anonymous needs {wire.AUTH_HASHEDCDKEY})"
            )

        ck = build_connect(nonce, ch["challenge"], name, build, password)
        sock.sendto(ck, server)
        emit("out", ck)

        deadline = time.time() + timeout
        while time.time() < deadline:
            sock.settimeout(max(0.1, deadline - time.time()))
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                break
            emit("in", data)
            if len(data) < 4:
                continue  # runt: cannot even be classified
            mark = struct.unpack_from("<i", data, 0)[0]
            if mark == wire.MARK_OOB and len(data) > 4:
                if data[4] == wire.S2C_CONNECTION:
                    return Connection(sock, server, nonce, ch["challenge"], name)
                if data[4] == wire.S2C_CONNREJECT:
                    raise HandshakeError(f"connect refused: {reject_reason(data)!r}")
                continue
            # in-band before/without 'B' - the channel is up; keep the datagram
            conn = Connection(sock, server, nonce, ch["challenge"], name)
            conn.pending = data
            return conn
        # The relay may have opened a channel whose accept we never saw; it
        # would hold a slot for 300 s. A disconnect at seq 1 closes it, and a
        # relay with no channel for us drops the packet.
        bye = netchan.build_packet(
            1, 0, ch["challenge"], 0, unreliable=messages.disconnect_body(LEAVE)
        )
        for _ in range(2):
            sock.sendto(bye, server)
            emit("out", bye)
        raise HandshakeError("no accept within timeout")
    except Exception:
        sock.close()
        raise
