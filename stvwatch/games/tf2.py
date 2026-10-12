"""Team Fortress 2.

User message ids: tools/read_usermessages.py on server_srv.so of build
11087207, the same order as RegisterUserMessages() in source-sdk-2013
src/game/shared/tf/tf_usermessages.cpp. Chat formats: GetChatFormat() in
src/game/shared/tf/tf_gamerules.cpp; death_flags and customkill: TF_DEATH_*
and ETFDmgCustom in src/game/shared/tf/tf_shareddefs.h.
"""

from . import Game, hl2dm

FEIGN_DEATH = 0x20  # TF_DEATH_FEIGN_DEATH: Dead Ringer, a death the players see as real
CUSTOM_KILLS = {1: "headshot", 2: "backstab"}


def death_details(f, player):
    """An assister is a userid, -1 when none."""
    how, more = "", {}
    custom = CUSTOM_KILLS.get(f.get("customkill"))
    if custom:
        how += ", " + custom
        more["custom"] = custom
    if f.get("death_flags", 0) & FEIGN_DEATH:
        how += ", feigned"
        more["feign"] = True
    assister = f.get("assister", -1)
    if assister > 0:
        sid, nick = player(assister)
        how += ", assist " + nick
        more.update(assister=assister, assister_nick=nick, assister_steamid64=sid)
    return how, more


GAME = Game(
    name="tf2",
    title="Team Fortress 2",
    folder="tf",
    server_app=232250,
    server_binary="srcds_linux64",
    bin_dir="bin/linux64",
    test_map="ctf_2fort",
    module="stvwatch/games/tf2.py",
    # g_SendTableCRC of engine_srv.so of the build (tools/read_crc.py)
    crc_by_build={
        "11087207": 0x5FCD4733,
    },
    user_messages=(
        "Geiger",
        "Train",
        "HudText",
        "SayText",
        "SayText2",
        "TextMsg",
        "ResetHUD",
        "GameTitle",
        "ItemPickup",
        "ShowMenu",
        "Shake",
        "Fade",
        "VGUIMenu",
        "Rumble",
        "CloseCaption",
        "SendAudio",
        "VoiceMask",
        "RequestState",
        "Damage",
        "HintText",
        "KeyHintText",
        "HudMsg",
        "AmmoDenied",
        "AchievementEvent",
        "UpdateRadar",
        "VoiceSubtitle",
        "HudNotify",
        "HudNotifyCustom",
        "PlayerStatsUpdate",
        "MapStatsUpdate",
        "PlayerIgnited",
        "PlayerIgnitedInv",
        "HudArenaNotify",
        "UpdateAchievement",
        "TrainingMsg",
        "TrainingObjective",
        "DamageDodged",
        "PlayerJarated",
        "PlayerExtinguished",
        "PlayerJaratedFade",
        "PlayerShieldBlocked",
        "BreakModel",
        "CheapBreakModel",
        "BreakModel_Pumpkin",
        "BreakModelRocketDud",
        "CallVoteFailed",
        "VoteStart",
        "VotePass",
        "VoteFailed",
        "VoteSetup",
        "PlayerBonusPoints",
        "RDTeamPointsChanged",
        "SpawnFlyingBird",
        "PlayerGodRayEffect",
        "PlayerTeleportHomeEffect",
        "MVMStatsReset",
        "MVMPlayerEvent",
        "MVMResetPlayerStats",
        "MVMWaveFailed",
        "MVMAnnouncement",
        "MVMPlayerUpgradedEvent",
        "MVMVictory",
        "MVMWaveChange",
        "MVMLocalPlayerUpgradesClear",
        "MVMLocalPlayerUpgradesValue",
        "MVMResetPlayerWaveSpendingStats",
        "MVMLocalPlayerWaveSpendingValue",
        "MVMResetPlayerUpgradeSpending",
        "MVMServerKickTimeUpdate",
        "PlayerLoadoutUpdated",
        "PlayerTauntSoundLoopStart",
        "PlayerTauntSoundLoopEnd",
        "ForcePlayerViewAngles",
        "BonusDucks",
        "EOTLDuckEvent",
        "PlayerPickupWeapon",
        "QuestObjectiveCompleted",
        "SdkRequestEquipment",
        "BuiltObject",
        "SPHapWeapEvent",
        "HapDmg",
        "HapPunch",
        "HapSetDrag",
        "HapSetConst",
        "HapMeleeContact",
        "SavedConvar",
    ),
    chat_prefix="TF_Chat",
    teams={0: "unassigned", 1: "spectator", 2: "red", 3: "blu"},
    # the hl2mp set, as since the first TF2 session: the relay takes it
    userinfo=hl2dm.USERINFO,
    death_details=death_details,
)
