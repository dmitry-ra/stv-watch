"""The synthetic recording in data/: what its replay must give, line for line,
and the JSON line format checked against docs/events.schema.json.

After a deliberate change of the output, regenerate the expectation with
`uv run python tests/test_sample.py` and review the diff.
"""

import json
import os
import re
import subprocess
import sys

import pytest
from make_sample import build

from stvwatch import events as ge
from stvwatch import version
from stvwatch.net import dump

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, "data", "sample.tvd")
EXPECTED = os.path.join(HERE, "data", "sample.events.jsonl")
SCHEMA = os.path.join(os.path.dirname(HERE), "docs", "events.schema.json")
ARGS = ["--speed", "0", "--json", "--events", "all", "--debug", "--tz", "Europe/Berlin"]
VIEWER_TYPES = ("conn", "net", "play", "tvd", "done")


def replay(out, *extra):
    cmd = [sys.executable, "-B", "-m", "stvwatch.cli", "--replay", SAMPLE, "--out", str(out)]
    p = subprocess.run(
        cmd + list(extra),
        capture_output=True,
        timeout=60,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )
    assert p.returncode == 0, p.stderr
    return p.stdout.decode().splitlines()


def normal(lines):
    """Run-dependent parts out: the replay's first line is written on the wall
    clock and names the recording's path and the build; the last one names the
    session."""
    out = []
    for ln in lines:
        r = json.loads(ln)
        if r["type"] == "play":
            r.pop("t_utc")
            r.pop("t_local", None)
            r["text"] = re.sub(r"^replay \S+", "replay SAMPLE", r["text"])
            if r.get("version") == version.version():
                r["version"] = "VERSION"
        if r["type"] == "done":
            r["text"] = r["text"].split(" -> ")[0]
        out.append(json.dumps(r, ensure_ascii=False))
    return out


def test_the_sample_is_what_the_generator_writes(tmp_path):
    fresh = str(tmp_path / "fresh.tvd")
    build(fresh)
    a, b = dump.DumpReader(SAMPLE), dump.DumpReader(fresh)
    assert a.endpoint == b.endpoint
    assert list(a) == list(b)


def test_replay_of_the_sample_gives_the_expected_events(tmp_path):
    lines = replay(tmp_path / "o", *ARGS)
    with open(EXPECTED, encoding="utf-8") as f:
        assert normal(lines) == f.read().splitlines()
    (session,) = list((tmp_path / "o").iterdir())
    assert (session / "events.jsonl").read_text(encoding="utf-8").splitlines() == lines


def test_replay_seconds_bound_what_is_shown_even_at_max_speed(tmp_path):
    """The sample's first datagram is at 0.100 s and its last line at 6.100 s:
    with --seconds 1 nothing after 1.100 s is shown, and the run says why it
    ended."""
    args = ("--speed", "0", "--seconds", "1", "--json", "--events", "all")
    lines = [json.loads(ln) for ln in replay(tmp_path, *args)]
    shown = max(r["t_utc"] for r in lines if r["type"] not in ("play", "done"))
    assert (shown, lines[-1]["text"].split(" -> ")[0]) == ("2025-10-07T09:40:01.100Z", "seconds")


def test_the_build_is_named_once_at_the_start_and_in_meta(tmp_path):
    """A reader of only events.jsonl, of only feed.log or of meta.json learns
    which build wrote the session."""
    v = version.version()
    lines = [json.loads(ln) for ln in replay(tmp_path / "j", "--speed", "0", "--json")]
    assert [r.get("version") for r in lines] == [v] + [None] * (len(lines) - 1)
    (session,) = list((tmp_path / "j").iterdir())
    meta = json.loads((session / "meta.json").read_text())
    assert (meta["version"], meta["build"]) == (v, version.build())
    log = (session / "feed.log").read_text().splitlines()
    shown = replay(tmp_path / "m", "--speed", "0", "--monitor")
    tag = f"  [stv-watch {v}]"
    assert [ln.endswith(tag) for ln in log] == [True] + [False] * (len(log) - 1)
    assert [ln.endswith(tag) for ln in shown] == [True] + [False] * (len(shown) - 1)


def check(value, schema, where="$"):
    """The subset of JSON Schema the event schema uses."""
    errors = []
    kinds = {"string": str, "object": dict, "boolean": bool}
    t = schema.get("type")
    if t == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif t is not None:
        ok = isinstance(value, kinds[t])
    else:
        ok = True
    if not ok:
        return [f"{where}: {value!r} is not {t}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: {value!r} is not {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} not in enum")
    if "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"{where}: {value!r} does not match {schema['pattern']}")
    if "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{where}: {value!r} below {schema['minimum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errors += [f"{where}: {k} missing" for k in schema.get("required", []) if k not in value]
        for k, v in value.items():
            if k in props:
                errors += check(v, props[k], f"{where}.{k}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{where}: unexpected {k}")
        for sub in schema.get("allOf", []):
            if not check(value, sub["if"], where):
                errors += check(value, sub["then"], where)
        for k, sub in schema.get("dependentSchemas", {}).items():
            if k in value and check(value, sub, where):
                errors.append(f"{where}: {k} on {value.get('type')!r}")
        for k, need in schema.get("dependentRequired", {}).items():
            errors += [f"{where}: {k} without {n}" for n in need if k in value and n not in value]
    return errors


def test_every_line_matches_the_schema(tmp_path):
    with open(SCHEMA, encoding="utf-8") as f:
        schema = json.load(f)
    assert schema["properties"]["type"]["enum"] == list(ge.TYPES + VIEWER_TYPES)
    with_tz = replay(tmp_path / "a", *ARGS)
    plain = replay(tmp_path / "b", "--speed", "0", "--json", "--events", "all")
    seen = set()
    for ln in with_tz + plain:
        r = json.loads(ln)
        assert check(r, schema) == [], ln
        seen.add(r["type"])
    assert set(ge.TYPES) <= seen
    assert all("t_local" in json.loads(ln) for ln in with_tz)
    assert not any("t_local" in json.loads(ln) for ln in plain)
    assert check({"t_utc": "x", "type": "kill", "steamid64": -1, "nick": 1, "z": 0}, schema) == [
        "$: text missing",
        "$.t_utc: 'x' does not match " + schema["properties"]["t_utc"]["pattern"],
        "$.type: 'kill' not in enum",
        "$.steamid64: -1 below 0",
        "$.nick: 1 is not string",
        "$: unexpected z",
    ]


CHAT = {
    "t_utc": "2025-10-07T09:40:00.200Z",
    "type": "chat",
    "steamid64": 0,
    "nick": "a",
    "text": "",
}
DEATH = dict(CHAT, type="death", weapon="slam", userid=2, attacker=3)


@pytest.mark.parametrize(
    "line, errors",
    [
        (dict(CHAT, channel="all", ent=1), []),
        (CHAT, ["$: channel missing", "$: ent missing"]),
        (dict(CHAT, channel="all", ent=1, weapon="slam"), ["$: weapon on 'chat'"]),
        (dict(DEATH, victim="bob", victim_steamid64=0), []),
        (dict(DEATH, victim="bob"), ["$: victim without victim_steamid64"]),
    ],
)
def test_the_schema_holds_each_type_to_its_own_fields(line, errors):
    with open(SCHEMA, encoding="utf-8") as f:
        assert check(line, json.load(f)) == errors


if __name__ == "__main__":
    import tempfile

    build(SAMPLE)
    with tempfile.TemporaryDirectory() as d:
        lines = normal(replay(d, *ARGS))
    with open(EXPECTED, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(SAMPLE, EXPECTED, len(lines), "lines")
