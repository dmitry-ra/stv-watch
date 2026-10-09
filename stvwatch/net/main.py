#!/usr/bin/env python3
"""The network client (tvdump): holds a relay session and writes the journal.

    python -m stvwatch.net.main run  --dump PATH --ip IP --port N [--seconds N] ...
    python -m stvwatch.net.main read PATH [--assert-full] [--assert-no-gaps-over S] ...

stv-watch starts `run` as its child process (client.LiveClient).

`read` is not garnish: the dump is the product, and a dump nobody can read is
not a product. The --assert-* flags make each acceptance test a single command.
"""

import argparse
import ctypes
import json
import os
import signal
import sys
import time

from . import dump as dumpfmt
from . import supervisor, wire


def _arm_parent_death(parent_pid):
    """SIGTERM when the launching process dies (PR_SET_PDEATHSIG), the path on
    which we leave with net_Disconnect. -> False if it already died."""
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.prctl(1, signal.SIGTERM)
    return os.getppid() == parent_pid


def cmd_run(a):
    # Handlers before anything that can take time or a slot: a signal that
    # arrives while the dump opens must still stop the run cleanly.
    stopping = []

    def stop(_sig, _frm):
        stopping.append(True)
        if sup is not None:
            sup.stop = True

    sup = None
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if a.parent_pid and not _arm_parent_death(a.parent_pid):
        print("[tvdump] launcher already gone, not connecting")
        return 0
    opt = supervisor.Options(
        ip=a.ip,
        port=a.port,
        name=a.name,
        build=None if a.build == "auto" else a.build,
        password=a.password,
        crc=None if a.crc == "auto" else int(a.crc, 0),
        seconds=a.seconds,
        fsync_ms=a.fsync_ms,
        silence_full_s=a.silence_full_s,
        rerate=a.rerate,
        version=a.stv_version,
    )
    sup = supervisor.Supervisor(opt, a.dump)
    sup.stop = bool(stopping)

    t0 = time.time()
    print(f"[tvdump] {sup.endpoint} -> {a.dump}")
    sup.run()
    print(
        f"[tvdump] {time.time() - t0:.1f}s | sessions={sup.sessions} "
        f"reconnects={sup.reconnects} full={sup.reached_full} "
        f"leaves={sup.leaves} records={sup.dump.records}"
    )
    if sup.alarm:
        print(f"[tvdump] ALARM: {sup.alarm}")
        return 3
    return 0


def shown(gaps):
    """(at, gap) seconds as printed; the comparisons use the raw values."""
    return [(round(at, 1), round(gap, 3)) for at, gap in gaps]


def cmd_read(a):
    r = dumpfmt.DumpReader(a.path)
    counts, sessions, breaks, signons = {}, 0, [], []
    first = last = None
    prev_in = None
    gaps = []
    for t_ns, rtype, data in r:
        counts[rtype] = counts.get(rtype, 0) + 1
        if first is None:
            first = t_ns
        last = t_ns
        if rtype == dumpfmt.DATAGRAM_IN:
            if prev_in is not None:
                gap = (t_ns - prev_in) / 1e9
                if gap > a.gap_report:
                    gaps.append(((t_ns - first) / 1e9, gap))
            prev_in = t_ns
        elif rtype in dumpfmt.EVENT_TYPES:
            try:
                f = json.loads(data.decode("utf-8"))
            except ValueError:
                f = {}
            rel = (t_ns - first) / 1e9 if first else 0.0
            if rtype == dumpfmt.SESSION_START:
                sessions += 1
            elif rtype == dumpfmt.BROKEN:
                breaks.append((round(rel, 1), f.get("cause"), f.get("detail")))
            elif rtype == dumpfmt.SIGNON:
                signons.append((round(rel, 1), f.get("state")))
            if a.timeline:
                print("  %8.1fs  %-14s %s" % (rel, dumpfmt.TYPE_NAMES.get(rtype), f))

    span = (last - first) / 1e9 if first else 0.0
    print(f"endpoint={r.endpoint} span={span:.1f}s truncated_tail={r.truncated_tail}")
    print(
        "records: "
        + ", ".join(f"{dumpfmt.TYPE_NAMES.get(k, k)}={v}" for k, v in sorted(counts.items()))
    )
    print(
        f"sessions={sessions} breaks={len(breaks)} "
        f"reached_full={sum(1 for _, s in signons if s == wire.SIGNON_FULL)}"
    )
    for rel, cause, detail in breaks:
        print(f"  break @{rel}s  {cause}: {detail}")
    if gaps:
        print(f"inbound gaps > {a.gap_report}s: {shown(gaps[:20])}")

    rc = 0
    if a.assert_full and not any(s == wire.SIGNON_FULL for _, s in signons):
        print("ASSERT FAILED: never reached FULL")
        rc = 1
    if a.assert_max_breaks is not None and len(breaks) > a.assert_max_breaks:
        print(f"ASSERT FAILED: {len(breaks)} breaks > {a.assert_max_breaks}")
        rc = 1
    if a.assert_no_gaps_over is not None:
        big = [g for g in gaps if g[1] > a.assert_no_gaps_over]
        if big:
            print(f"ASSERT FAILED: gaps over {a.assert_no_gaps_over}s: {shown(big[:10])}")
            rc = 1
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(description="SourceTV session client + dumper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="connect, hold a FULL session, dump traffic")
    r.add_argument("--dump", required=True, help="output .tvd path")
    r.add_argument("--ip", required=True)
    r.add_argument("--port", type=int, required=True)
    r.add_argument("--name", default="tvdump")
    r.add_argument(
        "--build", default="auto", help="server build; auto = A2S_INFO version of the relay"
    )
    r.add_argument("--password", default="", help="tv_password, if set")
    r.add_argument(
        "--crc",
        default="auto",
        help="SendTable CRC; auto = builds.CRC_BY_BUILD[build]. "
        'A wrong one is refused: "different class tables"',
    )
    r.add_argument(
        "--seconds",
        type=float,
        default=0.0,
        help="bounded run for tests; 0 = daemon (run until signal)",
    )
    r.add_argument("--silence-full-s", type=float, default=15.0)
    r.add_argument(
        "--rerate", type=int, default=0, help="resend this rate once after FULL; 0 = off"
    )
    r.add_argument("--fsync-ms", type=int, default=1000)
    r.add_argument(
        "--parent-pid", type=int, default=0, help="leave when this process (the launcher) dies"
    )
    r.add_argument(
        "--stv-version",
        default=None,
        help="version written into the dump (default: this package's own)",
    )
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("read", help="summarize a dump")
    d.add_argument("path")
    d.add_argument("--timeline", action="store_true", help="print every event")
    d.add_argument("--gap-report", type=float, default=2.0)
    d.add_argument("--assert-full", action="store_true")
    d.add_argument("--assert-max-breaks", type=int, default=None)
    d.add_argument("--assert-no-gaps-over", type=float, default=None)
    d.set_defaults(fn=cmd_read)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
