# Session files

Every run of stv-watch writes one directory:

    $XDG_DATA_HOME/stv-watch/sessions/<UTC start>_<what>_<pid>/

`<UTC start>` is `YYYYMMDDTHHMMSSZ`; `<what>` is the relay (`IP_PORT`) live,
`replay-<name>` or `follow-<name>` otherwise. `--out DIR` replaces the parent.

| File | Live | Replay | Content |
|---|---|---|---|
| `capture.tvd` | yes | - | the raw recording: every datagram both ways and the session lifecycle |
| `events.jsonl` | yes | yes | one JSON object per line, the same bytes `--json` prints |
| `feed.log` | yes | yes | the feed as text, tab separated, with full details |
| `transcript.tsv` | yes | yes | one row per utterance: who, when, the recognized text, its WAV |
| `audio/` | yes | yes | a WAV file per utterance (not with `--no-audio`; only once someone spoke) |
| `meta.json` | yes | yes | arguments, counters and how the run ended, written at exit |
| `tvdump.log` | yes | - | the network client's own log |
| `stderr.log` | yes | yes | anything printed to stdout or stderr other than the screen |

`events.jsonl`, `feed.log`, `meta.json` and `capture.tvd` name the stv-watch
version that wrote them, as `stv-watch --version` prints it:
`0.1.0` for a plain release, `0.1.0+g976b9ae` when run from a git checkout at
that commit, `0.1.0+g976b9ae.dirty` when tracked files differ from it.
`transcript.tsv` and `audio/` go by the session's version and carry none of
their own.

## events.jsonl

Written and flushed line by line in every screen mode, so a reader can tail it.
The schema is [events.schema.json](events.schema.json). Every line has:

