"""The live client against a simulated relay over a faulty link (simlink.py).

What must hold, with loss, duplicates, reordering and lost `-2` parts: every
reliable message is delivered exactly once and in order, the reliable channel
does not stall, nothing raises. A client that drops `-2` stalls on the first
split batch; the recording it leaves, read offline, still gives every message
exactly once (resends recognized by our recorded acks).
"""

import pytest
import simlink
from test_session import FakeConn, FakeSink

from stvwatch.net import dump, supervisor
from stvwatch.net import session as sess
from stvwatch.stream import framing
from stvwatch.stream.recording import Recording

TEXTS = [f"m{i:03d} " + "x" * (40 + (i * 397) % 2500) for i in range(40)]


def client(legacy=False):
    s = sess.Session(FakeConn(), supervisor.Options(crc=0xD9B6082D), FakeSink())
    if legacy:
        s.rx.reassemble_splits = False
    got = []
    feed = s.rx.feed

    def tapped(data, t_ns, index=None):
        pkt = feed(data, t_ns, index)
        for w in (pkt.walks if pkt else ()):
            if w.stream == "reliable":
                got.extend(
                    simlink.read_print(w.payload, st)
                    for mid, st, _e, _f in w.msgs
                    if mid == simlink.SVC_PRINT
                )
        return pkt

    s.rx.feed = tapped
    return s, got


def drive(
    seed,
    legacy=False,
    loss=0.15,
    dup=0.1,
    jitter=0.03,
    part_loss=0.1,
    steps=6000,
    record=None,
    events=None,
):
    rng = simlink.seeded(seed)
    s, got = client(legacy)
    relay = simlink.Relay(s.conn.challenge, [simlink.print_body(t) for t in TEXTS])
    down = simlink.Link(rng, loss, dup, jitter_s=jitter, part_loss=part_loss)
    up = simlink.Link(rng, loss, dup, jitter_s=jitter)
    kw = {}
    if record is not None:
        w = dump.DumpWriter(record, "127.0.0.1:27020", fsync_ms=0)
        kw = {
            "on_inbound": lambda d, t: w.write(dump.DATAGRAM_IN, d, t_ns=t),
            "on_outbound": lambda p, t: w.write(dump.DATAGRAM_OUT, p, t_ns=t),
        }
    n = simlink.run(s, relay, down, up, steps, events=events, **kw)
    if record is not None:
        w.close()
    return s, got, relay, n, (down, up)


@pytest.mark.parametrize("seed", range(6))
def test_reliable_exactly_once_in_order_over_a_faulty_link(seed):
    s, got, relay, n, (down, _up) = drive(seed)
    assert got == TEXTS, f"seed {seed}: {len(got)} of {len(TEXTS)}"
    assert relay.idle and n < 6000  # no stall
    assert down.parts_dropped and down.dropped and down.duplicated
    assert relay.resends > 0  # the faults did bite
    assert s.counters.get("reliable_parse_error", 0) == 0


def test_client_dropping_split_stalls_and_offline_replay_dedups(tmp_path):
    rec = str(tmp_path / "legacy.tvd")
    s, got, relay, n, _ = drive(
        1, legacy=True, loss=0.0, dup=0.0, jitter=0.0, part_loss=0.0, steps=600, record=rec
    )
    first_split = next(i for i, t in enumerate(TEXTS) if len(t) > 1100)
    assert got == TEXTS[:first_split] and not relay.idle and n == 600
    assert relay.resends > 50  # resent into a void

    off = []

    def on_packet(pkt):
        for w in pkt.walks:
            if w.stream == "reliable":
                off.extend(
                    simlink.read_print(w.payload, st)
                    for mid, st, _e, _f in w.msgs
                    if mid == simlink.SVC_PRINT
                )

    fr = framing.Framer(on_packet=on_packet)
    for _ in framing.frame(Recording(rec), fr):
        pass
    assert off == TEXTS[: first_split + 1]
    assert fr.counters["resend_skipped"] == relay.resends
    # without our acks the reader cannot tell a resend from new data
    fr2 = framing.Framer(on_packet=lambda p: None)
    for dg in Recording(rec):
        fr2.feed(dg)
    assert fr2.counters["resend_skipped"] == 0


def test_changelevel_clears_an_unacked_batch_only_for_a_stalled_client():
    """Seen live: a chat line queued behind an unacked split batch was lost
    when the map changed. A client that joins -2 acks the batch, so the line
    behind it is out before the change."""
    at = {300: lambda r: r.changelevel()}
    _s, got_new, *_ = drive(2, loss=0.0, dup=0.0, jitter=0.0, part_loss=0.0, steps=301, events=at)
    _s, got_old, *_ = drive(
        2, legacy=True, loss=0.0, dup=0.0, jitter=0.0, part_loss=0.0, steps=301, events=at
    )
    assert got_new == TEXTS
    first_split = next(i for i, t in enumerate(TEXTS) if len(t) > 1100)
    assert got_old == TEXTS[:first_split]


def test_recorded_acks_tell_the_two_clients_apart(tmp_path):
    """Offline, our recorded acks show which client joined -2: every delivered
    packet flipped for one, split packets left unflipped for the other."""
    new, old = str(tmp_path / "new.tvd"), str(tmp_path / "old.tvd")
    drive(3, loss=0.0, dup=0.0, jitter=0.0, part_loss=0.0, record=new)
    drive(3, legacy=True, loss=0.0, dup=0.0, jitter=0.0, part_loss=0.0, steps=300, record=old)
    counts = []
    for path in (new, old):
        fr = framing.Framer()
        for _ in framing.frame(Recording(path), fr):
            pass
        counts.append(fr.counters)
    a, b = counts
    assert a["ack_unflipped"] == 0 and a["ack_flipped_split"] > 0
    assert b["ack_unflipped_split"] > 0 and b["resend_skipped"] > 0
