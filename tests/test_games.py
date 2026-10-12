"""Game profiles: the CRC is looked up by game and build, the relay's game is
checked before connecting, the game is recorded in every capture file and a
replay takes its profile from there."""

import json
import os
import signal
import subprocess
import sys

import pytest
from fakerelay import FakeRelay

from stvwatch import cli, events, games
from stvwatch.app import App
from stvwatch.net import builds, client, dump, supervisor
from stvwatch.stream import streamevents
from stvwatch.stream.recording import Recording, game_of

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, "data", "sample.tvd")
SAMPLE_TF2 = os.path.join(HERE, "data", "sample_tf2.tvd")
STV = [sys.executable, "-B", "-m", "stvwatch.cli"]
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
TF2_BUILD = "11087207"


def stv(*args, timeout=60):
    p = subprocess.run(STV + list(args), capture_output=True, timeout=timeout, env=ENV)
    return p.returncode, [json.loads(ln) for ln in p.stdout.decode().splitlines()], p.stderr


def test_the_crc_is_the_build_of_that_game():
    assert builds.crc_for(TF2_BUILD, "tf2") == 0x5FCD4733
    assert builds.crc_for("10889068", "hl2dm") == 0xD9B6082D
    with pytest.raises(builds.UnknownBuild, match="of hl2dm: add its CRC to stvwatch/games/hl2dm"):
        builds.crc_for(TF2_BUILD, "hl2dm")
    with pytest.raises(builds.UnknownBuild, match="10889068 of tf2: .*stvwatch/games/tf2.py"):
        builds.crc_for("10889068", "tf2")
    assert builds.classify("unknown build 1 of tf2: ...") == builds.PERMANENT


def test_profiles_by_name_and_by_folder():
    assert games.get(games.DEFAULT).name == "hl2dm"
    assert [games.by_folder(f).name for f in ("hl2mp", "tf")] == ["hl2dm", "tf2"]
    assert games.by_folder("cstrike") is None
    with pytest.raises(ValueError, match="unknown game 'cs'"):
        games.get("cs")


def test_below_the_command_line_a_forgotten_game_is_an_error_not_hl2dm(tmp_path):
    """The default is the command line's (cli.py, net/main.py); everything
    under it wants the game named."""
    assert cli.parse_args(["--relay", "127.0.0.1:1"]).game == games.DEFAULT == "hl2dm"
    with pytest.raises(ValueError, match="no game given"):
        games.get(None)
    with pytest.raises(ValueError, match="no game given"):
        supervisor.Options(build="10889068")
    a = cli.parse_args(["--relay", "127.0.0.1:1", "--out", str(tmp_path / "o")])
    a.game = None
    with pytest.raises(ValueError, match="no game given"):
        App(a)


