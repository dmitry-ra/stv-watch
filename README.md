# stv-watch

A SourceTV relay viewer for Half-Life 2: Deathmatch, in the terminal. It joins a
relay as a spectator, as the game client would, and shows what the relay sends:
chat, console messages, connects, joins, leaves, kills, team and nick changes,
server and SourceMod notices, the connection's state and its traffic. It can also
replay a recording at any speed. For scripts and agents, the same events come
out as one JSON object per line.

Every live session is recorded byte for byte (`capture.tvd`), and a replay of
that recording gives the same lines the live run printed.

Voice chat is not shown yet: voice messages are recorded and skipped.

## Requirements

- Linux. The network client is a child process that the kernel stops when the
  viewer dies (`PR_SET_PDEATHSIG`), and the screen uses `termios`.
- Python 3.12 or newer.
- [uv](https://docs.astral.sh/uv/). There are no runtime dependencies beyond the
  standard library; uv provides the interpreter and the environment.

## Install and run

```sh
git clone https://github.com/dmitry-ra/stv-watch.git
cd stv-watch
uv run stv-watch --help
```

The first `uv run` creates `.venv` in the clone. Run later commands from the same
directory, or pass `--project PATH_TO_CLONE` to `uv run`.

## Examples

Watch a relay live (the relay's address is its SourceTV port, often 27020):

```sh
uv run stv-watch --relay RELAY_IP:27020
```

The feed scrolls above a status block pinned to the bottom of the terminal; `q`
quits. Before connecting, stv-watch asks the relay (A2S_INFO) whether it can take
a spectator and refuses when it should not (see Responsible use).

Replay a recording, as fast as possible or at its own pace:

```sh
uv run stv-watch --replay tests/data/sample.tvd --speed 0
uv run stv-watch --replay ~/.local/share/stv-watch/sessions/SESSION/capture.tvd --speed 4
```

Choose the events (`--events`, default `chat,console,connect,join,leave,name`):

```sh
uv run stv-watch --relay RELAY_IP:27020 --events all
uv run stv-watch --relay RELAY_IP:27020 --events all,-server   # everything but server notices
uv run stv-watch --relay RELAY_IP:27020 --events chat,death
```

Times are in UTC. `--tz` takes an IANA zone name for the screen and the files
(the system time zone database, package `tzdata`, must be installed):

```sh
uv run stv-watch --relay RELAY_IP:27020 --tz Europe/Berlin
```

### For scripts and agents

`--json` prints one JSON object per line, flushed as each event happens; no
escape sequences, no status lines:

```sh
uv run stv-watch --relay RELAY_IP:27020 --json --events all
```

```json
{"t_utc": "2025-10-07T09:40:00.200Z", "type": "chat", "steamid64": 76561201960265729, "nick": "alice", "text": "hello", "channel": "all", "ent": 1}
```

The fields are `t_utc`, `t_local` (only with `--tz`), `type`, `steamid64`, `nick`,
`text` and the extra fields of the type; the format is
[docs/events.schema.json](docs/events.schema.json), described in
[docs/session-files.md](docs/session-files.md). `--monitor` gives the same events
as plain text lines. [AGENTS.md](AGENTS.md) is a guide for agents that run it.

Exit codes: 0 when the run ended (q, a signal, `--seconds`, end of a recording),
2 when the relay was refused before connecting or the command line is wrong, 3
when the network client stopped on its own (the relay refuses us for a reason
retrying will not change).

## Version

`uv run stv-watch --version` prints the version: the release from
`pyproject.toml`, and when run from a git checkout also the commit, as a PEP 440
local label: `0.1.0+g976b9ae`, or `0.1.0+g976b9ae.dirty` when tracked files
differ from that commit. Without git, or outside a checkout, it is the release
alone (`0.1.0`). Every run names the same string in the status block, at the end
of its first feed line (screen, `--plain`, `--monitor` and `feed.log`), as the
`version` field of its first JSON line, in `meta.json` and in each session
start record of `capture.tvd`.

## Session files

Each run writes a directory under `$XDG_DATA_HOME/stv-watch/sessions`
(`~/.local/share/stv-watch/sessions` when `XDG_DATA_HOME` is unset; `--out`
chooses another parent): `capture.tvd` (live only), `events.jsonl`, `feed.log`,
`meta.json`, `tvdump.log` (live only), `stderr.log`. See
[docs/session-files.md](docs/session-files.md).

## A server on an unknown build

To join, a client must send the SendTable CRC of the server's build. stv-watch
knows the CRCs of the builds in `stvwatch/net/builds.py` and refuses a relay on
any other build, naming the build number, instead of guessing (each wrong guess
would be a connection on someone else's server). After a game update, extract the
new CRC with `tools/read_crc.py` from a local dedicated server of that build and
add it to the table: [docs/protocol.md](docs/protocol.md#server-builds).

## Responsible use

- stv-watch takes a spectator slot on someone else's server. Before connecting it
  checks that at least one slot stays free for others after it joins
  (`--allow-last-slot` overrides that), it does not join relays with a password or
  a Steam-only login, and on every way out, including a crash of the viewer, it
  tells the relay it left, so the slot is freed at once rather than after the
  relay's 300 s timeout.
- It does not hammer a relay: a refused or dropped connection is retried with a
  growing delay, and a refusal that retrying will not change (wrong build,
  password, ban) is retried only every few minutes and ends the viewer.
- Do not stay connected to a server whose owners asked you not to.
- Recordings and logs contain other people's nicks, SteamIDs and chat, and the
  recording also holds their voice. Treat them as personal data: keep them to
  yourself and delete what you do not need.

## Development

```sh
uv run pytest -q
uv run black --check .
uv run ruff check .
```

The tests need no network beyond loopback and no game server.
`tools/local_srcds.py` starts a loopback dedicated server with SourceTV for
manual checks, `tools/tmux_check.sh` checks the screen in a real terminal.
How the protocol works: [docs/protocol.md](docs/protocol.md).

## License

MIT, see [LICENSE](LICENSE).
