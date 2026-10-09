"""The client's precheck under a lost reply, the receiver's reading of our
own connectionless packets, datagrams that are not the relay's, and a relay
that streams before its accept."""

import socket
import struct
import threading

import pytest
import simlink
from fakerelay import FakeRelay
from helpers import chat
from test_framing import chats_of
from test_session import inbound, make_session

from stvwatch.net import client, handshake, netchan, receiver, supervisor, wire
from stvwatch.net import dump as dumpfmt
from stvwatch.stream.framing import frame
from stvwatch.stream.recording import Recording

CHALLENGE = 0x11223344


def reliable_single(seq, body, sub=0):
    w = wire.BitWriter()
    w.write_ubit(sub, wire.SUBCHANNEL_BITS)
    w.write_one_bit(1)
    w.write_one_bit(0)
    w.write_one_bit(0)
    w.write_varint32(len(body))
    w.write_bytes(body)
    w.write_one_bit(0)
    pad = (-w.nbits()) % 8
    w.write_ubit((1 << pad) - 1, pad)
    return netchan.build_packet(seq, 1, CHALLENGE, 0, reliable_region=w.get_bytes())


def test_handshake_outbound_is_not_an_ack():
    """The supervisor writes getchallenge/connect into the dump as
    DATAGRAM_OUT. They are connectionless: no sequence_ack, no
    in_reliable_state. A reliable packet left without an in-band ack (the
    session broke right after it) must stay undecided, not be judged by the
    bytes of a getchallenge nonce."""
    from stvwatch.net import messages

    rx = receiver.Receiver()
    rx.observe_outbound(netchan.build_ack(1, 0, CHALLENGE, 0))
    rx.feed(reliable_single(10, messages.signonstate_body(wire.SIGNON_SPAWN, 3)), 0)
    rx.observe_outbound(handshake.build_getchallenge(0x12345678))
    assert rx.counters["ack_unflipped"] == 0 and rx.counters["ack_flipped"] == 0
    assert rx.out_bits == 0


def test_auth_check_survives_one_lost_reply():
    """The precheck asks getchallenge before every connection. One lost UDP
    reply must not refuse a good relay as `auth_None`: A2S right next to it
    gets two attempts."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(10)
    port = srv.getsockname()[1]
    seen = []

    def serve():
        try:
            while len(seen) < 2:
                data, addr = srv.recvfrom(4096)
                seen.append(data)
                if len(seen) == 1:
                    continue  # first reply lost on the way
                nonce = struct.unpack_from("<I", data, 5)[0]
                srv.sendto(
                    b"\xff\xff\xff\xff"
                    + bytes([wire.S2C_CHALLENGE])
                    + struct.pack("<IiiI", wire.S2C_MAGICVERSION, 7, nonce, wire.AUTH_HASHEDCDKEY),
                    addr,
                )
        except OSError:
            pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        assert client.auth_proto("127.0.0.1:%d" % port) == wire.AUTH_HASHEDCDKEY
    finally:
        srv.close()
        t.join(5)


def test_a_datagram_from_another_address_never_reaches_the_session(tmp_path):
    """The checksum is unkeyed: a valid packet with a high sequence from any
    local socket would make every later relay packet stale."""
    path = str(tmp_path / "c.tvd")
    with FakeRelay() as r:
        opt = supervisor.Options(ip="127.0.0.1", port=r.port, build="10889068", crc=0xD9B6082D)
        sup = supervisor.Supervisor(opt, path, log=lambda *_a: None)
        th = threading.Thread(target=sup.run)
        th.start()
        try:
            assert r.wait(lambda: r.connected() == 1)
            ((addr, c),) = list(r.clients.items())
            spoof = netchan.build_packet(1_000_000, 0, c["relay"].challenge, 0)
            other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            other.sendto(spoof, addr)
            other.close()
            sent = c["sent"]
            assert r.wait(lambda: c["sent"] > sent + 10)
        finally:
            sup.stop = True
            th.join(30)
    got = [d for _t, t, d in dumpfmt.DumpReader(path) if t == dumpfmt.DATAGRAM_IN]
    assert spoof not in got


def test_a_packet_under_another_challenge_touches_no_state():
    s = make_session()
    s.feed(netchan.build_packet(1_000_000, 1, s.conn.challenge ^ 1, 0))
    s.feed(inbound(s, 2))
    assert (s.in_seq, s.counters.get("challenge_mismatch")) == (2, 1)


def long_chat():
    """A multi-fragment transfer (over 7 x 256 bytes) ending with a chat line:
    its first packet goes out as -2 parts."""
    w = wire.BitWriter()
    w.write_ubit(simlink.SVC_PRINT, wire.NETMSG_TYPE_BITS)
    w.write_string("x" * 3000)
    chat(w, "nick", "hello")
    return w.get_bytes()


@pytest.mark.parametrize("early, accept", [(False, True), (True, True), (True, False)])
def test_the_relays_first_packet_belongs_to_its_connection_in_any_order(tmp_path, early, accept):
    """The handshake keeps an in-band datagram that arrives before (or instead
    of) the accept; the supervisor has written it before the session marker.
    Replayed, it still opens the connection's first transfer."""
    path = str(tmp_path / "c.tvd")
    with FakeRelay(bodies=[long_chat()], early=early, accept=accept) as r:
        opt = supervisor.Options(ip="127.0.0.1", port=r.port, build="10889068", crc=0xD9B6082D)
        sup = supervisor.Supervisor(opt, path, log=lambda *_a: None)
        th = threading.Thread(target=sup.run)
        th.start()
        try:
            assert r.wait(lambda: r.connected() == 1)
            ((_addr, c),) = list(r.clients.items())
            assert r.wait(lambda: c["relay"].idle and c["sent"] > 20)
        finally:
            sup.stop = True
            th.join(30)
    kinds = [
        "start" if t == dumpfmt.SESSION_START else wire.classify(d)
        for _t, t, d in dumpfmt.DumpReader(path)
        if t in (dumpfmt.SESSION_START, dumpfmt.DATAGRAM_IN)
    ]
    fr, got = chats_of()
    for _ in frame(Recording(path), fr):
        pass
    assert (kinds.index("split") < kinds.index("start"), got) == (early, ["hello"])


@pytest.mark.parametrize("gap_ns, rc", [(2_000_000_000, 0), (2_004_000_000, 1)])
def test_the_gap_assertion_compares_the_measured_gap_not_its_rounding(tmp_path, gap_ns, rc):
    from stvwatch.net import main

    path = str(tmp_path / "g.tvd")
    with dumpfmt.DumpWriter(path, "127.0.0.1:27020", fsync_ms=0) as w:
        for t in (10**18, 10**18 + gap_ns):
            w.write(dumpfmt.DATAGRAM_IN, b"\0" * 8, t_ns=t)
    assert main.main(["read", path, "--assert-no-gaps-over-ms", "2000"]) == rc
