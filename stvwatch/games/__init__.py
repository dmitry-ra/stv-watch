"""Game profiles: what differs between the games on the same engine branch.

The transport, the signon ladder, string tables, game events (self-described
on the wire) and voice are the engine's and the same for every game here. A
profile holds the rest: the SendTable CRC of each server build, the order of
user message registration (ids are that order, no names travel), the chat
format names, the teams, and the folder A2S_INFO reports, by which a relay of
another game is told apart before connecting.
"""

from dataclasses import dataclass, field

DEFAULT = "hl2dm"


@dataclass(frozen=True)
class Game:
    name: str  # --game value, recorded in capture.tvd and meta.json
    title: str
    folder: str  # A2S_INFO folder, the server's -game directory
    server_app: int  # SteamCMD app of the dedicated server
    module: str  # where crc_by_build lives, for the refusal's hint
    crc_by_build: dict
    user_messages: tuple
    chat_prefix: str  # SayText2 format of a player's chat: PREFIX_Channel
    teams: dict
    # player_death: death_flags bit of a faked death, customkill names
    feign_flag: int = 0
    custom_kills: dict = field(default_factory=dict)

    def user_message(self, mid):
        return self.user_messages[mid] if mid < len(self.user_messages) else f"#{mid}"


def _load():
    from . import hl2dm, tf2

    return {g.name: g for g in (hl2dm.GAME, tf2.GAME)}


GAMES = _load()
NAMES = tuple(GAMES)


def get(name):
    """--game value (None: the default) -> Game; ValueError on an unknown name."""
    try:
        return GAMES[name or DEFAULT]
    except KeyError:
        raise ValueError(f"unknown game {name!r} (games: {', '.join(NAMES)})") from None


def by_folder(folder):
    """A2S_INFO folder -> Game, or None for a game stv-watch does not know."""
    return next((g for g in GAMES.values() if g.folder == folder), None)
