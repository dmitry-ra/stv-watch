#!/usr/bin/env python3
"""Read the SendTable CRC of a server build, for `crc_by_build` of a game profile.

    python3 tools/read_crc.py SRCDS_DIR [--game hl2dm|tf2] [--map MAP]
                              [--offset 0xNNNN] [--log srcds.log]

SRCDS_DIR is a dedicated server install of the build in question (SteamCMD app
232370 for hl2dm, 232250 for tf2, updated to that build). The script starts a
LOCAL srcds from it (loopback, LAN, no SourceTV, no rcon, no crash upload),
waits until it has loaded a map, then reads g_SendTableCRC from the memory of
its engine_srv.so through /proc/<pid>/mem. We are its parent, so
ptrace_scope=1 allows the read.

The symbol's offset is taken from `readelf -sW` of engine_srv.so unless given
with --offset. Prints the CRC as 0xXXXXXXXX; the build number is `version` in
the A2S_INFO reply of a server running that build, and `PatchVersion` in
GAME/steam.inf of the install.
"""

import argparse
import os
import struct
import subprocess
import sys
import time


def symbol_offset(srv_dir, rel="bin/engine_srv.so"):
    so = os.path.join(srv_dir, rel)
    out = subprocess.run(["readelf", "-sW", so], capture_output=True, text=True, check=True)
    for line in out.stdout.splitlines():
        f = line.split()
        if len(f) >= 8 and f[-1] == "g_SendTableCRC":
            return int(f[1], 16)
    raise SystemExit(f"g_SendTableCRC not found in the symbols of {so}")


# game -> (-game folder, launcher, engine library, a stock map)
GAMES = {
    "hl2dm": ("hl2mp", "srcds_linux", "bin/engine_srv.so", "dm_lockdown"),
    "tf2": ("tf", "srcds_linux64", "bin/linux64/engine_srv.so", "ctf_2fort"),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("srcds_dir")
    ap.add_argument("--offset", type=lambda v: int(v, 0), default=None)
    ap.add_argument("--log", default="srcds.log")
    ap.add_argument("--game", choices=sorted(GAMES), default="hl2dm")
    ap.add_argument("--map", default=None)
    a = ap.parse_args()
    folder, binary, engine, stock_map = GAMES[a.game]
    d = os.path.abspath(a.srcds_dir)
    off = a.offset if a.offset is not None else symbol_offset(d, engine)
    print(f"g_SendTableCRC at offset 0x{off:x} of engine_srv.so")
    env = dict(os.environ, LD_LIBRARY_PATH=f"{d}:{d}/{os.path.dirname(engine)}")
    cmd = [
        f"{d}/{binary}",
        "-game",
        folder,
        "-console",
        "-norestart",
        "-nohltv",
        "-nobreakpad",
        "+ip",
        "127.0.0.1",
        "-port",
        "27915",
        "+sv_lan",
        "1",
        "+maxplayers",
        "2",
        "+map",
        a.map or stock_map,
        "+rcon_password",
        "",
    ]
    log = open(a.log, "wb")
    p = subprocess.Popen(
        cmd, cwd=d, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT
    )
    val = None
    try:
        for i in range(90):
            time.sleep(1)
            if p.poll() is not None:
                print("srcds exited", p.returncode, "- see", a.log)
                break
            try:
                with open(f"/proc/{p.pid}/maps") as f:
                    maps = f.read()
            except OSError:
                continue
            bases = [
                int(ln.split("-")[0], 16)
                for ln in maps.splitlines()
                if ln.rstrip().endswith("engine_srv.so") and " 00000000 " in ln
            ]
            if not bases:
                continue
            with open(f"/proc/{p.pid}/mem", "rb") as m:
                m.seek(bases[0] + off)
                v = struct.unpack("<I", m.read(4))[0]
            if v and v != val:
                print(f"t={i}s base=0x{bases[0]:x} g_SendTableCRC=0x{v:08X}")
                val = v
            # the value is set once the map is loaded; give it time to settle
            if val and i > 25:
                break
    finally:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        log.close()
    print("final", "0x%08X" % val if val else None)
    return 0 if val else 1


if __name__ == "__main__":
    sys.exit(main())
