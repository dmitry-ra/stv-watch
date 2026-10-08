"""A loopback SourceTV relay that speaks just enough of the protocol for the
real client: A2S_INFO, getchallenge, connect, then a stream of in-band packets
from simlink.Relay, acks read back, net_Disconnect recorded.

It never reaches FULL (no signon messages are sent): what the tests ask of it
is the connection's life, not the game.
"""

import socket
import struct
import threading
import time

import simlink

from stvwatch.net import a2s, netchan, wire


def disconnect_reason(packet):
    """The reason of a net_Disconnect sent as the first unreliable message, else None."""
    try:
        h = netchan.decode_header(packet)
    except netchan.BadPacket:
        return None
    if h.reliable:
        return None
    br = wire.BitReader(packet)
    br.pos = h.body_offset * 8
    if br.bits_left() < wire.NETMSG_TYPE_BITS:
        return None
    if br.read_ubit(wire.NETMSG_TYPE_BITS) != wire.NET_DISCONNECT:
        return None
    return br.read_string()


class FakeRelay:
    def __init__(
        self, version="10889068", max_players=8, interval=0.02, cut_first_after=None, reject=None
    ):
        self.version = version
        self.reject = reject  # connect refused with this reason, as the engine does
        self.max_players = max_players
        self.interval = interval
        self.cut_first_after = cut_first_after
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.005)
        self.port = self.sock.getsockname()[1]
        self.addr = f"127.0.0.1:{self.port}"
        self.lock = threading.Lock()
        self.clients = {}  # addr -> dict(relay, sent, gone)
        self.connections = 0
        self.infos = 0
        self.challenges = 0
        self.refusals = 0
        self.disconnects = []  # (connection number, reason)
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop = True
        self._thread.join(5)
        self.sock.close()

    def connected(self):
        with self.lock:
            return sum(1 for c in self.clients.values() if not c["gone"])

    def wait(self, cond, timeout=20.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    # -- the loop ---------------------------------------------------------------
    def _run(self):
        next_send = time.monotonic()
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(65535)
                self._on(data, addr)
            except socket.timeout:
                pass
            except OSError:
                return
            if time.monotonic() >= next_send:
                next_send += self.interval
                self._send_all()

    def _send_all(self):
        with self.lock:
            live = [(a, c) for a, c in self.clients.items() if not c["gone"]]
        for addr, c in live:
            if c["n"] == 1 and self.cut_first_after is not None:
                if c["sent"] >= self.cut_first_after:
                    continue
            c["sent"] += 1
            for d in c["relay"].packet():
                self.sock.sendto(d, addr)

    def _on(self, data, addr):
        if data[:4] == b"\xff\xff\xff\xff":
            cmd = data[4:5]
            if data.startswith(a2s.INFO_QUERY):
                self.infos += 1
                self.sock.sendto(self._info(), addr)
            elif cmd == bytes([wire.A2S_GETCHALLENGE]):
                self.challenges += 1
                reply = (
                    b"\xff\xff\xff\xff"
                    + bytes([wire.S2C_CHALLENGE])
                    + struct.pack("<I", wire.S2C_MAGICVERSION)
                    + struct.pack("<I", 0x1000 + self.challenges)
                    + data[5:9]
                    + struct.pack("<I", wire.AUTH_HASHEDCDKEY)
                )
                self.sock.sendto(reply, addr)
            elif cmd == bytes([wire.C2S_CONNECT]) and self.reject is not None:
                self.refusals += 1
                nonce = data[17:21]
                refusal = b"\xff\xff\xff\xff" + bytes([wire.S2C_CONNREJECT]) + nonce
                self.sock.sendto(refusal + self.reject.encode() + b"\0", addr)
            elif cmd == bytes([wire.C2S_CONNECT]):
                challenge = struct.unpack_from("<I", data, 13)[0]
                with self.lock:
                    self.connections += 1
                    self.clients[addr] = {
                        "relay": simlink.Relay(challenge, []),
                        "sent": 0,
                        "gone": False,
                        "n": self.connections,
                    }
                self.sock.sendto(
                    b"\xff\xff\xff\xff" + bytes([wire.S2C_CONNECTION]) + b"\0" * 4, addr
                )
            return
        c = self.clients.get(addr)
        if c is None:
            return
        reason = disconnect_reason(data)
        if reason is not None:
            with self.lock:
                c["gone"] = True
                self.disconnects.append((c["n"], reason))
            return
        c["relay"].on_ack(data)

    def _info(self):
        players = self.connected()
        body = (
            bytes([a2s.S2A_INFO, 17])
            + b"Fake relay\0dm_test\0hl2mp\0Half-Life 2 Deathmatch\0"
            + struct.pack("<H", 320)
            + bytes([players, self.max_players, 0])
            + b"dl\x00\x01"
            + self.version.encode()
            + b"\0"
        )
        return a2s.HEADER + body
