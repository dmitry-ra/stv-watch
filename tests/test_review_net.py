"""The client's precheck under a lost reply, the receiver's reading of our
own connectionless packets, and datagrams that are not the relay's."""

import socket
import struct
import threading

from fakerelay import FakeRelay
from test_session import inbound, make_session

from stvwatch.net import client, handshake, netchan, receiver, supervisor, wire
from stvwatch.net import dump as dumpfmt

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
