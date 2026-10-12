"""Half-Life 2: Deathmatch.

User message ids: registration order in RegisterUserMessages()
(source-sdk-2013 src/game/shared/hl2/hl2_usermessages.cpp, then
RegisterHapticMessages() and RegisterScriptMessages()).
"""

from . import Game

GAME = Game(
    name="hl2dm",
    title="Half-Life 2: Deathmatch",
    folder="hl2mp",
    server_app=232370,
    module="stvwatch/games/hl2dm.py",
    # g_SendTableCRC of engine_srv.so of the build (tools/read_crc.py)
    crc_by_build={
        "10889068": 0xD9B6082D,
        "9540945": 0x35B19FD9,
    },
    user_messages=(
        "Geiger",
        "Train",
        "HudText",
        "SayText",
        "SayText2",
        "TextMsg",
        "HudMsg",
        "ResetHUD",
        "GameTitle",
        "ItemPickup",
        "ShowMenu",
        "Shake",
        "Fade",
        "VGUIMenu",
        "Rumble",
        "Battery",
        "Damage",
        "VoiceMask",
        "RequestState",
        "CloseCaption",
        "HintText",
        "KeyHintText",
        "SquadMemberDied",
        "AmmoDenied",
        "CreditsMsg",
        "LogoTimeMsg",
        "AchievementEvent",
        "UpdateJalopyRadar",
        "SPHapWeapEvent",
        "HapDmg",
        "HapPunch",
        "HapSetDrag",
        "HapSetConst",
        "HapMeleeContact",
        "SavedConvar",
    ),
    chat_prefix="HL2MP_Chat",
    teams={0: "unassigned", 1: "spectator", 2: "combine", 3: "rebels"},
)
