"""Voice from the receive path to speech segments: every svc_VoiceData the
walk finds (plain, split, behind a reliable region, after a reconnect, in the
tail of a resend) reaches the voice list; segments are cut by clock and by the
protocol's own seq, never by arrival gaps alone."""

import pytest
from helpers import CHALLENGE, packet, reliable_packet, split, steamid64, write_recording
from voicegen import payload as voice_payload

from stvwatch.net import messages, netchan, wire
from stvwatch.stream.framing import Framer
from stvwatch.stream.recording import Datagram, Recording
from stvwatch.voice import steamvoice
from stvwatch.voice.segments import ChannelFrame, Segmenter


def synthetic_datagrams():
    """Two connections. Voice in plain packets, in split packets (parts
    reordered, one duplicated), behind an unaligned reliable region."""
    a, b = steamid64(1), steamid64(2)
    out = [packet(1, [(1, voice_payload(a, 0))])]
    big = packet(2, [(2, voice_payload(b, 0, n=12))])
    parts = split(big, group=5)
    assert len(parts) >= 3
    out += [parts[1], parts[0], parts[0]] + parts[2:]
    out.append(packet(3, [(1, voice_payload(a, 3))], reliable_first=True))
    # new connection: sequences restart, challenge differs
    out.append(packet(1, [(1, voice_payload(a, 6))], challenge=0x55667788))
    return out


@pytest.fixture
def synth(tmp_path):
    p = str(tmp_path / "synth.tvd")
    write_recording(
        p, [(1_000_000_000_000 + i * 15_000_000, d) for i, d in enumerate(synthetic_datagrams())]
    )
    return p


MS = 1_000_000


def test_split_padded_and_reconnected_voice_all_recovered(synth):
    fr = Framer()
    for dg in Recording(synth):
        fr.feed(dg)
    fr.finish()
    assert [len(v.data) > 0 for v in fr.voice] == [True] * 4
    assert [v.via_split for v in fr.voice] == [False, True, False, False]
    assert fr.counters["voice_crc_bad"] == 0
    assert fr.counters["challenge_change"] == 1
    assert fr.splits.counters["split_duplicate"] == 1
    assert {f.fate for f in fr.fates} == {"ok"}


def test_legacy_policy_loses_exactly_the_split_voice(synth):
    fr = Framer(reassemble_splits=False)
    for dg in Recording(synth):
        fr.feed(dg)
    assert [v.via_split for v in fr.voice] == [False, False, False]


def frames(sid, t_ms, kinds):
    """kinds: ints are opus seqs, 'R' a reset mark, ('S', n) silence."""
    out = []
    for k in kinds:
        if k == "R":
            f = steamvoice.Frame("reset")
        elif isinstance(k, tuple):
            f = steamvoice.Frame("silence", value=k[1])
        else:
            f = steamvoice.Frame("opus", seq=k, data=b"x")
        out.append(ChannelFrame(sid, t_ms * MS, 0, 0, 1, f))
    return out


def run_segmenter(batches, end_ms, close_s=1.0):
    seg = Segmenter(close_s=close_s)
    for t_ms, cfs in sorted(batches, key=lambda b: b[0]):
        seg.clock(t_ms * MS)
        for cf in cfs:
            seg.feed(cf)
    seg.clock(end_ms * MS)
    seg.flush(end_ms * MS)
    return seg


def test_reset_mark_mid_spurt_does_not_cut_the_phrase():
    a = 7
    seg = run_segmenter(
        [(0, frames(a, 0, ["R", 0, 1, 2])), (60, frames(a, 60, [3, 4, "R", 5, 6]))], end_ms=5000
    )
    assert [s.opus_frames for s in seg.closed] == [7]


def test_speech_end_is_decided_by_the_clock_not_by_the_next_packet():
    """The closing time must not wait for another packet: a silent channel is
    closed close_s after its last frame, while other traffic keeps flowing."""
    a, b = 7, 8
    batches = [(0, frames(a, 0, [0, 1, 2]))]
    batches += [(t, frames(b, t, [t // 100])) for t in range(100, 5000, 100)]
    seg = run_segmenter(batches, end_ms=5000)
    first = [s for s in seg.closed if s.steamid64 == a][0]
    assert first.close_reason == "clock"
    assert 1000 * MS < first.close_ns - first.last_rx_ns <= 1100 * MS


def test_seq_restart_splits_and_late_tail_is_kept_as_continuation():
    a = 7
    seg = run_segmenter(
        [
            (0, frames(a, 0, [0, 1, 2])),
            (3000, frames(a, 3000, [3, 4])),  # late, same spurt
            (3100, frames(a, 3100, [0, 1])),
        ],  # new key press
        end_ms=9000,
    )
    assert [(s.first_seq, s.last_seq, s.continuation_of) for s in seg.closed] == [
        (0, 2, -1),
        (3, 4, 0),
        (0, 1, -1),
    ]
    assert sum(s.opus_frames for s in seg.closed) == seg.counters["frames_in_opus"]


def test_speakers_are_separate_channels_by_steamid_not_slot():
    a, b = 7, 8
    fa = frames(a, 0, [0, 1])
    fb = [ChannelFrame(b, cf.t_ns, 0, 0, 1, cf.frame) for cf in frames(b, 0, [0, 1])]
    seg = run_segmenter([(0, fa + fb)], end_ms=5000)
    assert sorted((s.steamid64, s.opus_frames) for s in seg.closed) == [(7, 2), (8, 2)]


def test_resend_is_recognized_by_our_acks_and_its_tail_still_read():
    """A reliable packet our ack did not flip comes again on the same
    subchannel with the same region: skipped (also when unacked again), its
    unreliable tail read; a different region on that subchannel is new data."""
    a = steamid64(1)
    x = messages.signonstate_body(wire.SIGNON_SPAWN, 3)
    y = messages.signonstate_body(wire.SIGNON_PRESPAWN, 4)
    streams = []
    fr = Framer(
        on_packet=lambda p: streams.extend(w.payload for w in p.walks if w.stream == "reliable")
    )

    def ack(out_seq, seq, bits=0):
        fr.observe(0, 2, netchan.build_packet(out_seq, seq, CHALLENGE, bits))

    ack(1, 0)
    for i, (seq, body) in enumerate([(10, x), (11, x), (12, x), (13, y)]):
        fr.feed(Datagram(i, i, 0, reliable_packet(seq, body, [(1, voice_payload(a, 3 * i))])))
        ack(2 + i, seq)  # never flipped: the live client lost it
    assert streams == [x, y]
    assert len(fr.voice) == 4
    assert fr.counters["resend_skipped"] == 2 and fr.counters["resend_mismatch"] == 1
    assert [f.fate for f in fr.fates] == ["ok", "resend", "resend", "ok"]
    assert fr.counters["ack_unflipped"] == 4  # every copy, resends too
