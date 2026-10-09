"""Line-mode terminal output: a scrolling feed above a status block pinned to
the bottom rows of the screen, the way coding agents lay out their terminal.

The feed lives in a scroll region (DECSTBM, rows 1..rows-H): each line is
written at the region's bottom after a line feed, so the region scrolls and
the lines that leave its top go to the terminal's own history (a region whose
top is row 1 keeps history in tmux and xterm). The block is drawn in the H rows
under the region by absolute addressing and never scrolls, so the history
holds feed lines only, no copies of the block.

The block starts with a thin coloured rule (U+2500 on the normal background).
Its height is fixed by the caller; should it grow anyway (a smaller terminal
cut it before), the feed is scrolled up by the difference first, so no feed
line is overwritten. Block lines are cut to width - 1, so nothing autowraps
outside the region.

Live lines: a feed line that keeps changing after it was printed (a speaker's
progress). Its row is known as the distance from the region's bottom, which
every later feed line increases by the rows it takes. While live it is cut to
one row, so the distance stays exact; updates rewrite that row in place. When
it closes, the final text, which may wrap, replaces it: the rows above it are
scrolled up inside a temporary region 1..row to make room, so the lines under
it do not move. A live line that has scrolled out of the region cannot be
addressed any more: its next update, or its final text, is printed as a new
line at the bottom (the final one marked as a continuation).

A resize reflows the screen unpredictably (tmux rewraps lines, moves rows to
and from its history), so the feed region is not patched but repainted from
the last lines kept in memory, and live lines get their new rows from that.
"""

import collections
import os
import select
import signal
import sys
import unicodedata

RESET = "\x1b[0m"
STYLES = {
    "": "",
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
    "inv": "\x1b[7m",
    "rule": "\x1b[34m",
    "rulelabel": "\x1b[1;34m",
    # feed lines by weight: joins and leaves in 256-colour shades of their
    # own, apart from the plain green and red of our connection lines
    "boldyellow": "\x1b[1;33m",
    "joingreen": "\x1b[38;5;71m",
    "leavered": "\x1b[38;5;167m",
    "gray": "\x1b[2;90m",
}


