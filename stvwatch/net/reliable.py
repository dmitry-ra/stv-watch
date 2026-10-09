#!/usr/bin/env python3
"""Reliable subchannel: block parsing and multi-fragment transfer reassembly.

Acking needs only the 3-bit subchannel index (see netchan.latch_subchannel).
Reassembly is needed for a different reason: `svc_ServerInfo` (which carries the
spawncount every signon echo must quote) and the `svc_SignonState` pushes arrive
inside multi-fragment transfers. Without reassembly the ladder cannot be climbed.

Region layout, immediately after the netchannel header, when flags & RELIABLE:

    ubit(3)  subchannel index
    per stream (0 = messages, 1 = files):
      bit  data-follows?
      if set:
        bit  0 => single block, 1 => multi-fragment
        if multi:  ubit(18) start_fragment | ubit(3) num_fragments
        if start_fragment == 0:          # first fragment carries the header
          if single:
            bit compressed? -> ubit(26) uncompressed_size
            varint32 nbytes              # NOTE: varint here...
          else:
            bit is_file? -> ubit(32) transferID + filename string
            bit compressed? -> ubit(26) uncompressed_size
            ubit(26) nbytes              # ...but a fixed 26-bit field here
        <num_fragments * 256 bytes of payload, tail-trimmed on the last one>

The single/multi size-field asymmetry (varint32 vs ubit(26)) is real; swapping
them misaligns everything downstream.
"""

from . import codec, wire

FRAGMENT_BITS = 8
FRAGMENT_SIZE = 1 << FRAGMENT_BITS  # 256
MAX_FILENAME = 260
MAX_TRANSFER_BYTES = 4 << 20  # cap: refuse absurd declared sizes


class ReliableError(Exception):
    pass


def _ceil_frags(nbytes):
    return (nbytes + FRAGMENT_SIZE - 1) // FRAGMENT_SIZE


class Stream:
    """One reliable stream's in-flight transfer."""

    __slots__ = ("nbytes", "total_frags", "have", "buf", "compressed")

    def __init__(self):
        self.reset()

    def reset(self):
        self.nbytes = 0
        self.total_frags = 0
        self.have = 0  # bitmask of fragments actually received
        self.buf = bytearray()
        self.compressed = False

    @property
    def active(self):
        return self.total_frags > 0

    def mark(self, start_frag, num_frags):
        """Record which fragments arrived, not how many.

        A counter over-counts a resent fragment -- which is exactly what happens
        whenever one of our acks is lost -- and the transfer then completes one
        packet early with the still-missing 256 bytes left as zeros. The hole
        lands preferentially in the large signon transfers, where it desyncs the
        chain walk and costs a whole session."""
        self.have |= ((1 << num_frags) - 1) << start_frag

    def complete(self):
        # Exact mask, not popcount>=: a corrupt num_frags that sets bits past the
        # end must NOT satisfy completion with a hole below. A stalled transfer is
        # recovered by the reliable-dead breaker; a hole would ship zeros as data.
        return self.active and self.have == (1 << self.total_frags) - 1


class Reassembler:
    """Per-connection reliable reassembly. One instance per session; drop it on
    reconnect - transfer state is meaningless across a fresh handshake."""

    def __init__(self):
        self.streams = [Stream() for _ in range(wire.MAX_STREAMS)]
        self.completed = 0
        self.aborted = 0
        self.end_bit = None  # bit offset just past the region feed() parsed

    def feed(self, data, header):
        """Parse the reliable region of one packet.

        Returns (subchannel_index, [completed message-stream payloads]).

        This function does NOT decide the ack. Per CNetChan::ProcessPacket the
        receiver flips m_nInReliableState once per received reliable PACKET, not
        once per completed block - see session._handle_reliable, which owns that
        decision and skips the flip only when this call raises.

        Raises ReliableError on a malformed region.
        """
        br = wire.BitReader(data)
        br.pos = header.body_offset * 8
        self.end_bit = None
        subchannel = br.read_ubit(wire.SUBCHANNEL_BITS)
        payloads = []
        for idx in range(wire.MAX_STREAMS):
            if not br.read_one_bit():
                continue
            done = self._read_block(br, self.streams[idx])
            if done is not None and idx == 0:  # stream 1 is the file stream
                payloads.append(done)
        # Where the unreliable stream begins. Left None on any raise above, so a
        # caller cannot walk a tail whose start we never established.
        self.end_bit = br.pos
        return subchannel, payloads

    def _read_block(self, br, st):
        multi = br.read_one_bit()
        if multi:
            start_frag = br.read_ubit(18)
            num_frags = br.read_ubit(3)
        else:
            start_frag = 0
            num_frags = 0

        if start_frag == 0:  # transfer header present
            st.reset()
            if not multi:
                st.compressed = bool(br.read_one_bit())
                if st.compressed:
                    br.read_ubit(26)  # uncompressed size
                st.nbytes = br.read_varint32()
                num_frags = _ceil_frags(st.nbytes)
            else:
                if br.read_one_bit():  # is_file
                    br.read_ubit(32)  # transferID
                    br.read_string(MAX_FILENAME)
                st.compressed = bool(br.read_one_bit())
                if st.compressed:
                    br.read_ubit(26)  # uncompressed size
                st.nbytes = br.read_ubit(26)
            if st.nbytes > MAX_TRANSFER_BYTES:
                self.aborted += 1
                st.reset()
                raise ReliableError(f"declared transfer {st.nbytes}B over cap")
            st.total_frags = _ceil_frags(st.nbytes)

        if not st.active:
            # A continuation fragment for a transfer that began before we
            # attached. Unreadable by definition - skip its bytes and move on.
            raise ReliableError("continuation of an unseen transfer")

        if start_frag >= st.total_frags:
            # start_frag is an 18-bit field, so an out-of-range one would size the
            # buffer below to start_frag*256 -- up to 67 MB per stream, allocated
            # from a single corrupt packet and held until the transfer resets.
            self.aborted += 1
            st.reset()
            raise ReliableError(f"fragment {start_frag} past transfer end {st.total_frags}")

        length = num_frags * FRAGMENT_SIZE
        if start_frag + num_frags >= st.total_frags:
            trim = st.nbytes % FRAGMENT_SIZE
            if trim:
                length -= FRAGMENT_SIZE - trim
        chunk = br.read_bytes(length)

        offset = start_frag * FRAGMENT_SIZE
        if len(st.buf) < offset + len(chunk):
            st.buf.extend(b"\x00" * (offset + len(chunk) - len(st.buf)))
        st.buf[offset : offset + len(chunk)] = chunk
        st.mark(start_frag, num_frags)

        if st.complete():
            out = bytes(st.buf[: st.nbytes])
            was_compressed = st.compressed
            st.reset()
            self.completed += 1
            if was_compressed:
                try:
                    return codec.inflate_compressed(out)
                except Exception as e:  # noqa: BLE001
                    self.aborted += 1
                    raise ReliableError(f"inflate failed: {e}") from e
            return out
        return None
