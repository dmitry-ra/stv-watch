# stv-watch

A SourceTV relay viewer for Half-Life 2: Deathmatch, in the terminal. It joins a
relay as a spectator, as the game client would, and shows what the relay sends:
who talks on voice chat and, with `--asr parakeet`, what they say; chat, console
messages, connects, joins, leaves, kills, team and nick changes, server and
SourceMod notices, the connection's state and its traffic. It can also replay a
recording at any speed. For scripts and agents, the same events come out as one
JSON object per line.

Every live session is recorded byte for byte (`capture.tvd`), and a replay of
that recording gives the same lines the live run printed.

## Requirements

- Linux. The network client is a child process that the kernel stops when the
  viewer dies (`PR_SET_PDEATHSIG`), and the screen uses `termios`.
- Python 3.12 to 3.14, x86-64. onnxruntime publishes wheels up to CPython
  3.14; on a newer Python the install stops at it with no matching wheel.
- [uv](https://docs.astral.sh/uv/). uv provides the interpreter and the
  environment with the dependencies, all wheels from PyPI (about 45 MB of
  downloads): numpy, opuslib-next-bundled (the Opus decoder, libopus inside),
  onnxruntime and onnx-asr (speech recognition).
- For `--asr parakeet`: 2.55 GB of disk for the model weights (Parakeet and
  Silero VAD) and about 2.6 GB of memory while it runs.

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

### Voice

Every utterance is a line: who talks, `talking N s` while the key is held, then
the final line when the speaker has been silent for a second. A monologue is cut
into pieces of at most two minutes (`--max-utt-ms`, default 120000, at most
400000: Parakeet takes at most 400 s in one call); later pieces are marked
`(cont)`. A piece ends in the middle of the longest pause in its last 40 %, the
pauses found by Silero VAD (with `--asr`); with no pause there, or without
`--asr`, the cut is hard at the limit. Each utterance is also saved as a WAV file and a row of
`transcript.tsv` (`--no-audio` turns the WAV files off).

To see what is said, add a recognizer:

```sh
uv run stv-watch --relay RELAY_IP:27020 --asr parakeet
```

The first run downloads the weights of Parakeet TDT 0.6B v3 (2.55 GB, about 25
European languages, Russian and English among them) and of Silero VAD (0.64 MB)
into `$XDG_CACHE_HOME/stv-watch/models` (`~/.cache/stv-watch/models` when it is
unset; `--models-dir` chooses another place). For each it prints one line with
the size and the revision first, fetches every file from its source at a pinned
revision (Hugging Face, GitHub) and checks its size and SHA-256 before using it;
a file that fails the check is deleted and the run stops. Later runs start from
the files on disk.
Recognition runs on the CPU (`--threads`, default 2) after an utterance ends,
so its text follows the speech by about a second. A replay that reaches its
end waits until every queued utterance is recognized; live, or stopped by a
signal or `q`, the exit waits at most `--drain-ms` (default 20000), and the
rest are marked `not recognized before exit`. `--drain-ms` bounds a replay's
wait too, and a signal or `q` during the wait ends it. The queue holds at most 600 s of audio: a replay waits for room, while
live an utterance that does not fit is marked `not recognized, queue full`.

Silero VAD stands in front of the model as a gate: an utterance with less
speech in it than `--min-speech-ms` (default 250) gets no text (`no speech`)
and is not recognized, because on noise Parakeet tends to make up an
interjection; `--min-speech-ms 0` turns the gate off. Each voice line carries
the speech the VAD found, `speech_ms`.

The SteamID a voice line names comes from the voice data, which the speaker's
own game writes; the slot the data came from is the server's, and the server's
`userinfo` table says who is in that slot. Each voice line checks one against
the other (`verified`): a mismatch, a client claiming someone else's SteamID,
is marked in red on screen with the slot and the nick of its real owner.

Durations in options and in the files are whole milliseconds, their names end
in `_ms` / `-ms`; moments are ISO times. The screen speaks seconds.

Without a recording at hand, `tests/voicegen.py` writes a synthetic one with
voice made of tones and noise (no speech):

```sh
uv run python tests/voicegen.py /tmp/voice.tvd
uv run stv-watch --replay /tmp/voice.tvd --speed 0 --asr parakeet
```

### Time zones

Times are in UTC. `--tz` takes an IANA zone name for the screen and the files
(the system time zone database, package `tzdata`, must be installed):

```sh
uv run stv-watch --relay RELAY_IP:27020 --tz Europe/Berlin
```

### A headless engine and its screens

`--serve SOCKET` runs the viewer without a terminal UI: it connects, records,
recognizes and writes its session files as always, and serves its screen on a
Unix socket (mode 0600) to any number of clients. `--attach SOCKET` is such a
client: the same screen, drawn from what the engine sends, with no connection to
the relay, no parsing and no model of its own. A name without `/` is a socket in
`$XDG_RUNTIME_DIR/stv-watch/`:

```sh
uv run stv-watch --relay RELAY_IP:27020 --asr parakeet --serve noob   # in tmux, a service, ...
uv run stv-watch --attach noob                                         # in any terminal, as often as you like
uv run stv-watch --attach noob --plain                                 # finished lines only
```

The attached screen looks like a local run, with one more line in the block:
`attach SOCKET  engine pid N`. On connect it gets the last 200 feed lines, the
voice lines still open and the block, then follows the engine. `q` closes only
that screen. When the engine goes away the screen waits for it, and when an
engine serves the socket again it reconnects by itself and says so in one feed
line. A client that does not keep up is dropped and reconnects; the engine never
waits for a screen. The engine's stdout gets its finished feed lines (as
`--plain` without status lines); a socket file left by an engine that died is
taken over, one that a live engine serves is refused.

