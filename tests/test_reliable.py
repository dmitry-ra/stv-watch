"""Reliable-reassembly tests.

Targets the two accounting fixes that a recording cannot force on demand: a
duplicate fragment must not complete a transfer early (bitmask, not counter),
and an out-of-range start_frag must be refused before it sizes a 67 MB buffer.
"""

import pytest

from stvwatch.net import messages, netchan, reliable, wire

# --- fragment accounting (bitmask, not counter) ---------------------------


def test_duplicate_fragment_does_not_complete_a_transfer_early():
    st = reliable.Stream()
    st.nbytes = 3 * reliable.FRAGMENT_SIZE
    st.total_frags = 3
    st.mark(0, 1)
    st.mark(0, 1)  # a resend of fragment 0 (lost ack)
    assert not st.complete()  # a counter would say 2 == "2 of 3"? no,
    #                                       it would count 2 and, with the third,
    #                                       fire at 3 having never seen frag 2.
    st.mark(1, 1)
    st.mark(0, 1)  # another duplicate for good measure
    assert not st.complete()  # still missing fragment 2
    st.mark(2, 1)
    assert st.complete()  # only now, with all three distinct


def test_multi_fragment_block_marks_a_contiguous_run():
    st = reliable.Stream()
    st.nbytes = 5 * reliable.FRAGMENT_SIZE
    st.total_frags = 5
    st.mark(0, 3)  # fragments 0,1,2 in one block
    assert not st.complete()
    st.mark(3, 2)  # fragments 3,4
    assert st.complete()


def test_overshoot_past_the_end_does_not_falsely_complete():
    # A corrupt num_frags sets bits beyond total_frags. Exact-mask completion must
    # reject it (popcount>= would have accepted a count that includes stray high
    # bits), so the transfer stalls and recovers rather than shipping a hole.
    st = reliable.Stream()
    st.nbytes = 3 * reliable.FRAGMENT_SIZE
    st.total_frags = 3
    st.mark(0, 1)  # fragment 0
    st.mark(1, 5)  # corrupt: claims 1..5, past the 3-frag end
    assert not st.complete()  # have has high bits set -> != full mask


# --- start_frag bounds guard -----------------------------------------------


def _multi_region(subchannel, start_frag, num_frags, total_bytes):
    """A reliable region whose message stream carries a multi-fragment block at
    an arbitrary start_frag. Only the fields the reader consumes are written."""
    w = wire.BitWriter()
    w.write_ubit(subchannel, wire.SUBCHANNEL_BITS)
    w.write_one_bit(1)  # stream 0: data follows
    w.write_one_bit(1)  # multi-fragment
    w.write_ubit(start_frag, 18)
    w.write_ubit(num_frags, 3)
    if start_frag == 0:  # header only on the first fragment
        w.write_one_bit(0)  # not a file
        w.write_one_bit(0)  # not compressed
        w.write_ubit(total_bytes, 26)
    w.write_bytes(b"\x00" * (num_frags * reliable.FRAGMENT_SIZE))
    w.write_one_bit(0)  # stream 1: nothing
    return w.get_bytes()


def _feed(reasm, region):
    pkt = netchan.build_packet(5, 1, 0xABCD, 0, reliable_region=region)
    header = netchan.decode_header(pkt)
    return reasm.feed(pkt, header)


def test_start_frag_past_transfer_end_is_refused():
    ra = reliable.Reassembler()
    # Open a 3-fragment transfer but deliver only fragment 0, so it stays ACTIVE
    # (an already-complete transfer would trip the earlier "unseen" guard first).
    # Then send a continuation naming a wildly out-of-range fragment: without the
    # bounds check this sizes a ~67 MB buffer.
    _feed(ra, _multi_region(0, start_frag=0, num_frags=1, total_bytes=3 * reliable.FRAGMENT_SIZE))
    huge = _multi_region(0, start_frag=200000, num_frags=1, total_bytes=0)
    with pytest.raises(reliable.ReliableError, match="past transfer end"):
        _feed(ra, huge)
    assert ra.aborted >= 1


def test_declared_transfer_over_cap_is_refused():
    ra = reliable.Reassembler()
    over = reliable.MAX_TRANSFER_BYTES + 1
    with pytest.raises(reliable.ReliableError, match="over cap"):
        _feed(ra, _multi_region(0, start_frag=0, num_frags=1, total_bytes=over))


# --- single-block roundtrip (the signon-carrying path) ---------------------


def test_single_block_reassembles_to_its_payload():
    ra = reliable.Reassembler()
    body = messages.signonstate_body(wire.SIGNON_SPAWN, 7)
    region = messages.reliable_region(body, subchannel=0)
    _sub, done = _feed(ra, region)
    assert done == [body]
    assert ra.completed == 1