| Field | Type | Meaning |
|---|---|---|
| `t_utc` | string | `YYYY-MM-DDTHH:MM:SS.mmmZ`, receive time of the packet that carried the event (voice: of the utterance's first frame) |
| `t_local` | string | `YYYY-MM-DD HH:MM:SS` in the `--tz` zone; only when `--tz` was given |
| `type` | string | see below |
| `steamid64` | integer | the player the line is about, 0 when none or unknown |
| `nick` | string | the player's nick as the stream names him |
| `text` | string | what happened |

The first line of a session (`asr` when `--asr` loads the recognizer first,
else `conn` live, `play` in a replay or `--follow`; `done` if the run ended
before either) also has `version`, the stv-watch version that wrote it. No
other line has it.

Players are named by the stream itself: the nick is the current entry of the
`userinfo` string table, the SteamID64 comes from the same entry or from the
game event; nothing is looked up elsewhere.

Voice (always shown, not filtered by `--events`):

| type | extra fields | what |
|---|---|---|
| `voice` | `result`, `continued`, `spectator`, `details`, `t_end_utc`, `speech_ms`, `slot`, `verified`, `slot_steamid64` | one utterance, or one piece of a monologue; `text` is what was recognized |

- `result`: `text` (`text` holds what was said), `no speech` (the voice
  activity detector found less speech than `--min-speech-ms`, or the model
  heard nothing), `asr off` (run without `--asr`), `not recognized before exit`
  (still queued when `--drain-ms` ran out), `not recognized, queue full` (live
  only: 600 s of audio was already waiting for the recognizer; a replay waits
  for room instead), `recognition failed` (the engine raised an error on it;
  the `asr` line before it says which).
- `continued`: a piece of a monologue after the first; pieces are cut at the
  longest pause in their last 40 %, at most `--max-utt-ms` long.
- `speech_ms`: milliseconds of speech Silero VAD found in the utterance
  (32 ms windows above 0.5); only when the recognizer looked at it.
- `spectator`: the speaker is a spectator and `sv_alltalk` is off, so the
  players in game did not hear him.
- `details`: audio seconds / seconds from first to last frame, Opus frames
  (`fr`), frames concealed by the decoder (`plc`) and filled with silence
  (`gap`), bit rate, key presses, voice messages and their share in split
  packets, distinct arrivals with the median and largest gap between them, why
  the utterance closed when not by the clock (`max_len`, `pause`, `session`,
  `end`) and how long after its last frame; with `--asr` also the recognition
  time and how far the text was behind the speech.
- `t_end_utc`: when the utterance closed.
- `slot`: the server slot its voice messages came from (with a mismatch, the
  slot of the first one that did not match).
- `verified`: whether the server's `userinfo` entry for that slot is the
  player whose SteamID the voice payload carries (`steamid64`). `true` when
  every message that could be checked matched; `false` when any did not;
  `null` when none could be checked (no `userinfo` table yet, or an empty
  slot). The screen marks `false` in red after the nick: `[slot 5: NICK]`,
  NICK being who the server had there; `--debug` adds `slot N` to the
  transport numbers of every voice line (and so does `feed.log`).
- `slot_steamid64`: only when `verified` is `false`: SteamID64 of the player
  in that slot (0 for a bot).

A voice line is written when its utterance closes (without `--asr`) or when its
recognition returns, so voice lines are not in time order with the rest; sort by
`t_utc` if order matters.

Game event types (filtered by `--events`) and their extra fields:

| type | extra fields | what |
|---|---|---|
| `chat` | `channel`, `ent` | a chat line; `channel` is the game's channel (`all`, `team`, `alldead`, `allspec`, ...), `plugin` when a server plugin formatted it, `saytext` for SayText |
| `console` | - | a message from the server console |
| `connect` | `userid` | a player appeared in the `userinfo` table (is connecting) |
| `join` | - | a player entered the game (not repeated for those who stay over a map change) |
| `leave` | `userid`, `networkid`, `bot` | a player left; `text` is the reason |
| `death` | `weapon`, `userid`, `attacker`, `victim`, `victim_steamid64` | a kill; the line is about the killer, `victim*` only for a kill by another player |
| `team` | `team`, `oldteam`, `userid` | a team change (0 unassigned, 1 spectator, 2 combine, 3 rebels) |
| `name` | `old`, `userid` | a nick change |
| `server` | `via`, `dest` or `cvar`, `value` | a server notice (TextMsg, HudMsg) or a changed console variable |
| `sourcemod` | `via` | a notice of a SourceMod plugin |

The viewer's own lines: `conn` (connecting, signon, map change, breaks,
reconnects, leaving, the slot check), `net` (traffic started, stopped, resumed;
with `--debug` also `lost: seq A -> B (N)` with `seq_from`, `seq_to`, `lost`),
`play` (replay started), `tvd` (a line of the network client's log, live only),
`asr` (the recognizer loaded, failed to load, or failed on an utterance),
`done` (the last line: why the run ended and where its files are).

## feed.log

`<t_utc>\t<steamid64 or empty>\t<line>`, one line per event, UTF-8. The line is
what the screen shows with `--debug`: a kill names both SteamIDs, the time is
not repeated. The first line ends with `  [stv-watch VERSION]`.

## transcript.tsv

Tab separated, UTF-8, a header line, then one row per utterance in the order
they were finished (tabs and line breaks inside a field become spaces):

| Column | Meaning |
|---|---|
| `t_start_utc`, `t_end_utc` | first frame and close of the utterance (a monologue piece ends where the next begins), the format of `t_utc` |
| `steamid64`, `nick` | the speaker |
| `model` | the recognizer (`parakeet`), empty without `--asr` |
| `result` | as in the `voice` line: `text`, `no speech`, `asr off`, `not recognized before exit`, `not recognized, queue full`, `recognition failed` |
| `text` | what was recognized, empty unless `result` is `text` |
| `audio` | the WAV file, relative to the session directory; empty with `--no-audio` |
| `speech_ms` | speech found by the VAD, as in the `voice` line; empty when not measured |
| `asr_ms` | milliseconds the recognizer spent on it |
| `lag_ms` | milliseconds from the close of the utterance (of a monologue piece: from its cut) to its line |
| `t_local` | only with `--tz`: the start in that zone, `YYYY-MM-DD HH:MM:SS` |
| `slot`, `verified`, `slot_steamid64` | as in the `voice` line; `verified` is `true`, `false` or empty (null), `slot_steamid64` empty unless `verified` is `false` |

## audio/

`HHMMSS_<steamid64>_<n>.wav`: 16 kHz mono 16-bit PCM of one utterance as
decoded (lost frames concealed or filled with silence, loud peaks limited).
`HHMMSS` is the start in UTC, or in the `--tz` zone; `<n>` counts the speaker's
utterances in the session, so names do not repeat.

## capture.tvd

Little-endian. Header: `"TVDUMP\n"`, `u16` version (1), `u64` start time (UTC
epoch ns), `u16` length and the relay's address. Then records:
`u64 t_ns | u8 type | u32 length | data`, times in UTC epoch nanoseconds.

| type | record |
|---|---|
| `0x01` | datagram received, raw bytes |
| `0x02` | datagram sent, raw bytes |
| `0x10` | session started (JSON: `session`, `attempt`, `endpoint`, `version`) |
| `0x11` | signon state reached (JSON: `state`, `name`) |
| `0x12` | session broken (JSON: `cause`, `detail`) |
| `0x13` | connection attempt (JSON: `attempt`, `ok`, `error`) |
| `0x14` | map change (JSON: `map`) |
| `0x15` | a `-2` split part was seen (JSON: `length`) |
| `0x16` | we left (JSON: `why`, `sent` = net_Disconnect copies) |

`version` in a session start is the stv-watch version that wrote the record;
the event records are JSON, so it needed no change of the format, and files
written before it simply lack the field.

Voice messages stay in the recorded datagrams: a replay shows and recognizes
them as a live run does. A file cut by a crash is readable up to the cut.
`stv-watch --replay` also reads the older `.hcap` format (magic `HLTVCAP`,
datagrams only).

## meta.json

Written at exit: `version`, `build` (`version`, `release`, `commit` = the full
hash, `dirty`; the last two null outside a git checkout), `args` (the command
line), `model` (the `--asr` engine or null), `dir`, `tz`, `pid`, `start_utc`,
`end_utc`, `quit` (`end of recording`, `duration`, `key q`, `SIGINT`,
`SIGTERM`, `SIGHUP`, `alarm`, `client exited`, `precheck`, `asr failed`),
`tvdump_rc` (exit code of the network client; null when it had to be killed),
`traffic`, `framer` (receive path counters: `seq_lost`, `seq_choked`,
`resend_skipped`, `voice_msgs`, `voice_crc_bad`, packet fates, ...), `segments`
(speech segmenter counters), `counters` (`utterances`, `phrases` with text,
`nospeech`, `wav` files, `payload_bad` voice messages that failed the Steam
voice check), `conn` (last connection state; `state_utc` and `full_utc` are
when it was entered and when it reached FULL), `game_events` (counts per type,
shown or not), `speakers` (per SteamID64: `nick`, `audio_ms`, Opus `frames`,
`utterances`, `phrases`). With `--asr` also `asr`: `state`, `error`, `load_ms`,
`audio_ms` and `compute_ms` recognized, `jobs`. Every duration is whole
milliseconds named `*_ms`; `args` holds the options under their names
(`duration_ms`, `skip_ms`, ...).
Live runs also have `slot_released`, `relay_before`, `relay_with_us` and
`relay_after`: the relay's spectator counts used to check that our slot was
freed.
