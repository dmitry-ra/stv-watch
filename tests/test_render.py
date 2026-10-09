"""The terminal layout, the traffic state and the replay pacer."""

import io

from stvwatch import version
from stvwatch.model import NS, Channel, Traffic
from stvwatch.render import Screen, clean, fit, width
from stvwatch.source import Pacer


def test_external_text_cannot_drive_the_terminal_and_cuts_by_cells():
    nick = "a\x1b[2Jb\u202ec\u200dd"  # ESC, bidi override, ZWJ
    assert clean(nick) == "a?[2Jbcd"
    spans, used = fit([("ab\u4e2d\u6587", "")], 5)  # 2 + 2 + 2 cells > 5
    assert "".join(t for t, _s in spans) == "ab\u4e2d~" and used == 5
    assert width("e\u0301") == 1  # combining mark


class FakeTty(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        raise OSError("no fd")


def test_block_is_pinned_to_the_bottom_rows_and_the_feed_scrolls_above_it():
    out = FakeTty()
    size = [(80, 24)]
    sc = Screen(out=out, color=False, size=lambda: size[0])
    block = [[("HEAD", "")], [("line2", "")], [("line3", "")]]
    sc.draw(block)
    first = out.getvalue()
    # the shell's lines are pushed above the block rows, region 1..21,
    # block on rows 22..24 by absolute address, cursor parked on row 22
    assert first.startswith("\r\n\n\n\x1b[1;21r")
    assert "\x1b[22;1HHEAD\x1b[K\x1b[23;1Hline2\x1b[K\x1b[24;1Hline3\x1b[K" in first
    assert first.endswith("\x1b[22;1H")
    n = len(out.getvalue())
    sc.feed([("feed", "")])
    sc.draw(block)
    assert out.getvalue()[n:].startswith("\x1b[21;1H\nfeed\x1b[K\x1b[22;1H")
    # a fourth block line: the feed moves up one row first, then the region shrinks
    n = len(out.getvalue())
    sc.draw(block + [[("line4", "")]])
    assert out.getvalue()[n:].startswith("\x1b[21;1H\n\x1b[1;20r")
    n = len(out.getvalue())
    sc.draw(block)
    assert "r" not in out.getvalue()[n:].replace("\x1b[K", "") and sc.block_h == 4
    sc.stop([[("FINAL", "")]])
    tail = out.getvalue()
    assert tail.rindex("\x1b[r") > tail.rindex("\x1b[1;20r")
    assert tail.endswith("\x1b[21;1HFINAL\x1b[K\n\x1b[?25h")


def live_screen(cols=80, rows=24):
    out = FakeTty()
    size = [(cols, rows)]
    sc = Screen(out=out, color=False, size=lambda: size[0])
    sc.draw([[("H", "")], [("h2", "")], [("h3", "")]])  # region 1..rows-3
    return sc, out, size


def step(sc, out, ops):
    n = len(out.getvalue())
    ops(sc)
    sc.draw([[("H", "")], [("h2", "")], [("h3", "")]])
    return out.getvalue()[n:]


def test_live_lines_are_rewritten_in_place_as_the_feed_scrolls_them_up():
    sc, out, _size = live_screen()
    got = step(
        sc,
        out,
        lambda s: (
            s.live_open("a", [("A talking", "")]),
            s.live_open("b", [("B talking", "")]),
            s.feed([("event", "")]),
        ),
    )
    assert got.startswith(
        "\x1b[21;1H\nA talking\x1b[K\x1b[21;1H\nB talking\x1b[K" "\x1b[21;1H\nevent\x1b[K"
    )
    # two speakers updated where they now are: A two rows up, B one
    got = step(
        sc, out, lambda s: (s.live_update("a", [("A 2s", "")]), s.live_update("b", [("B 1s", "")]))
    )
    assert got.startswith("\x1b[19;1HA 2s\x1b[K\x1b[20;1HB 1s\x1b[K")
    # a final that wraps (100 cells on 80 columns, 2 rows): rows 1..19 scroll
    # up one inside a temporary region, B and the event under A stay put,
    # then the region is restored
    got = step(sc, out, lambda s: s.live_close("a", [("x" * 100, "")]))
    assert got.startswith("\x1b[1;19r\x1b[19;1H\n\x1b[18;1H" + "x" * 100 + "\x1b[K\x1b[1;21r")
    got = step(sc, out, lambda s: s.live_update("b", [("B 2s", "")]))
    assert got.startswith("\x1b[20;1HB 2s\x1b[K")


def test_a_wrapping_final_moves_the_live_lines_above_it_up_with_the_room_it_takes():
    sc, out, _size = live_screen()
    step(
        sc,
        out,
        lambda s: (
            s.live_open("c", [("C talking", "")]),
            s.live_open("a", [("A talking", "")]),
            s.feed([("event", "")]),
        ),
    )
    step(sc, out, lambda s: s.live_close("a", [("x" * 100, "")]))
    got = step(sc, out, lambda s: s.live_update("c", [("C 2s", "")]))
    assert got.startswith("\x1b[18;1HC 2s\x1b[K")


def test_a_live_line_that_scrolled_away_comes_back_at_the_bottom():
    sc, out, _size = live_screen(80, 7)  # region 1..4
    step(
        sc,
        out,
        lambda s: (
            s.live_open("a", [("A talking", "")]),
            [s.feed([("e%d" % i, "")]) for i in range(4)],
        ),
    )
    got = step(sc, out, lambda s: s.live_close("a", [("A: text", "")], cont=[("^ ", "")]))
    assert got.startswith("\x1b[4;1H\n^ A: text\x1b[K")
    # a live line is cut to one row, so the rows under it stay countable
    got = step(sc, out, lambda s: s.live_open("b", [("B" * 100, "")]))
    assert got.startswith("\x1b[4;1H\n" + "B" * 78 + "~\x1b[K")
    step(sc, out, lambda s: [s.feed([("f%d" % i, "")]) for i in range(4)])
    got = step(sc, out, lambda s: s.live_update("b", [("B 3s", "")]))
    assert got.startswith("\x1b[4;1H\nB 3s\x1b[K")


def test_resize_repaints_the_feed_from_memory_and_moves_live_lines():
    sc, out, size = live_screen(80, 24)
    step(
        sc,
        out,
        lambda s: (
            s.feed([("old", "")]),
            s.live_open("a", [("A talking", "")]),
            s.feed([("y" * 50, "")]),
        ),
    )
    size[0] = (40, 12)  # region 1..9
    got = step(sc, out, lambda s: s.live_update("a", [("A 3s", "")]))
    # no erase-below (tmux copies the screen into its history on that):
    # every row erased by itself, then the newest lines that fit, bottom up
    assert got.startswith(
        "\x1b[r\x1b[1;9r" + "".join("\x1b[%d;1H\x1b[2K" % r for r in range(1, 13))
    )
    assert "\x1b[J" not in got
    # the 50-cell line takes rows 8-9 at 40 columns, so A is on 7, "old" on 6
    assert (
        "\x1b[8;1H" + "y" * 50 + "\x1b[K\x1b[7;1HA talking\x1b[K\x1b[6;1Hold\x1b[K"
        "\x1b[7;1HA 3s\x1b[K"
    ) in got


def test_not_a_terminal_gets_only_finished_lines():
    out = io.StringIO()
    sc = Screen(out=out, color=False)
    sc.live_open("a", [("A talking", "")])
    sc.feed([("event", "")])
    sc.live_update("a", [("A 2s", "")])
    sc.draw([])
    sc.live_close("a", [("A: text", "")])
    sc.draw([])
    assert out.getvalue() == "event\nA: text\n"


def test_the_rule_is_a_thin_coloured_line_on_the_normal_background():
    bar = [[(" stv-watch ", "bar")], [("HEAD", "")]]
    out = FakeTty()
    Screen(out=out, color=True, size=lambda: (40, 10)).draw(bar)
    rule = "\u2500"
    assert (
        "\x1b[9;1H\x1b[34m"
        + rule * 2
        + "\x1b[0m\x1b[1;34m stv-watch \x1b[0m\x1b[34m"
        + rule * 26
        + "\x1b[0m\x1b[10;1HHEAD"
    ) in out.getvalue()
    out = FakeTty()
    Screen(out=out, color=False, size=lambda: (40, 10)).draw(bar)
    assert "\x1b[9;1H-- stv-watch " + "-" * 26 + "\x1b[K" in out.getvalue()


def test_block_height_is_fixed_whatever_the_state(tmp_path):
    from stvwatch.app import App
    from stvwatch.cli import parse_args

    app = App(parse_args(["--replay", "x.tvd", "--out", str(tmp_path), "--debug"]))
    app.pacer = Pacer(False)
    empty = len(app.block())
    app.conn.state, app.conn.full_ns, app.conn.sessions = "FULL", 1, 3
    app.conn.hostname, app.conn.map = "host", "dm_test"
    app.traffic.inbound(10, 100, True)
    for sid in range(3):
        app.channels[sid] = Channel(sid, 0)
        app.channels[sid].talking = True
    assert len(app.block()) == empty == app.BLOCK_LINES == 6
    assert app.block()[0] == [(f" stv-watch {version.version()} ", "bar")]


def test_traffic_reports_each_transition_once_and_learns_an_idle_pace():
    t = Traffic(quiet_s=1.5)
    t.inbound(0, 100, False)
    assert t.check(0) == ("started", 0.0)
    for i in range(1, 10):
        t.inbound(i * NS // 10, 100, False)
    assert t.check(5 * NS) == ("stopped", 4.1)
    assert t.check(6 * NS) is None
    t.inbound(6 * NS, 100, False)
    assert t.check(6 * NS) == ("resumed", 5.1)
    # an idle relay paces snapshots 2 s apart: after a few gaps, not a stop
    idle = Traffic(quiet_s=1.5)
    events = []
    for i in range(40):
        idle.inbound(i * 2 * NS, 100, False)
        events.append(idle.check(i * 2 * NS))
        events.append(idle.check(i * 2 * NS + NS * 19 // 10))
    assert [e for e in events[40:] if e] == []


def test_pacer_holds_items_to_recorded_pace_after_the_skip():
    p = Pacer(False, speed=2.0, skip_s=10.0)
    assert p.due(0) == 0.0 and p.skipping(5 * NS)
    assert p.due(9 * NS) == 0.0  # skipped: not paced
    assert 1.9 < p.due(13 * NS) <= 2.0  # 4 s after the last skipped, at x2