@pytest.mark.parametrize(
    "call",
    [
        lambda _d: events.GameEvents(),
        lambda _d: streamevents.Collector(),
        lambda d: dump.DumpWriter(str(d / "x.tvd"), "127.0.0.1:1"),
        lambda _d: client.precheck("127.0.0.1:1"),
        lambda _d: builds.known("10889068"),
        lambda _d: builds.crc_for("10889068"),
    ],
    ids=["GameEvents", "Collector", "DumpWriter", "precheck", "known", "crc_for"],
)
def test_no_consumer_of_a_profile_has_a_default_one(tmp_path, call):
    with pytest.raises(TypeError, match="'game'|'profile'"):
        call(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_tf2_user_message_ids_seen_on_live_relays():
    """Ids seen on TF2 relays (2026-10-11), named by the profile as
    tools/read_usermessages.py read them from server_srv.so."""
    tf2 = games.get("tf2")
    names = [tf2.user_message(i) for i in (3, 4, 5, 19, 20, 21, 57, 70)]
    assert names == [
        "SayText",
        "SayText2",
        "TextMsg",
        "HintText",
        "KeyHintText",
        "HudMsg",
        "MVMResetPlayerStats",
        "PlayerTauntSoundLoopStart",
    ]
    assert tf2.user_message(86) == "#86"
    assert games.get("hl2dm").user_message(6) == "HudMsg"


def test_every_capture_file_names_its_game_and_the_stream_skips_it(tmp_path):
    first, second = str(tmp_path / "a.tvd"), str(tmp_path / "b.tvd")
    w = dump.DumpWriter(
        first, "127.0.0.1:1", fsync_ms=0, game="tf2", next_path=lambda t: second if t > 5 else None
    )
    for t in (1, 2, 9):
        w.write(dump.DATAGRAM_IN, b"x%d" % t, t_ns=t)
    w.close()
    assert game_of(first) == game_of(second) == "tf2"
    assert [d.data for d in Recording(first)] == [b"x1", b"x2"]
    seen = []
    assert [d.data for d in Recording(second, on_event=lambda *e: seen.append(e))] == [b"x9"]
    assert seen == []
    plain = str(tmp_path / "c.tvd")
    with dump.DumpWriter(plain, "127.0.0.1:1", fsync_ms=0, game=None) as w:
        w.write(dump.DATAGRAM_IN, b"y", t_ns=1)
    assert (game_of(plain), game_of(SAMPLE), game_of(SAMPLE_TF2)) == (None, None, "tf2")


def test_a_replay_takes_the_game_of_the_recording(tmp_path):
    rc, lines, _ = stv(
        "--replay",
        SAMPLE_TF2,
        "--speed",
        "0",
        "--json",
        "--events",
        "all",
        "--out",
        str(tmp_path / "a"),
    )
    assert rc == 0
    (session,) = (tmp_path / "a").iterdir()
    assert json.loads((session / "meta.json").read_text())["game"] == "tf2"
    assert "team bob team spectator -> blu" in [
        f"{x['type']} {x['nick']} {x['text']}" for x in lines
    ]
    rc, lines, err = stv(
        "--replay", SAMPLE_TF2, "--game", "hl2dm", "--json", "--out", str(tmp_path / "b")
    )
    assert (rc, lines) == (2, [])
    assert b"the recording is of tf2, not --game hl2dm" in err
    rc, _, _ = stv(
        "--replay", SAMPLE, "--game", "tf2", "--speed", "0", "--json", "--out", str(tmp_path / "c")
    )
    (session,) = (tmp_path / "c").iterdir()
    assert (rc, json.loads((session / "meta.json").read_text())["game"]) == (0, "tf2")


@pytest.mark.parametrize(
    "folder, game, rc, text",
    [
        ("tf", None, 2, "relay serves tf2, not hl2dm (A2S folder tf): use --game tf2"),
        ("hl2mp", "tf2", 2, "relay serves hl2dm, not tf2 (A2S folder hl2mp): use --game hl2dm"),
        ("cstrike", None, 3, "relay serves the game 'cstrike', which stv-watch does not know"),
    ],
)
def test_a_relay_of_another_game_is_refused_before_anything_is_sent(
    tmp_path, folder, game, rc, text
):
    extra = ["--game", game] if game else []
    with FakeRelay(folder=folder, version=TF2_BUILD) as r:
        got, lines, _ = stv("--relay", r.addr, "--json", "--out", str(tmp_path), *extra)
        assert (got, r.challenges, r.connections) == (rc, 0, 0)
    (refused,) = [x for x in lines if x["text"].startswith("refused:")]
    assert text in refused["text"]
    assert lines[-1]["class"] == {2: "usage", 3: "refused"}[rc]


def test_a_live_tf2_run_records_its_game(tmp_path):
    out = tmp_path / "out"
    with FakeRelay(folder="tf", version=TF2_BUILD) as r:
        p = subprocess.Popen(
            STV + ["--relay", r.addr, "--game", "tf2", "--json", "--out", str(out)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=ENV,
        )
        try:
            assert r.wait(lambda: r.connected() == 1)
            p.send_signal(signal.SIGINT)
            p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
    assert p.returncode == 0
    (session,) = out.iterdir()
    assert game_of(str(session / "capture.tvd")) == "tf2"
    meta = json.loads((session / "meta.json").read_text())
    assert (meta["game"], meta["args"]["game"]) == ("tf2", "tf2")
    assert f"[build] {TF2_BUILD} crc=0x5FCD4733" in (session / "tvdump.log").read_text()
