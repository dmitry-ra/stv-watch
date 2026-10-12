# Session files

Every run of stv-watch writes one directory (with `--rotate`, one per period):

    $XDG_DATA_HOME/stv-watch/sessions/<UTC start>_<what>_<pid>/

`<UTC start>` is `YYYYMMDDTHHMMSSZ`; `<what>` is the relay (`IP_PORT`) live,
`replay-<name>` or `follow-<name>` otherwise. `--out DIR` replaces the parent.

With `--rotate CRON` a run goes on in a new directory at each time of the
schedule (5 fields, in the `--tz` zone, by the time of the recording's
records), named the same way with the start of its period as `<UTC start>`
(in a replay that is the recording's time, so those names sort before the
first one's; follow `next_dir`). Every file below goes on there; the
connection to the relay stays. A rotation cuts the stream of events at its
moment: every line before it is in the old session, every line from it on in
the new one (see [events.jsonl](#eventsjsonl)). The order is fixed: the new
directory and its files are made and opened, then the old `events.jsonl` gets
its last line (a `rotate` line naming the new one), then the old files are
closed; nothing is written to them after it. An utterance that starts before
the cut and closes after it has its `talk` line in the old session and its
`voice` line, `transcript.tsv` row and WAV in the new one. A period without any
record gets no directory.

With `--keep-trying` every network client after the first one of a session
starts a new directory the same way, named by the time it starts: the `rotate`
lines that link the two have `reason` `restart`, and so has the new
`meta.json`. A new client never appends to the journal of a dead one. A restart
cuts the stream like a rotation, at the moment the new client is started: the
old session keeps everything before it (the dead client's journal read to its
end, the utterances in it closed there, the refusal or death and the `wait`
line), the new one starts with its `rotate` line.

| File | Live | Replay | Content |
|---|---|---|---|
| `capture.tvd` | yes | - | the raw recording: every datagram both ways and the session lifecycle |
| `events.jsonl` | yes | yes | one JSON object per line, the same bytes `--json` prints |
| `feed.log` | yes | yes | the feed as text, tab separated, with full details |
| `transcript.tsv` | yes | yes | one row per utterance: who, when, the recognized text, its WAV |
| `audio/` | yes | yes | a WAV file per utterance (not with `--no-audio`; only once someone spoke) |
| `meta.json` | yes | yes | arguments, counters and how the session ended, written at its start and its end |
| `tvdump.log` | yes | - | the network client's own log |
| `stderr.log` | yes | yes | anything printed to stdout or stderr other than the screen; the traceback of an error in stv-watch itself |

`events.jsonl`, `feed.log`, `meta.json` and `capture.tvd` name the stv-watch
version that wrote them, as `stv-watch --version` prints it:
`0.1.0` for a plain release (also from a git checkout at the tag `v0.1.0` with
no changes), `0.1.0+g976b9ae` when run from a git checkout at that commit,
`0.1.0+g976b9ae.dirty` when tracked files differ from it.
`transcript.tsv` and `audio/` go by the session's version and carry none of
their own.

## events.jsonl

Written and flushed line by line in every screen mode, so a reader can tail it.
The schema is [events.schema.json](events.schema.json).

Lines are in the order of `t_utc` (never earlier than the line before), in one
file and across the sessions of a rotated run, and `--json` prints the same
lines. The clock is the recording's: the lines of a recording's events are the
same in the live run that made it and in a replay at any `--speed`, whatever
the recognizer's pace, but for what depends on the wall clock (the recognition
time in `details`, `asr_ms` and `lag_ms` in `transcript.tsv`, the load time and
speed in `asr` and `play` lines, the directories). Lines a replay makes before
its first record (`asr`, `play`) take that record's time. Live, while no
network client runs (before the first one, between two under `--keep-trying`),
the stream goes on the wall clock: a refusal, a `wait` line, a `done` line are
stamped when they happen. `done` is always the last line, its `t_utc` that of
the line before it or later. Without `--asr` each line is written as soon as the
stream is past its moment. With `--asr` the stream also waits for the text of
an utterance that has closed: nothing after its `voice` line is written until
the recognizer has answered (live usually under a second), while nothing waits
for an utterance still being spoken. Every line has:

| Field | Type | Meaning |
|---|---|---|
| `t_utc` | string | `YYYY-MM-DDTHH:MM:SS.mmmZ`, the moment of the event on the recording's clock: the receive time of the packet that carried it (`talk`: of the utterance's first voice data; `voice`: when the utterance closed) |
| `t_local` | string | `YYYY-MM-DD HH:MM:SS` in the `--tz` zone; only when `--tz` was given |
| `type` | string | see below |
| `steamid64` | integer | the player the line is about, 0 when none or unknown |
| `nick` | string | the player's nick as the stream names him |
| `text` | string | what happened |

The first line of a session (`asr` when `--asr` loads the recognizer first,
else `conn` live, `play` in a replay or `--follow`; `done` if the run ended
before either; `rotate` in a session a rotation opened) also has `version`,
the stv-watch version that wrote it. No other line has it.

Players are named by the stream itself: the nick is the current entry of the
`userinfo` string table, the SteamID64 comes from the same entry or from the
game event; nothing is looked up elsewhere.

Voice (always written, not filtered by `--events`): two lines per utterance,
paired by `utt`.

| type | extra fields | what |
|---|---|---|
| `talk` | `utt`, `start_utc`, `continued`, `slot`, `bot` | a player starts an utterance, or a piece of a monologue; `text` is empty |
| `voice` | `utt`, `start_utc`, `end_utc`, `result`, `continued`, `spectator`, `details`, `speech_ms`, `slot`, `verified`, `slot_steamid64`, `bot` | the utterance closed; `text` is what was recognized |

- `utt`: the utterance's number in the run (1, 2, ...); every `talk` line has
  exactly one `voice` line with the same `utt`, later in the same session or in
  the next one.
- `start_utc`: the utterance's first voice data. A `talk` line has it at its
  `t_utc`, except for a later piece of a monologue: that piece starts where the
  one before it was cut (its `end_utc`), and its `talk` line comes when the cut
  is made, right after the `voice` line of the piece before.
- `end_utc`: where the utterance's audio ends: its close, the same as `t_utc`;
  for a monologue piece the pause it was cut at.
- An utterance closes when a packet comes after more than a second without
  voice data of the player (or a new key press, a new connection, the end of
  the run); a monologue piece when it passes `--max-utt-ms`. The `voice` line's
  `t_utc` is that packet's time.

- `result`: `text` (`text` holds what was said), `no speech` (the voice
  activity detector found less speech than `--min-speech-ms`, or the model
  heard nothing), `asr off` (run without `--asr`), `not recognized before exit`
  (still queued when the exit stopped waiting, see `--drain-ms`), `not recognized, queue full` (live
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
- `slot`: the server slot its voice messages came from (with a mismatch, the
  slot of the first one that did not match); in a `talk` line, the slot of the
  voice data that started it.
- `verified`: whether the server's `userinfo` entry for that slot is the
  player whose SteamID the voice payload carries (`steamid64`). `true` when
  every message that could be checked matched; `false` when any did not;
  `null` when none could be checked (no `userinfo` table yet, or an empty
  slot). The screen marks `false` in red after the nick: `[slot 5: NICK]`,
  NICK being who the server had there; `--debug` adds `slot N` to the
  transport numbers of every voice line (and so does `feed.log`).
- `slot_steamid64`: only when `verified` is `false`: SteamID64 of the player
  in that slot (0 for one without an account).
- `bot`: only when the voice came from the slot of a bot (SourceTV or a bot:
  the server's fake client, guid `BOT`), always `true`, in both lines of the
  utterance. Such a slot has no Steam account to check, so the `voice` line
  has no `verified` (and no `slot_steamid64`); `nick` is the slot's, and
  `steamid64` is whatever the server signed the voice with (SourceTV: the
  server's own, an anonymous game server). The screen shows the name and a
  dim `[bot]`; a SteamID that is not a player's reads in Steam3, as the
  server's `status` prints it (`[A:1:1234567:4321]`).

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
reconnects, leaving, the slot check), `net` (traffic started, stopped (at the moment the silence passed the
threshold the line names), resumed;
with `--debug` also `lost: seq A -> B (N)` with `seq_from`, `seq_to`, `lost`),
`play` (replay started), `tvd` (a line of the network client's log, live only),
`rotate` (`--rotate`: the last line of a session, with `next_dir` and
`next_events`, the absolute paths of the next session and its `events.jsonl`,
and the first line of that one, with `prev_dir`, `prev_events` and `version`;
only the first is shown on the screen, `--json` prints both, so its output is
the sessions' `events.jsonl` one after the other),
`asr` (the recognizer loaded, failed to load, or failed on an utterance; with
`--live-asr` also the live recognizer: `nemotron live ready in ...`, `... live
failed: ...`, `... live error: ...`, or `... live off` when the run has no
screen to show its text on),
`wait` (`--keep-trying`: an attempt failed and the next one is due; below),
`done` (the last line: how the run ended and where its files are; below).
A `rotate` line of a restart under `--keep-trying` also has `reason`
`restart`.

| type | extra fields | what |
|---|---|---|
| `done` | `quit`, `class`, `cause`, `rc`, `retrying`, `tvdump_rc` | the run ends; `text` is `<quit> -> <session directory>` |
| `wait` | `quit`, `class`, `cause`, `attempt`, `next_try_utc`, `retrying` | `--keep-trying`: attempt `attempt` (1 for the first) failed, the next one is due at `next_try_utc` (earlier if the password file changes); `text` is `refused: <cause>, next try in M:SS` or `client died (rc N): <cause>, next try in M:SS` |

- `quit`: what ended the run or the attempt, the same label as in `meta.json`:
  `key q`, `SIGINT`, `SIGTERM`, `SIGHUP`, `time limit` (a replay's
  `--duration-ms`), `client left: time limit` (a live one's), `end of
  recording`, `precheck` (refused before connecting), `alarm` (a standing
  refusal), `client died (rc N)` (the network client's exit code, negative for
  a signal), `asr failed`, `password` (the password file or store could not be
  read after the start), `write error`, `crash`.
- `class`, one per exit code: `ok` 0 (done as asked), `bug` 1 (an error in
  stv-watch itself), `usage` 2, `refused` 3 (the relay does not take us),
  `client` 4 (the network client died), `io` 5 (the session cannot be written).
- `cause`: why, as text, empty for `ok`: the relay's refusal (`Bad spectator
  password`, `relay does not answer A2S_INFO`, ...), how the client died
  (`killed by SIGKILL`, `stopped from outside (exit code 0)`, the last line of
  its crash such as `RuntimeError: ...`), the exception of a bug (`Type:
  message`), the file that could not be written and why.
- `rc`: the exit code of the run. `retrying`: `false` on `done`, `true` on
  `wait`. `tvdump_rc`: the exit code of the last network client, null when
  none ran or it had to be killed.

`done` is written on every way out that leaves the process running long
enough, a crash included. It is missing only when the process was killed
(SIGKILL, the OOM killer) or `events.jsonl` itself could not be written; then
`meta.json` has no `end_utc`. While a run with `--keep-trying` waits, its last
line so far is the `wait` of the failed attempt; when that run ends, `done`
still follows it.

## feed.log

`<t_utc>\t<steamid64 or empty>\t<line>`, one line per event, UTF-8, in the order
of `events.jsonl` (`talk` lines are not in it). The line is what the screen
shows with `--debug`: a kill names both SteamIDs, the time is not repeated. The first line ends with `  [stv-watch VERSION]`.

## transcript.tsv

Tab separated, UTF-8, a header line, then one row per utterance, written with
its `voice` line and in the same order (tabs and line breaks inside a field
become spaces):

| Column | Meaning |
|---|---|
| `t_start_utc`, `t_end_utc` | `start_utc` and `end_utc` of the `voice` line: first frame and close of the utterance (a monologue piece ends where the next begins), the format of `t_utc` |
| `steamid64`, `nick` | the speaker |
| `model` | the recognizer (`parakeet`), empty without `--asr` |
| `result` | as in the `voice` line: `text`, `no speech`, `asr off`, `not recognized before exit`, `not recognized, queue full`, `recognition failed` |
| `text` | what was recognized, empty unless `result` is `text` |
| `audio` | the WAV file, relative to the session directory; empty with `--no-audio` |
| `speech_ms` | speech found by the VAD, as in the `voice` line; empty when not measured |
| `asr_ms` | milliseconds the recognizer spent on it |
| `lag_ms` | milliseconds of the recording's clock from the close of the utterance (of a monologue piece: from its cut) to its text |
| `t_local` | only with `--tz`: the start in that zone, `YYYY-MM-DD HH:MM:SS` |
| `slot`, `verified`, `slot_steamid64` | as in the `voice` line; `verified` is `true`, `false` or empty (null, and for a `bot` line), `slot_steamid64` empty unless `verified` is `false` |

## audio/

`HHMMSS_<steamid64>_<n>.wav`, written with the utterance's `voice` line, in
its session: 16 kHz mono 16-bit PCM of one utterance as decoded (lost frames concealed or filled with silence, loud peaks limited).
`HHMMSS` is the start in UTC, or in the `--tz` zone; `<n>` counts the speaker's
utterances in the run, so names do not repeat, across rotated sessions too.

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
| `0x17` | `--rotate`: the last record; the recording goes on in the file `path` (JSON), absolute |

`version` in a session start is the stv-watch version that wrote the record;
the event records are JSON, so it needed no change of the format, and files
written before it simply lack the field.

Voice messages stay in the recorded datagrams: a replay shows and recognizes
them as a live run does. A file cut by a crash is readable up to the cut.
With `--rotate` the network client cuts the file by the receive time of the
packets, at the moment of the cut of the events: the voice data of an utterance
that closes after the cut, and so has its `voice` line in a later session, is
partly in a later file too. A rotated file starts in the middle of a
connection, with no handshake or string tables of its own: `--replay` of it shows voice but not who is who
before the next map change, and a replay ends with the file it was given;
`--follow` goes on into the next file.
`stv-watch --replay` also reads the older `.hcap` format (magic `HLTVCAP`,
datagrams only).

## meta.json

Written when the session starts and again when it ends (at exit, or at a
rotation), each time whole (a new file renamed over the old one), so a full
disk leaves the earlier one: `version`, `build` (`version`, `release`, `commit` = the full
hash, `dirty`; the last two null outside a git checkout), `args` (the command
line), `model` (the `--asr` engine or null), `dir`, `tz`, `pid`, `start_utc`,
`end_utc`, `quit` (as in the `done` line, or `rotated`), `class`, `cause` and
`rc` (as in the `done` line; not in a session a rotation ended),
`tvdump_rc` (exit code of the last network client; null when none ran or it
had to be killed),
`traffic`, `framer` (receive path counters: `seq_lost`, `seq_choked`,
`resend_skipped`, `voice_msgs`, `voice_crc_bad`, packet fates, ...), `segments`
(speech segmenter counters), `counters` (`utterances`, `phrases` with text,
`nospeech`, `wav` files, `payload_bad` voice messages that failed the Steam
voice check), `conn` (last connection state; `state_utc` and `full_utc` are
when it was entered and when it reached FULL), `game_events` (counts per type,
shown or not), `speakers` (per SteamID64: `nick`, `audio_ms`, Opus `frames`,
`utterances`, `phrases`). With `--asr` also `asr`: `state`, `error`, `load_ms`,
`audio_ms` and `compute_ms` recognized, `jobs`. With `--live-asr` on a screen
also `live_asr`: `engine`, `state`, `error`, `load_ms`, `audio_ms` and
`compute_ms` decoded, `dropped_ms` of audio dropped when it fell behind and
`drops`. Its partial text is in no file. Every duration is whole
milliseconds named `*_ms`; `args` holds the options under their names
(`duration_ms`, `skip_ms`, ...).
A session a rotation ended has `next_dir`; one a rotation opened has
`prev_dir` and `rotated_utc` (the time of the record it starts with), and
`reason` `restart` when a `--keep-trying` restart opened it. The
counters (`traffic` to `live_asr`) are totals since the run started, in every
session of it.
Live runs also have `slot_released`, `relay_before`, `relay_with_us` and
`relay_after`: the relay's spectator counts used to check that our slot was
freed.
