"""Leaving the relay, build/CRC resolution, failure classes, A2S."""

import os
import socket
import struct
import tempfile
import threading

import pytest

from stvwatch.net import a2s, builds, handshake, netchan, supervisor, wire
from stvwatch.net import dump as dumpfmt
from stvwatch.net import session as sess


class FakeSock:
    def __init__(self, recv_exc=socket.timeout):
        self.sent = []
        self.recv_exc = recv_exc

    def sendto(self, data, addr):
        self.sent.append(data)

    def settimeout(self, t):
        pass

    def recvfrom(self, n):
        raise self.recv_exc()


class FakeConn:
    def __init__(self, sock):
        self.sock = sock
        self.server = ("127.0.0.1", 1)
        self.challenge = 0x1234
        self.pending = None

    def close(self):
        pass


def disconnect_reasons(packets):
    """Reasons of every net_Disconnect found as the first unreliable message."""
    out = []
    for p in packets:
        h = netchan.decode_header(p)
        if h.reliable:
            continue
        br = wire.BitReader(p)
        br.pos = h.body_offset * 8
        if (
            br.bits_left() >= wire.NETMSG_TYPE_BITS
            and br.read_ubit(wire.NETMSG_TYPE_BITS) == wire.NET_DISCONNECT
        ):
            out.append(br.read_string())
    return out


def make_sup(**kw):
    path = os.path.join(tempfile.mkdtemp(), "t.tvd")
    opt = supervisor.Options(build="10889068", **kw)
    sup = supervisor.Supervisor(opt, path, log=lambda *a: None)
    sup.crc = builds.crc_for("10889068")
    return sup, path


def events(path, rtype):
    return [f for _, t, f in dumpfmt.DumpReader(path).events() if t == rtype]


# --- leaving ----------------------------------------------------------------


def test_every_exit_path_sends_net_disconnect():
    # deadline, stop flag and an exception escaping the loop: all must leave.
    for setup in ("deadline", "stop", "exception"):
        sock = FakeSock(RuntimeError if setup == "exception" else socket.timeout)
        sup, path = make_sup()
        sup.stop = setup == "stop"
        if setup == "exception":
            with pytest.raises(RuntimeError):
                sup._run_session(FakeConn(sock), deadline=None)
        else:
            sup._run_session(FakeConn(sock), deadline=0.0)
        sup.dump.close()
        assert disconnect_reasons(sock.sent) == ["Disconnect by user."] * 2, setup
        assert [e["sent"] for e in events(path, dumpfmt.LEAVE)] == [2], setup


def test_no_disconnect_after_the_relay_dropped_us():
    sock = FakeSock()
    sup, path = make_sup()
    s = sess.Session(FakeConn(sock), sup.opt, sup, crc=sup.crc)
    s.fail(sess.BREAK_DISCONNECT, "Server shutting down")
    sup._leave(s)
    assert disconnect_reasons(sock.sent) == []
    # Any other break (silence, changelevel) still leaves: the slot may exist.
    s2 = sess.Session(FakeConn(sock), sup.opt, sup, crc=sup.crc)
    s2.fail(sess.BREAK_CHANGELEVEL, "server signalled changelevel")
    sup._leave(s2)
    assert disconnect_reasons(sock.sent) == ["Disconnect by user."] * 2


# --- builds and failure classes ----------------------------------------------


def test_crc_by_build_and_unknown_build():
    assert builds.crc_for("10889068") == 0xD9B6082D
    assert builds.crc_for(9540945) == 0x35B19FD9
    with pytest.raises(builds.UnknownBuild):
        builds.crc_for("10889069")


@pytest.mark.parametrize(
    "reason,cls",
    [
        ("connect refused: '#GameUI_ServerRejectOldVersion'", builds.PERMANENT),
        ("connect refused: '#GameUI_ServerRejectBadPassword'", builds.PERMANENT),
        ("Server uses different class tables", builds.PERMANENT),
        ("Bad spectator password", builds.PERMANENT),
        ("unknown build 1: add its CRC to stvwatch/net/builds.py", builds.PERMANENT),
        ("relay wants auth protocol 3 (anonymous needs 2)", builds.PERMANENT),
        ("connect refused: '#GameUI_ServerRejectServerFull'", builds.TEMPORARY),
        ("connect refused: '#GameUI_ServerRejectBadChallenge'", builds.TEMPORARY),
        ("timed out", builds.TEMPORARY),
        ("a2s: no reply from 127.0.0.1:27020", builds.TEMPORARY),
        (None, builds.TEMPORARY),
    ],
)
def test_classify(reason, cls):
    assert builds.classify(reason) == cls


class Driven(supervisor.Supervisor):
    """Network removed: handshake outcomes scripted, waits recorded."""

    def __init__(self, script, a2s_version=None, **kw):
        path = os.path.join(tempfile.mkdtemp(), "t.tvd")
        super().__init__(supervisor.Options(seconds=0, **kw), path, log=lambda *a: None)
        self.script = list(script)
        self.waits = []
        self.handshakes = 0
        self.a2s_version = a2s_version

    def _handshake(self, attempt):
        self.handshakes += 1
        self.last_error = self.script[len(self.waits)]
        return None

    def _wait(self, seconds, deadline):
        self.waits.append(seconds)
        return len(self.waits) < len(self.script)


