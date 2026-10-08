#!/usr/bin/env python3
"""Server build -> SendTable CRC, and the permanent/temporary failure split.

The CRC is a property of the build, and the build is public in A2S_INFO
`version`, so the client never has to guess it. An unknown build is an alarm
for a human (how to extract a new CRC: docs/protocol.md, "Server builds"), not a
reason to try candidates: every wrong guess is a fresh connection on the relay.
"""

# Extracted from engine_srv.so of the matching build (g_SendTableCRC).
CRC_BY_BUILD = {
    "10889068": 0xD9B6082D,
    "9540945": 0x35B19FD9,
}

PERMANENT = "permanent"
TEMPORARY = "temporary"

# Reasons that will not change until someone edits the server or this table.
# Matched as substrings, lower-case: the protocol carries text, not codes.
_PERMANENT_MARKS = (
    "unknown build",  # ours: not in CRC_BY_BUILD
    "different class tables",  # wrong CRC
    "rejectoldversion",
    "rejectnewversion",
    "different version",
    "spectator password",
    "rejectbadpassword",
    "rejectbanned",
    "banned",
    "rejectlanrestrict",
    "steam ticket",
    "wants auth protocol",  # ours: relay requires Steam login
)


class UnknownBuild(Exception):
    pass


def crc_for(build):
    try:
        return CRC_BY_BUILD[str(build)]
    except KeyError:
        raise UnknownBuild(
            "unknown build %s: add its CRC to stvwatch/net/builds.py, see "
            "docs/protocol.md" % build
        ) from None


def classify(reason):
    """Failure text -> PERMANENT | TEMPORARY. Unknown text is TEMPORARY: a
    wrong 'temporary' costs a few extra connects, a wrong 'permanent' costs
    minutes of capture."""
    text = (reason or "").lower()
    return PERMANENT if any(m in text for m in _PERMANENT_MARKS) else TEMPORARY
