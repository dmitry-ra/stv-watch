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

from make_sample import build

from stvwatch import events as ge
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
    clock and names the recording's path; the last one names the session."""
    out = []
    for ln in lines:
        r = json.loads(ln)
        if r["type"] == "play":
            r.pop("t_utc")
            r.pop("t_local", None)
            r["text"] = re.sub(r"^replay \S+", "replay SAMPLE", r["text"])
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
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} not in enum")
    if "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"{where}: {value!r} does not match {schema['pattern']}")
    if "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{where}: {value!r} below {schema['minimum']}")
    if t == "object":
        props = schema.get("properties", {})
        errors += [f"{where}: {k} missing" for k in schema.get("required", []) if k not in value]
        for k, v in value.items():
            if k in props:
                errors += check(v, props[k], f"{where}.{k}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{where}: unexpected {k}")
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


if __name__ == "__main__":
    import tempfile

    build(SAMPLE)
    with tempfile.TemporaryDirectory() as d:
        lines = normal(replay(d, *ARGS))
    with open(EXPECTED, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(SAMPLE, EXPECTED, len(lines), "lines")