def test_permanent_handshake_refusal_polls_slowly_and_alarms():
    old = "connect refused: '#GameUI_ServerRejectOldVersion'"
    sup = Driven([old] * 10, build="10889068")
    sup.run()
    assert max(sup.waits) > sup.opt.backoff_max_s
    assert sup.alarm == old
    # A temporary failure keeps the ordinary cap.
    sup = Driven(["timed out"] * 10, build="10889068")
    sup.run()
    assert max(sup.waits) == sup.opt.backoff_max_s
    assert sup.alarm is None


def test_unknown_build_from_a2s_never_connects(monkeypatch):
    monkeypatch.setattr(a2s, "info", lambda ip, port: {"version": "1"})
    sup = Driven([None] * 8)
    sup.run()
    assert sup.handshakes == 0
    assert "unknown build 1" in sup.alarm
    assert max(sup.waits) > sup.opt.backoff_max_s


def test_auto_build_resolves_crc_from_a2s(monkeypatch):
    monkeypatch.setattr(a2s, "info", lambda ip, port: {"version": "10889068"})
    sup = Driven(["timed out"])
    sup.run()
    assert sup.handshakes == 1
    assert (sup.build, sup.crc) == ("10889068", 0xD9B6082D)


# --- A2S ----------------------------------------------------------------------


def _info_reply(players=3, bots=1, version="10889068"):
    body = (
        bytes([0x49, 17])
        + b"Srv\0dm_x\0hl2mp\0Deathmatch\0"
        + struct.pack("<H", 320)
        + bytes([players, 33, bots])
        + b"dl\x00\x01"
        + version.encode()
        + b"\0"
        + bytes([0x80 | 0x40])
        + struct.pack("<H", 27015)
        + struct.pack("<H", 27020)
        + b"SourceTV\0"
    )
    return a2s.HEADER + body


def test_a2s_follows_challenge_and_parses_edf():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(5)
    seen = []

    def serve():
        for _ in range(2):
            data, addr = srv.recvfrom(1024)
            seen.append(data)
            if data == a2s.INFO_QUERY:
                srv.sendto(a2s.HEADER + b"A" + b"\x01\x02\x03\x04", addr)
            else:
                srv.sendto(_info_reply(), addr)

    t = threading.Thread(target=serve)
    t.start()
    try:
        info = a2s.info("127.0.0.1", srv.getsockname()[1], timeout=2, attempts=1)
    finally:
        t.join(5)
        srv.close()
    assert seen[1] == a2s.INFO_QUERY + b"\x01\x02\x03\x04"
    assert (info["players"], info["bots"], info["version"]) == (3, 1, "10889068")
    assert (info["port"], info["tv_port"], info["tv_name"]) == (27015, 27020, "SourceTV")


def test_handshake_stops_before_connect_when_relay_wants_steam_auth():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(5)
    got = []

    def serve():
        data, addr = srv.recvfrom(1024)
        got.append(data)
        nonce = data[5:9]
        srv.sendto(
            b"\xff\xff\xff\xffA"
            + struct.pack("<I", wire.S2C_MAGICVERSION)
            + b"\x01\x00\x00\x00"
            + nonce
            + struct.pack("<I", 3),
            addr,
        )
        srv.settimeout(1.0)
        try:
            got.append(srv.recvfrom(1024)[0])
        except socket.timeout:
            pass

    t = threading.Thread(target=serve)
    t.start()
    try:
        with pytest.raises(handshake.HandshakeError, match="auth protocol 3"):
            handshake.connect("127.0.0.1", srv.getsockname()[1], "t", "10889068", timeout=2.0)
    finally:
        t.join(5)
        srv.close()
    assert len(got) == 1  # no 'k' after the challenge


def test_unanswered_connect_is_followed_by_a_disconnect():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(5)
    got = []

    def serve():
        data, addr = srv.recvfrom(1024)
        srv.sendto(
            b"\xff\xff\xff\xffA"
            + struct.pack("<I", wire.S2C_MAGICVERSION)
            + b"\x01\x00\x00\x00"
            + data[5:9]
            + struct.pack("<I", 2),
            addr,
        )
        srv.settimeout(3.0)
        try:
            while True:
                got.append(srv.recvfrom(1024)[0])
        except socket.timeout:
            pass

    t = threading.Thread(target=serve)
    t.start()
    try:
        with pytest.raises(handshake.HandshakeError, match="no accept"):
            handshake.connect("127.0.0.1", srv.getsockname()[1], "t", "10889068", timeout=1.0)
    finally:
        t.join(10)
        srv.close()
    assert got[0][4:5] == b"k"
    assert disconnect_reasons(got[1:]) == ["Disconnect by user."] * 2


def test_reject_reason_skips_the_client_challenge():
    """Measured on a full loopback relay: '#GameUI_ServerRejectServerFull'
    came after 4 bytes of our challenge, which used to garble the text."""
    pkt = (
        struct.pack("<iBi", -1, wire.S2C_CONNREJECT, 0x7F04E539)
        + b"#GameUI_ServerRejectServerFull\x00"
    )
    assert handshake.reject_reason(pkt) == "#GameUI_ServerRejectServerFull"
    assert builds.classify(handshake.reject_reason(pkt)) == builds.TEMPORARY
