"""Command line of stv-watch.

stv-watch --relay IP:PORT [--asr parakeet] [--events all] [--tz Europe/Berlin] ...
stv-watch --replay capture.tvd [--speed 0] [--json] ...
"""

import argparse
import os
import sys
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import asr
from . import events as gamevents
from .app import App
from .asr import weights


def default_out():
    """$XDG_DATA_HOME/stv-watch/sessions, ~/.local/share when it is unset."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(base, "stv-watch", "sessions")


def relay_addr(text, ap):
    """IP:PORT -> the same with the port in canonical form. IPv4 or a host name
    only: the client's sockets are AF_INET."""
    host, sep, port = text.rpartition(":")
    if not sep or not host or ":" in host:
        ap.error(f"--relay wants IP:PORT (IPv4 or a host name), not {text!r}")
    if not (port.isascii() and port.isdigit() and 1 <= int(port) <= 65535):
        ap.error(f"--relay: the port must be a number 1-65535, not {port!r}")
    return f"{host}:{int(port)}"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="stv-watch",
        description="Watch a SourceTV relay of Half-Life 2: Deathmatch: voice chat with an "
        "optional transcript, game chat and events, connection and traffic, live or from a "
        "recording.",
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--relay", metavar="IP:PORT", help="live: connect to this SourceTV relay")
    src.add_argument("--replay", metavar="DUMP", help="replay a .tvd recording")
    src.add_argument(
        "--follow",
        metavar="JOURNAL",
        help="watch a .tvd journal another process is writing (another stv-watch): no client "
        "of our own, one connection for many watchers; earlier records only set the state",
    )
    ap.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="replay pace: 1 = as recorded, 4 = four times faster, 0 = max",
    )
    ap.add_argument(
        "--skip",
        default="",
        metavar="SECONDS",
        help="replay: fast-forward this many seconds into the recording",
    )
    ap.add_argument(
        "--seconds",
        type=float,
        default=0.0,
        help="stop after this long; live: wall seconds (the client leaves the relay), "
        "replay: seconds of the recording after --skip, whatever --speed; 0 = until q",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="parent of the session directory (default: $XDG_DATA_HOME/stv-watch/sessions, "
        "~/.local/share/stv-watch/sessions when XDG_DATA_HOME is unset)",
    )
    ap.add_argument("--name", default="stvwatch", help="client name shown on the relay")
    ap.add_argument(
        "--allow-last-slot",
        action="store_true",
        help="live: connect even if no relay slot would remain free after us",
    )
    ap.add_argument(
        "--asr",
        choices=asr.ENGINES,
        default=None,
        help="recognize what players say with this engine; its weights are downloaded on "
        "the first use (parakeet: 2.55 GB). Without it voice is still shown: who talks and "
        "how long, WAV files, transcript.tsv rows without text",
    )
    ap.add_argument("--threads", type=int, default=2, help="recognizer CPU threads")
    ap.add_argument(
        "--max-utt",
        type=float,
        default=30.0,
        help="a monologue is recognized in pieces of about this many seconds, cut at a pause "
        "(from 5 s before it; at it, the last pause past a third of it); hard only with no "
        "pause; later pieces are marked (cont)",
    )
    ap.add_argument(
        "--drain",
        type=float,
        default=20.0,
        help="on exit, wait this many seconds for queued recognition",
    )
    ap.add_argument("--no-audio", action="store_true", help="do not write utterance WAV files")
    ap.add_argument(
        "--models-dir",
        default=None,
        help="where recognition weights live (default: $XDG_CACHE_HOME/stv-watch/models, "
        "~/.cache/stv-watch/models when XDG_CACHE_HOME is unset)",
    )
    ap.add_argument(
        "--tz",
        metavar="NAME",
        default=None,
        help="time zone of the screen and the files, an IANA name such as Europe/Berlin "
        "(default UTC); with it each JSON line also gets t_local",
    )
    ap.add_argument(
        "--quiet",
        type=float,
        default=3.0,
        help="traffic counts as stopped after this many seconds without datagrams",
    )
    ap.add_argument(
        "--status-every",
        type=float,
        default=10.0,
        help="plain output: status lines every N seconds",
    )
    ap.add_argument(
        "--plain",
        action="store_true",
        help="no pinned block: feed lines plus status lines every --status-every s",
    )
    ap.add_argument(
        "--monitor",
        action="store_true",
        help="for a program reading a pipe: one self-contained line per event, no status "
        "lines, no escape sequences, voice only as its final line",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="like --monitor, one JSON object per line (t_utc, t_local with --tz, type, "
        "steamid64, nick, text, extra fields); the session's events.jsonl gets the same "
        "lines in any screen mode",
    )
    ap.add_argument(
        "--events",
        default="default",
        help="game events in the feed, comma list of: "
        + ",".join(gamevents.TYPES)
        + "; 'all', 'none', 'default' ("
        + ",".join(gamevents.DEFAULT)
        + "); a leading '-' takes out: 'all,-server' or '-server' = all but server, "
        "'default,-name' = default but name; minus items apply after all plus items "
        "(kills are always counted in the block)",
    )
    ap.add_argument(
        "--debug",
        action="store_true",
        help="on screen also the transport and recognizer numbers: per utterance (frames, "
        "gaps, bit rate, arrivals, recognition time), in the block (rates, -2, choked, CPU, "
        "queue, RTF) and both SteamIDs of a kill; feed.log and --json always hold them. "
        "Also a feed line per sequence gap counted as lost ('net lost: seq A -> B (N)'), in "
        "feed.log and --json only with --debug",
    )
    ap.add_argument("--no-color", action="store_true")
    argv = list(sys.argv[1:] if argv is None else argv)
    for i in range(len(argv) - 1):
        # argparse takes '-server' for an option, not the value of --events
        if argv[i] == "--events" and argv[i + 1][:1] == "-" and argv[i + 1][:2] != "--":
            argv[i : i + 2] = ["--events=" + argv[i + 1]]
            break
    a = ap.parse_args(argv)
    if a.relay is not None:
        a.relay = relay_addr(a.relay, ap)
    if a.follow:
        a.replay, a.speed, a.skip = a.follow, 1.0, ""
    try:
        a.skip_s = float(a.skip) if a.skip else 0.0
    except ValueError:
        ap.error(f"--skip wants seconds, not {a.skip!r}")
    try:
        a.event_types = gamevents.parse_types(a.events)
    except ValueError as e:
        ap.error(str(e))
    a.tz_given = a.tz is not None
    if a.tz_given:
        try:
            a.tz = ZoneInfo(a.tz)
        except (ZoneInfoNotFoundError, ValueError):
            ap.error(
                f"--tz: unknown time zone {a.tz!r} (an IANA name such as Europe/Berlin; "
                "needs the system time zone database, package tzdata)"
            )
    else:
        a.tz = timezone.utc
    if a.out is None:
        a.out = default_out()
    if a.models_dir is None:
        a.models_dir = weights.default_dir()
    if a.threads < 1:
        ap.error("--threads wants 1 or more")
    if a.max_utt < 6:
        ap.error("--max-utt wants 6 seconds or more")
    a.monitor = a.monitor or a.json
    a.plain = a.plain or a.monitor
    if a.monitor:
        a.no_color = True
    return a


def main(argv=None):
    a = parse_args(argv)
    if a.asr:
        try:
            weights.ensure(weights.PINS[a.asr], a.models_dir)
        except (weights.WeightsError, KeyboardInterrupt) as e:
            print(f"stv-watch: {a.asr} weights not available: {e}", file=sys.stderr)
            return 2
    return App(a).run()


if __name__ == "__main__":
    sys.exit(main())