### For scripts and agents

`--json` prints one JSON object per line, flushed as each event happens; no
escape sequences, no status lines:

```sh
uv run stv-watch --relay RELAY_IP:27020 --json --events all
```

```json
{"t_utc": "2025-10-07T09:40:00.200Z", "type": "chat", "steamid64": 76561201960265729, "nick": "alice", "text": "hello", "channel": "all", "ent": 1}
{"t_utc": "2025-10-07T09:40:09.060Z", "type": "voice", "steamid64": 76561201960265729, "nick": "alice", "text": "see you", "result": "text", "continued": false, "spectator": false, "details": "1.0s/0.9s fr 50 plc 0 gap 0 33kb/s press 1 msg 17 -2 0% arr 17 p50 60 max 60ms +1.1s asr 0.24s +0.9s", "t_end_utc": "2025-10-07T09:40:11.050Z"}
```

A voice line is written when the utterance is recognized, so it can come after
lines of later events; its `t_utc` is when the speaker started.

The fields are `t_utc`, `t_local` (only with `--tz`), `type`, `steamid64`, `nick`,
`text` and the extra fields of the type; the format is
[docs/events.schema.json](docs/events.schema.json), described in
[docs/session-files.md](docs/session-files.md). `--monitor` gives the same events
as plain text lines. [AGENTS.md](AGENTS.md) is a guide for agents that run it.

Exit codes: 0 when the run ended (q, a signal, `--duration-ms`, end of a recording),
2 when the relay was refused before connecting, the command line is wrong, no
engine serves the socket given to `--attach`, the socket of `--serve` is taken or the
recognizer could not start (its weights could not be fetched or checked, or the
model failed to load), 3 when the network client stopped on its own (the relay
refuses us for a reason retrying will not change).

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
`transcript.tsv`, `audio/` (a WAV per utterance), `meta.json`, `tvdump.log`
(live only), `stderr.log`. See [docs/session-files.md](docs/session-files.md).

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
- Recordings, WAV files, transcripts and logs contain other people's voices,
  nicks, SteamIDs and chat. Treat them as personal data: keep them to yourself
  and delete what you do not need. Recording or transcribing voice chat may need
  the consent of those who speak where you or they live.

## Development

```sh
uv run pytest -q
uv run black --check .
uv run ruff check .
```

The tests need no network beyond loopback, no game server and no model weights;
they use synthetic tones and noise, never speech. The test with the real model
runs on its own: `uv run pytest -m model` (it uses the weights in the default
place, or the directory in `STV_WATCH_MODELS`).
`tools/local_srcds.py` starts a loopback dedicated server with SourceTV for
manual checks, `tools/tmux_check.sh` checks the screen in a real terminal.
How the protocol works: [docs/protocol.md](docs/protocol.md).

Every pull request that changes `stvwatch/` raises `version` in `pyproject.toml`
(the patch number for a fix, the minor one for a feature) and refreshes `uv.lock`;
CI fails a pull request that does not.

## License

MIT, see [LICENSE](LICENSE).

The recognition model is not part of this repository: stv-watch downloads it
from its source. Parakeet TDT 0.6B v3 is by NVIDIA
([nvidia/parakeet-tdt-0.6b-v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)),
the ONNX export it uses is
[istupakov/parakeet-tdt-0.6b-v3-onnx](https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx);
both are under CC-BY-4.0. Silero VAD is by the Silero team
([snakers4/silero-vad](https://github.com/snakers4/silero-vad), MIT); the file
it uses, `silero_vad.onnx` (v4), is the ONNX export published by k2-fsa in the
`asr-models` release of [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx).