def clean(text):
    """External text (player names, ASR output) -> printable: control and
    format characters (ESC, CR, bidi overrides, ZWJ) become nothing or '?'.
    A nick is chosen by a stranger and must not drive the terminal."""
    out = []
    for ch in str(text):
        cat = unicodedata.category(ch)
        if cat == "Cc":
            out.append(" " if ch in "\t\n\r" else "?")
        elif cat in ("Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    return "".join(out)


def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(text):
    return sum(char_width(c) for c in text)


def fit(spans, cols):
    """Styled spans [(text, style)] cut to `cols` display cells.
    -> (spans, used_cells). A cut line ends in '~'."""
    total = sum(width(t) for t, _s in spans)
    if total <= cols:
        return spans, total
    room = max(0, cols - 1)
    out, used = [], 0
    for text, style in spans:
        piece = []
        for ch in text:
            w = char_width(ch)
            if used + w > room:
                break
            piece.append(ch)
            used += w
        if piece:
            out.append(("".join(piece), style))
        if used >= room or len(piece) < len(text):
            break
    out.append(("~", "dim"))
    return out, used + 1


def tail(text, cols):
    """The end of `text` in at most `cols` cells: '...' and the last whole
    words. A growing line (live transcript) is read at its end."""
    if width(text) <= cols:
        return text
    room = max(0, cols - 3)
    out, used = [], 0
    for ch in reversed(text):
        w = char_width(ch)
        if used + w > room:
            break
        out.append(ch)
        used += w
    kept = "".join(reversed(out))
    cut = kept.find(" ")
    if 0 <= cut < len(kept) - 1:
        kept = kept[cut + 1 :]
    return "..." + kept


RULE = "\u2500"  # box drawing light horizontal


def bar_line(spans, cols, color=True):
    """The separator from a line whose last span has style "bar": its text as
    a label in colour on the normal background, then a thin coloured rule to
    cols - 1. Without colour the rule is '-'."""
    label = "".join(t for t, _s in spans)
    rest = max(0, cols - 1 - width(label) - 2)
    if not color:
        return ("--" + label + "-" * rest)[: max(0, cols - 1)]
    return to_ansi(
        fit([(RULE * 2, "rule"), (label, "rulelabel"), (RULE * rest, "rule")], cols - 1)[0], True
    )


def is_bar(spans):
    return bool(spans) and spans[-1][1] == "bar"


def rows_for(spans, cols):
    """Screen rows a line takes once the terminal wraps it."""
    w = sum(width(t) for t, _s in spans)
    return max(1, -(-w // max(1, cols)))


def to_ansi(spans, color=True):
    if not color:
        return "".join(t for t, _s in spans)
    parts = []
    for text, style in spans:
        code = STYLES.get(style, "")
        parts.append(code + text + RESET if code else text)
    return "".join(parts)


class Screen:
    """Feed + pinned block on a terminal; plain feed lines when not a tty."""

    def __init__(self, out=None, inp=None, color=None, plain=False, size=None):
        self.out = out or sys.stdout
        self.inp = inp or sys.stdin
        self.tty = self.out.isatty() and not plain
        if color is None:
            color = (
                self.tty and not os.environ.get("NO_COLOR") and os.environ.get("TERM", "") != "dumb"
            )
        self.color = color
        self._size = size  # test hook: () -> (cols, rows)
        self.block_h = 0  # rows reserved under the scroll region
        self.rows = self.cols = 0  # screen size the layout was made for
        self.started = False
        self.pending = []  # ("feed", spans) / ("open"|"close", key, ...)
        self.updates = {}  # key -> latest spans of a live line
        self.live = {}  # key -> rows between it and the region bottom
        self.lines = collections.deque(maxlen=1000)  # [key or None, spans], oldest first
        self.entries = {}  # key -> its entry in `lines`
        self._repaint_due = False
        self.resized = False
        self.keys = b""
        self._termios = None
        self._prev_winch = None
        self._saved = None

    # ---- lifecycle
    def start(self, stderr_path=None):
        if stderr_path:
            # Libraries print to fds 1 and 2 (onnxruntime, sherpa, NeMo-Speech);
            # on the terminal any such line lands at a random place of the
            # layout, in a pipe it mixes into the feed. Our output goes to a
            # private duplicate of fd 1, theirs to a file.
            self.out.flush()
            sys.stderr.flush()
            self._saved = (os.dup(1), os.dup(2))
            self.out = os.fdopen(os.dup(1), "w", encoding="utf-8", errors="replace")
            fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            os.close(fd)
        if not self.tty:
            return
        self._prev_winch = signal.signal(signal.SIGWINCH, self._on_winch)
        # Started from a background process group (e.g. under `timeout` without
        # --foreground) tcsetattr would stop us with SIGTTOU.
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        try:
            import termios

            fd = self.inp.fileno()
            if os.isatty(fd):
                old = termios.tcgetattr(fd)
                new = termios.tcgetattr(fd)
                new[3] &= ~(termios.ECHO | termios.ICANON)
                termios.tcsetattr(fd, termios.TCSADRAIN, new)
                self._termios = (fd, old)
        except (ImportError, OSError, ValueError):
            self._termios = None
        self.out.write("\x1b[?25l")
        self.out.flush()

    def stop(self, final_block=()):
        """Leave the terminal as found: no scroll region, cursor shown, the feed
        on screen and the final status right under it, cursor below that."""
        if self.tty:
            buf = []
            if self.started:
                buf += self._feed_lines(self.cols)
                top = self.rows - self.block_h + 1
                buf.append("\x1b[r")
                for r in range(top, self.rows + 1):
                    buf.append("\x1b[%d;1H\x1b[2K" % r)
                buf.append("\x1b[%d;1H" % top)
            else:
                buf += [to_ansi(sp, self.color) + "\n" for sp in self._finished()]
            cols, _rows = self.size()
            for spans in final_block:
                buf.append(self._line(spans, cols) + "\n")
            buf.append("\x1b[?25h")
            self.out.write("".join(buf))
            self.out.flush()
            if self._termios:
                import termios

                fd, old = self._termios
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            if self._prev_winch is not None:
                signal.signal(signal.SIGWINCH, self._prev_winch)
        else:
            self.flush_plain()
            for spans in final_block:
                self.out.write(to_ansi(spans, False) + "\n")
            self.out.flush()
        if self._saved is not None:
            sys.stdout.flush()
            sys.stderr.flush()
            for orig, fd in zip(self._saved, (1, 2), strict=True):
                os.dup2(orig, fd)
                os.close(orig)
            self._saved = None
            self.out.close()
            self.out = sys.stdout
        self.pending = []
        self.started = False

    def _on_winch(self, _sig, _frm):
        self.resized = True

    def size(self):
        if self._size is not None:
            return self._size()
        try:
            sz = os.get_terminal_size(self.out.fileno())
            return sz.columns, sz.lines
        except (OSError, ValueError):
            return 80, 24

    # ---- input
    def _read(self, timeout):
        if not self._termios:
            return b""
        fd = self._termios[0]
        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            return b""
        try:
            return os.read(fd, 64)
        except OSError:
            return b""

    def key(self):
        """One pending key press, or None. Never blocks."""
        if not self.keys:
            self.keys = self._read(0)
        if not self.keys:
            return None
        k, self.keys = self.keys[:1], self.keys[1:]
        return k.decode("ascii", "replace")

    # ---- output
    def feed(self, spans):
        self.pending.append(("feed", spans))

    def live_open(self, key, spans):
        """Start a live line (cut to one row) at the bottom of the feed."""
        self.pending.append(("open", key, spans))

    def live_update(self, key, spans):
        """New text of a live line; only the latest before a draw is shown."""
        self.updates[key] = spans

    def live_close(self, key, spans, cont=()):
        """Final text of a live line, in place if it can still be addressed,
        else as a new line prefixed with `cont`."""
        self.updates.pop(key, None)
        self.pending.append(("close", key, spans, list(cont)))

    def _finished(self):
        """Lines a non-terminal gets: feed lines and final texts, in order."""
        out = [
            op[1] if op[0] == "feed" else op[2] for op in self.pending if op[0] in ("feed", "close")
        ]
        self.pending, self.updates = [], {}
        return out

    def flush_plain(self):
        for spans in self._finished():
            self.out.write(to_ansi(spans, self.color) + "\n")
        self.out.flush()

    # ---- feed region bookkeeping
    def _bottom(self):
        return self.rows - self.block_h

    def _row(self, key):
        """Screen row of a live line, or None if it left the region."""
        off = self.live.get(key)
        if off is None:
            return None
        row = self._bottom() - off
        if row < 1:
            del self.live[key]
            return None
        return row

    def _shift(self, n, above=-1):
        """Live lines scrolled up by n rows (only those further from the
        bottom than `above`, when a region over one row scrolled)."""
        for k in self.live:
            if self.live[k] > above:
                self.live[k] += n

    def _text(self, spans, cols):
        """Line text, erased to the end of its last row. Not after a line that
        fills its last row: the cursor waits in the last column there, and an
        erase from it would take the line's last character."""
        w = sum(width(t) for t, _s in spans)
        return to_ansi(spans, self.color) + ("" if w and w % cols == 0 else "\x1b[K")

    def _new_line(self, spans, cols, key=None):
        old = self.entries.pop(key, None)
        if old is not None:
            old[0] = None  # a frozen copy stays where it is
        entry = [key, spans]
        self.lines.append(entry)
        if key is not None:
            self.entries[key] = entry
            spans = fit(spans, cols - 1)[0]
        self._shift(rows_for(spans, cols))
        return ["\x1b[%d;1H\n" % self._bottom() + self._text(spans, cols)]

    def _close(self, key, spans, cont, cols):
        row = self._row(key)
        k = rows_for(spans, cols)
        if row is None or row - k + 1 < 1:
            self.live.pop(key, None)
            old = self.entries.pop(key, None)
            if old is not None:
                old[0] = None
            return self._new_line(cont + spans, cols)
        off = self.live.pop(key)
        entry = self.entries.pop(key)
        entry[0], entry[1] = None, spans
        if k == 1:
            return ["\x1b[%d;1H" % row + self._text(spans, cols)]
        # make room above: rows 1..row scroll up k - 1, the rows under stay
        self._shift(k - 1, above=off)
        return [
            "\x1b[1;%dr\x1b[%d;1H" % (row, row)
            + "\n" * (k - 1)
            + "\x1b[%d;1H" % (row - k + 1)
            + self._text(spans, cols)
            + "\x1b[1;%dr" % self._bottom()
        ]

    def _feed_lines(self, cols):
        """Pending feed operations, then the latest text of each live line."""
        buf = []
        for op in self.pending:
            if op[0] == "feed":
                buf += self._new_line(op[1], cols)
            elif op[0] == "open":
                buf += self._new_line(op[2], cols, op[1])
                self.live[op[1]] = 0
            else:
                buf += self._close(op[1], op[2], op[3], cols)
        for key, spans in self.updates.items():
            row = self._row(key)
            if row is None:
                if key not in self.entries:
                    continue  # closed meanwhile
                # scrolled out of the region: the line comes back at the bottom
                buf += self._new_line(spans, cols, key)
                self.live[key] = 0
            else:
                self.entries[key][1] = spans
                buf.append(
                    "\x1b[%d;1H" % row + to_ansi(fit(spans, cols - 1)[0], self.color) + "\x1b[K"
                )
        self.pending, self.updates = [], {}
        return buf

    def _layout(self, want_h, cols, rows):
        """Make the bottom `want_h` rows the block and rows above it the scroll
        region, moving feed lines so none is covered. -> escape sequences."""
        buf = []
        if not self.started:
            # Push what is on the screen (the shell prompt) above the block's
            # rows: from the cursor, want_h line feeds scroll the screen as
            # needed and leave the rows under the old content blank.
            buf.append("\r" + "\n" * want_h)
        elif (cols, rows) != (self.cols, self.rows):
            buf.append("\x1b[r")
            self._repaint_due = True
        elif want_h > self.block_h:
            buf.append("\x1b[%d;1H" % (self.rows - self.block_h))
            buf.append("\n" * (want_h - self.block_h))
        else:
            return buf  # a shorter block keeps the zone: blank rows
        self.block_h, self.cols, self.rows = want_h, cols, rows
        self.started = True
        buf.append("\x1b[1;%dr" % (rows - want_h))
        return buf

    def _repaint(self, cols):
        """After a resize: the terminal has reflowed the screen, so nothing on
        it is where we last put it. Erase the feed region row by row (an
        erase-below from the top-left corner is a whole-screen clear to tmux,
        which copies the screen into the history) and paint the newest lines
        that fit from memory; live lines get their new rows."""
        bottom = self._bottom()
        buf = ["\x1b[%d;1H\x1b[2K" % r for r in range(1, self.rows + 1)]
        self.live = {}
        left, placed = bottom, []
        for entry in reversed(self.lines):
            spans = fit(entry[1], cols - 1)[0] if entry[0] is not None else entry[1]
            k = rows_for(spans, cols)
            if k > left:
                break
            left -= k
            placed.append((left + 1, spans, entry[0]))
        for start, spans, key in placed:
            buf.append("\x1b[%d;1H" % start + self._text(spans, cols))
            if key is not None:
                self.live[key] = bottom - start
        return buf

    def _line(self, spans, cols):
        if is_bar(spans):
            return bar_line(spans, cols, self.color) + ("" if self.color else "\x1b[K")
        return to_ansi(fit(spans, cols - 1)[0], self.color) + "\x1b[K"

    def draw(self, block):
        """Print pending feed lines into the scroll region, redraw the block in
        the rows under it, park the cursor on the block's first row."""
        if not self.tty:
            self.flush_plain()
            return
        cols, rows = self.size()
        self.resized = False
        block = list(block)[: max(1, rows - 2)]
        buf = self._layout(len(block), cols, rows)
        if self._repaint_due:
            self._repaint_due = False
            buf += self._repaint(cols)
        buf += self._feed_lines(cols)
        top = rows - self.block_h + 1
        for i in range(self.block_h):
            spans = block[i] if i < len(block) else []
            buf.append("\x1b[%d;1H" % (top + i) + self._line(spans, cols))
        buf.append("\x1b[%d;1H" % top)
        self.out.write("".join(buf))
        self.out.flush()
