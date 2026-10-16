#!/usr/bin/env python3
"""(game, server build) -> SendTable CRC, and the permanent/temporary failure split.

The CRC is a property of the build, and the build is public in A2S_INFO
`version`, so the client never has to guess it. An unknown build is an alarm
for a human (how to extract a new CRC: docs/protocol.md, "Server builds"), not a
reason to try candidates: every wrong guess is a fresh connection on the relay.
The tables are in the game profiles (stvwatch/games/).
"""

from .. import games

PERMANENT = "permanent"
TEMPORARY = "temporary"

# Reasons that will not change until someone edits the server or this table.
# Matched as substrings, lower-case: the protocol carries text, not codes.
_PERMANENT_MARKS = (
    "unknown build",  # ours: not in the game's crc_by_build
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


def known(build, game):
    return str(build) in games.get(game).crc_by_build


def crc_for(build, game):
    g = games.get(game)
    try:
        return g.crc_by_build[str(build)]
    except KeyError:
        raise UnknownBuild(
            "unknown build %s of %s: add its CRC to %s, see docs/protocol.md"
            % (build, g.name, g.module)
        ) from None


def classify(reason):
    """Failure text -> PERMANENT | TEMPORARY. Unknown text is TEMPORARY: a
    wrong 'temporary' costs a few extra connects, a wrong 'permanent' costs
    minutes of capture."""
    text = (reason or "").lower()
    return PERMANENT if any(m in text for m in _PERMANENT_MARKS) else TEMPORARY
