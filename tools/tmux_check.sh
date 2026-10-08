#!/bin/bash
# Check stv-watch in a real terminal (tmux): the status block sits on the
# bottom rows from the first frame, under a thin coloured rule (no background
# colour), with a fixed height (rule on row H-3, head on H-2, hint on H), the
# feed scrolls above it, the history holds feed lines and no copies of the
# block, across resizes, and the terminal is back to normal after exit.
#
#   tmux_check.sh SECONDS -- STV-WATCH-ARGS...
#
# SECONDS is how long the program runs (it is stopped with SIGTERM, i.e. the
# same path as Ctrl-C/q). Session $TMUX_CHECK_SESSION (stvwatch-check), pane 100x30;
# captures at 3 s, at a third of the run, after 60x20, 130x40 and 130x25
# (height only), and after exit, when the same pane prints 60 lines with
# printf. Screens go to $CAPDIR/<name>.txt, .ansi.txt and .hist.txt (default:
# a fresh temporary directory). Exit code 0 only if every check passed.
set -u
T=$1; shift 2
HERE=$(cd "$(dirname "$0")/.." && pwd)
S=${TMUX_CHECK_SESSION:-stvwatch-check}
OUT=${CAPDIR:-$(mktemp -d -t stvwatch-check.XXXXXX)}
mkdir -p "$OUT"
echo "captures: $OUT"
tmux kill-session -t $S 2>/dev/null
# the pane runs this string through a shell: every argument goes in quoted
ARGS=$(printf '%q ' "$@")
# window-size manual: a detached session otherwise takes the size of the
# most recent client of the server, not -x/-y
tmux new-session -d -s $S -x 100 -y 30 \
    "sleep 1; cd $(printf '%q' "$HERE") && timeout --foreground -s TERM $T uv run stv-watch $ARGS; printf 'AFTER%s\n' \$(seq 1 60); sleep 600" \
    \; set-option -t $S window-size manual \; resize-window -t $S -x 100 -y 30
tmux set-option -t $S history-limit 20000 >/dev/null
fail=0
check() {  # name, expect_block (1/0)
    tmux capture-pane -t $S -p > "$OUT/$1.txt"
    tmux capture-pane -t $S -p -e > "$OUT/$1.ansi.txt"
    tmux capture-pane -t $S -p -S - > "$OUT/$1.hist.txt"
    local h on hist quit_row last head_row bar_row bar_colour
    h=$(tmux display -t $S -p '#{pane_height}')
    on=$(grep -c -E '^(REPLAY|LIVE) ' "$OUT/$1.txt")
    hist=$(grep -c -E '^(REPLAY|LIVE) ' "$OUT/$1.hist.txt")
    quit_row=$(grep -n '^q quit' "$OUT/$1.txt" | tail -1 | cut -d: -f1)
    last=$(grep -n '[^ ]' "$OUT/$1.txt" | tail -1 | cut -d: -f2- | sed 's/ *$//')
    head_row=$(grep -n -E '^(REPLAY|LIVE) ' "$OUT/$1.txt" | tail -1 | cut -d: -f1)
    bar_row=$(grep -n 'stv-watch ' "$OUT/$1.txt" | tail -1 | cut -d: -f1)
    # the rule line: coloured (an SGR), and no background colour SGR (4x, 48)
    local rule
    rule=$(grep 'stv-watch ' "$OUT/$1.ansi.txt" | tail -1)
    bar_colour=0
    if printf '%s' "$rule" | grep -q $'\e\\[[0-9;]*m' \
        && ! printf '%s' "$rule" | grep -qP '\e\[(?:[0-9;]*;)?(?:4[0-7]|48)[;m]'; then
        bar_colour=1
    fi
    echo "$1 $(tmux display -t $S -p '#{pane_width}x')$h block-head on-screen=$on in-history=$hist" \
         "rows bar=${bar_row:-none} head=${head_row:-none} hint=${quit_row:-none} thin-coloured-rule=$bar_colour" \
         "last-line='${last:0:40}'"
    if [ "$2" = 1 ]; then
        [ "$on" = 1 ] && [ "$hist" = 1 ] && [ "${quit_row:-0}" = "$h" ] \
            && [ "${bar_row:-0}" = $((h - 3)) ] && [ "${head_row:-0}" = $((h - 2)) ] \
            && [ "$bar_colour" = 1 ] || { echo "  FAIL"; fail=1; }
    else
        [ "$last" = "AFTER60" ] && [ -z "$quit_row" ] && grep -q '^AFTER1$' "$OUT/$1.hist.txt" \
            || { echo "  FAIL"; fail=1; }
    fi
}
sleep 4; check a-first-frame 1
sleep $((T / 3)); check b-100x30 1
tmux resize-window -t $S -x 60 -y 20; sleep 2; check c-60x20 1
tmux resize-window -t $S -x 130 -y 40; sleep 2; check d-130x40 1
tmux resize-window -t $S -x 130 -y 25; sleep 2; check e-130x25 1
timeout $((T + 60)) bash -c "until tmux capture-pane -t $S -p | grep -q '^AFTER60'; do sleep 1; done"
check f-after-exit 0
tmux kill-session -t $S
exit $fail
