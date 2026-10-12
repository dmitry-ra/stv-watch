#!/usr/bin/env python3
"""Print the user message names of a server build in id order, for a game profile.

    python3 tools/read_usermessages.py SRCDS_DIR [--game hl2dm|tf2]

A user message travels as its id, the index of its Register() call; no table
of names is sent. The names are read from the game library itself: the string
arguments of RegisterUserMessages(), RegisterHapticMessages() and
RegisterScriptMessages() of GAME/bin/linux64/server_srv.so (or bin/server_srv.so),
in the order the code loads them (`objdump -d`, RIP-relative `lea` of a string
in .rodata). Compare the list with `user_messages` of stvwatch/games/<game>.py
after a game update: an inserted message moves every id after it. Needs
objdump and readelf (binutils).
"""

import argparse
import os
import re
import subprocess
import sys

FUNCS = (
    "_Z20RegisterUserMessagesv",
    "_Z22RegisterHapticMessagesv",
    "_Z22RegisterScriptMessagesv",
)
FOLDERS = {"hl2dm": "hl2mp", "tf2": "tf"}
LEA = re.compile(r"\blea\s+0x[0-9a-f]+\(%rip\),%r\w+\s+# ([0-9a-f]+)")


def library(srv_dir, game):
    for rel in ("bin/linux64/server_srv.so", "bin/server_srv.so"):
        path = os.path.join(srv_dir, game, rel)
        if os.path.exists(path):
            return path
    sys.exit(f"no server_srv.so under {os.path.join(srv_dir, game, 'bin')}")


def symbols(path):
    out = subprocess.run(["readelf", "-sW", path], capture_output=True, text=True, check=True)
    found = {}
    for line in out.stdout.splitlines():
        f = line.split()
        if len(f) >= 8 and f[7] in FUNCS and f[3] == "FUNC":
            found[f[7]] = (int(f[1], 16), int(f[2], 0))
    return found


def rodata(path):
    out = subprocess.run(["readelf", "-SW", path], capture_output=True, text=True, check=True)
    for line in out.stdout.splitlines():
        f = line.replace("[ ", "[").split()
        if len(f) > 5 and f[1] == ".rodata":
            return int(f[3], 16), int(f[4], 16), int(f[5], 16)
    sys.exit("no .rodata section")


def names(path):
    syms = symbols(path)
    missing = [f for f in FUNCS if f not in syms]
    if missing:
        sys.exit(f"symbols not found: {', '.join(missing)}")
    addr, off, size = rodata(path)
    data = open(path, "rb").read()
    out = []
    for func in FUNCS:
        start, length = syms[func]
        dis = subprocess.run(
            [
                "objdump",
                "-d",
                "--no-show-raw-insn",
                f"--start-address={start}",
                f"--stop-address={start + length}",
                path,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for m in LEA.finditer(dis):
            va = int(m.group(1), 16)
            if not addr <= va < addr + size:
                continue
            at = off + va - addr
            out.append(data[at : data.index(b"\0", at)].decode("ascii", "replace"))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("srcds_dir")
    ap.add_argument("--game", choices=sorted(FOLDERS), default="hl2dm")
    a = ap.parse_args()
    for i, name in enumerate(names(library(os.path.abspath(a.srcds_dir), FOLDERS[a.game]))):
        print(i, name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
