"""Game profiles: what differs between the games on the same engine branch.

The transport, the signon ladder, string tables, game events (self-described
on the wire) and voice are the engine's and the same for every game here. A
profile holds the rest: the SendTable CRC of each server build, the order of
user message registration (ids are that order, no names travel), the chat
format names, the teams, the game's own player_death fields, who may not
hear whom, the userinfo we connect with, the folder A2S_INFO reports (by
which a relay of another game is told apart before connecting) and how to run
a dedicated server of the game for the tools.

There is no default game below the command line: every consumer takes a
profile, so a caller that forgot it fails instead of decoding as HL2DM.
"""

from dataclasses import dataclass
from typing import Callable

DEFAULT = "hl2dm"  # of the command line only (cli.py, net/main.py)
SPECTATOR = 1  # TEAM_SPECTATOR of the engine's shared teams


def no_death_details(_f, _player):
    return "", {}


def spectator_unheard(team, alltalk):
    """CanPlayerHearPlayer of hl2mp and tf (hl2mp_gamerules.cpp,
    tf_gamerules.cpp) passes voice only between equal teams, all of it with
    sv_alltalk 1: a spectator talks to spectators."""
    return not alltalk and team == SPECTATOR


@dataclass(frozen=True)
class Game:
    name: str  # --game value, recorded in capture.tvd and meta.json
    title: str
    folder: str  # A2S_INFO folder, the server's -game directory
    server_app: int  # SteamCMD app of the dedicated server
    server_binary: str  # its launcher, in the install's root
    bin_dir: str  # where that launcher's engine_srv.so lives
    test_map: str  # a stock map for a local server
    module: str  # where crc_by_build lives, for the refusal's hint
    crc_by_build: dict
    user_messages: tuple
    chat_prefix: str  # SayText2 format of a player's chat: PREFIX_Channel
    teams: dict
    # net_SetConVar at CONNECTED, in wire order; `name` is set per run
    userinfo: tuple
    # player_death of this game: (event fields, player) -> (text after the
    # weapon, extra fields); player(userid) -> (steamid64 or 0, nick)
    death_details: Callable = no_death_details
    # (team of the speaker or None, sv_alltalk) -> voice the players may not hear
    unheard: Callable = spectator_unheard

    def user_message(self, mid):
        return self.user_messages[mid] if mid < len(self.user_messages) else f"#{mid}"


def _load():
    from . import hl2dm, tf2

    return {g.name: g for g in (hl2dm.GAME, tf2.GAME)}


GAMES = _load()
NAMES = tuple(GAMES)


def get(name):
    """--game value -> Game; ValueError on an unknown name or none."""
    if name is None:
        raise ValueError(f"no game given (games: {', '.join(NAMES)})")
    try:
        return GAMES[name]
    except KeyError:
        raise ValueError(f"unknown game {name!r} (games: {', '.join(NAMES)})") from None


def titles():
    """'A, B or C' of the games' titles, for the command line's description."""
    t = [g.title for g in GAMES.values()]
    return t[0] if len(t) == 1 else ", ".join(t[:-1]) + " or " + t[-1]


def by_folder(folder):
    """A2S_INFO folder -> Game, or None for a game stv-watch does not know."""
    return next((g for g in GAMES.values() if g.folder == folder), None)
