"""Half-Life 2: Deathmatch.

User message ids: registration order in RegisterUserMessages()
(source-sdk-2013 src/game/shared/hl2/hl2_usermessages.cpp, then
RegisterHapticMessages() and RegisterScriptMessages()).
"""

from . import Game

# The userinfo cvar set a stock hl2mp client sends at CONNECTED, in wire order.
# Values are the engine defaults a fresh client reports. Order matters only for
# byte-identity with the reference capture - the server reads them as an
# unordered set.
USERINFO = (
    ("cl_interp_npcs", "0"),
    ("cl_thirdperson", "0"),
    ("cl_team", "default"),
    ("cl_class", "default"),
    ("cl_predict", "1"),
    ("cl_interp_ratio", "2"),
    ("cl_interp", "0.1"),
    ("cl_showhelp", "1"),
    ("english", "1"),
    ("cl_predictweapons", "1"),
    ("cl_lagcompensation", "1"),
    ("cl_spec_mode", "1"),
    ("cl_playermodel", "none"),
    ("cl_defaultweapon", "weapon_physcannon"),
    ("cl_autowepswitch", "1"),
    ("name", "unnamed"),
    ("cl_interpolate", "1"),
    ("cl_clanid", "0"),
    ("cl_connectmethod", ""),
    ("tv_nochat", "0"),
    ("cl_language", "english"),
    ("rate", "80000"),
    ("cl_cmdrate", "30"),
    ("cl_updaterate", "20"),
    ("closecaption", "0"),
    ("net_maxroutable", "1260"),
    ("voice_loopback", "0"),
)

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
    userinfo=USERINFO,
)
