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
| `meta.json` | yes | yes | arguments, counters and how the run ended, written at exit |
| `tvdump.log` | yes | - | the network client's own log |
| `stderr.log` | yes | yes | anything printed to stdout or stderr other than the screen |

## events.jsonl

Written and flushed line by line in every screen mode, so a reader can tail it.
The schema is [events.schema.json](events.schema.json). Every line has:

| Field | Type | Meaning |
|---|---|---|
| `t_utc` | string | `YYYY-MM-DDTHH:MM:SS.mmmZ`, receive time of the packet that carried the event |
| `t_local` | string | `YYYY-MM-DD HH:MM:SS` in the `--tz` zone; only when `--tz` was given |
| `type` | string | see below |
| `steamid64` | integer | the player the line is about, 0 when none or unknown |
| `nick` | string | the player's nick as the stream names him |
| `text` | string | what happened |

Players are named by the stream itself: the nick is the current entry of the
`userinfo` string table, the SteamID64 comes from the same entry or from the
game event; nothing is looked up elsewhere.

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
`done` (the last line: why the run ended and where its files are).

## feed.log

`<t_utc>\t<steamid64 or empty>\t<line>`, one line per event, UTF-8. The line is
what the screen shows with `--debug`: a kill names both SteamIDs, the time is
not repeated.

## capture.tvd

Little-endian. Header: `"TVDUMP\n"`, `u16` version (1), `u64` start time (UTC
epoch ns), `u16` length and the relay's address. Then records:
`u64 t_ns | u8 type | u32 length | data`, times in UTC epoch nanoseconds.

| type | record |
|---|---|
| `0x01` | datagram received, raw bytes |
| `0x02` | datagram sent, raw bytes |
| `0x10` | session started (JSON: `session`, `attempt`, `endpoint`) |
| `0x11` | signon state reached (JSON: `state`, `name`) |
| `0x12` | session broken (JSON: `cause`, `detail`) |
| `0x13` | connection attempt (JSON: `attempt`, `ok`, `error`) |
| `0x14` | map change (JSON: `map`) |
| `0x15` | a `-2` split part was seen (JSON: `length`) |
| `0x16` | we left (JSON: `why`, `sent` = net_Disconnect copies) |

Voice messages stay in the recorded datagrams: a later version can replay old
recordings with voice. A file cut by a crash is readable up to the cut.
`stv-watch --replay` also reads the older `.hcap` format (magic `HLTVCAP`,
datagrams only).

## meta.json

Written at exit: `args` (the command line), `dir`, `tz`, `pid`, `start_utc`,
`end_utc`, `quit` (`end of recording`, `seconds`, `key q`, `SIGINT`, `SIGTERM`,
`SIGHUP`, `alarm`, `client exited`, `precheck`), `tvdump_rc` (exit code of the
network client; null when it had to be killed), `traffic`, `framer` (receive
path counters: `seq_lost`, `seq_choked`, `resend_skipped`, packet fates, ...),
`conn` (last connection state), `game_events` (counts per type, shown or not).
Live runs also have `slot_released`, `relay_before`, `relay_with_us` and
`relay_after`: the relay's spectator counts used to check that our slot was
freed.
