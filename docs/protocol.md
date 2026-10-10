# How stv-watch talks to a relay

A short map of the protocol as stv-watch uses it, for whoever changes the client.
It was worked out from the public Source SDK 2013 headers and from observing
relays; no SDK code is included here. Module names are under `stvwatch/`.
Earlier public work it builds on: [prior-art.md](prior-art.md).

## Processes

The viewer (`app.py`) never touches the network. It starts the network client
(`net/main.py`, called tvdump in logs and file names) as a child process with
the same interpreter (`python -m stvwatch.net.main run ...`). The child holds
the session, writes every datagram and lifecycle event to `capture.tvd`, and
the viewer reads that file as it grows, with the same reader a replay uses. So
a live run and the replay of its recording go through the same code after the
socket.

The child asks the kernel for SIGTERM when its parent dies
(`PR_SET_PDEATHSIG`). SIGTERM, from the viewer or the kernel, makes it send
`net_Disconnect` and exit: the relay frees the slot at once instead of after
its 300 s timeout.

## Before connecting

`net/client.py` `precheck`: A2S_INFO of the relay port (spectator count, slots,
password flag, build number in `version`), then a connectionless getchallenge
to read the auth protocol. Nothing in it takes a slot. Refused: a silent relay,
a password, an unknown build, a relay that wants Steam login (auth protocol 3),
no slot left for others after us.

## Handshake

`net/handshake.py`: `q` getchallenge -> `A` challenge (with the auth protocol)
-> `k` connect (protocol 24, anonymous hashed-cdkey auth, name, password,
build) -> `B` accepted. A relay may send in-band packets before or instead of
`B`; one is taken as proof that the channel is up. If nothing answers the
connect, a `net_Disconnect` is sent anyway, in case the relay opened a channel
whose answer was lost.

The anonymous login works on the relay port only. The game server's own port
wants a Steam session; a relay that offers authentication protocol 2 checks
nothing but `tv_password`, so with no password it takes the hashed-cdkey login
(a dummy MD5 of the name) and no Steam ticket.

## Netchannel

`net/netchan.py`: every in-band packet starts with sequence, acked sequence,
flags, a checksum (CRC32 folded to 16 bits, over everything after it), our
reliable state byte, and optional choked count and challenge. The checksum is
checked before anything else, so a corrupt packet cannot move the sequence.

Datagrams that start with `-3` and `SNAP` carry one packet compressed with raw
Snappy (`net/codec.py`). Datagrams that start with `-2` are parts of a larger
packet (`net/split.py`): a 12-byte header with group id, part count and part
number; parts may arrive in any order, interleaved with other packets.

## Reliable stream

`net/reliable.py`, `net/receiver.py`: a reliable packet carries a 3-bit
subchannel index and up to two streams (messages, files), sent whole or in
256-byte fragments of a larger transfer. The receiver acknowledges a reliable
packet by flipping the subchannel's bit in the state byte it sends back; the
sender repeats an unacknowledged packet on the same subchannel. The client
flips the bit only for a packet whose region it could parse, so a broken one is
sent again rather than lost.

Reading a recording later, our own sent packets (recorded too) tell which
reliable packets the live client acknowledged; a repeat of one it did not is
recognized bit for bit and not read twice.

## Messages

`net/chain.py`: a stream is a chain of messages, each a 6-bit id and a body with
no length prefix. Every message type the relay sends has a sizer that consumes
exactly its bits, so the walk stays aligned; the walk stops at an unknown id.
`svc_VoiceData` is sized by its own 16-bit length and skipped. Decoded: signon
state, server info (map, spawn count), disconnect, tick; and, for the feed,
user messages (chat, console, notices) and game events
(`stream/streamevents.py`), and the `userinfo` string table for nicks and
SteamIDs (`stream/userinfo.py`).

## Signon

`net/session.py`: the relay pushes signon states; the client echoes each one,
quoting the spawn count of the latest server info. At NEW it sends its client
info with the SendTable CRC of the build. It promotes itself to FULL once world
data flows. A map change starts the ladder again; the supervisor
(`net/supervisor.py`) reconnects with a growing delay after a break, and polls
slowly after a refusal that retrying will not change.

The first packet is the one a real game client sends after `B`, recorded from
one: `tests/data/connected_reply.bin`, 480 bytes, sequence 1, flags
RELIABLE|CHALLENGE, holding `net_SetConVar` with the client's 27 userinfo
variables and `net_SignonState(CONNECTED, -1)`. `messages.connected_reply_body`
generates the same message stream and a test compares the two bit for bit.
After it the game client sends only 16-byte acks (flags CHALLENGE, no messages)
while the relay transfers the signon data, which takes tens of seconds on a
server with hundreds of maps among its downloadables. A relay that feeds other
relays also sends `tv_relay 1` in that userinfo; a spectator, stv-watch too,
does not need it. A client that answers `B` with empty acks alone, without the
CONNECTED packet, is never moved up: the relay keeps sending it empty
keepalives and does not count it as a spectator.

## Where voice comes from

Voice reaches a viewer only in the per-frame broadcast that the relay sends to
the clients it holds active, those at FULL. A client still on the ladder gets
the reliable signon data, resent until it is acknowledged, and no voice even
while players talk: a session stuck below FULL looks like a quiet server. The
relay passes voice on only with the server variable `tv_relayvoice` at 1 (the
default; `tools/local_srcds.py` sets it). Newer engines (CS2) add per-viewer
voice filters such as `tv_listen_voice_indices`; Source 2013 has none.

`svc_VoiceInit` names the codec. stv-watch decodes only `steam`, Opus frames in
the Steam voice format (`voice/steamvoice.py`), the codec of the relays it was
developed on. A server on `vaudio_celt` or `vaudio_speex` sends the same
`svc_VoiceData` messages with another payload; stv-watch does not decode it,
and those messages fail the Steam voice CRC check (`voice_crc_bad` in
`meta.json`).

## Server builds

The SendTable CRC depends on the server build and a wrong one is refused by the
relay ("different class tables"). `net/builds.py` maps the build number from
A2S_INFO `version` to its CRC. The server computes it over all its send tables
when the game library loads; other client implementations found no practical
way to compute it from what a client receives (see
[prior-art.md](prior-art.md)), and 0 is refused like any other wrong value. It
changes with game updates that touch the class tables, as the 2023 anniversary
update of the game did. When Valve ships a new build, stv-watch refuses relays
on it until its CRC is added. To get it:

1. Install a dedicated server of that build with SteamCMD (app 232370):
   `steamcmd +force_install_dir /path/to/srcds +login anonymous +app_update 232370 +quit`
2. Run `python3 tools/read_crc.py /path/to/srcds`. It starts the server on
   loopback (LAN only, no SourceTV, `-nobreakpad`), waits for the map to load
   and reads `g_SendTableCRC` from the memory of `engine_srv.so` (the symbol's
   offset is taken with `readelf`). It needs `readelf` (binutils) and
   permission to read its own child's memory (the default `ptrace_scope` 1 is
   enough).
3. Add `"BUILD": 0xCRC,` to `CRC_BY_BUILD` in `stvwatch/net/builds.py`; the
   build number is what A2S_INFO reports as `version` for a server of that
   build (stv-watch's refusal names it).

## Receive buffer

The network client leaves the socket's receive buffer (`SO_RCVBUF`) at the
system default, so a stall of its receive loop longer than that buffer holds
drops datagrams; this is why the dump is fsynced at most once a second rather
than per record (`net/dump.py`). If the buffer is ever enlarged: Linux doubles
the requested size for its own bookkeeping and silently caps the request at
`net.core.rmem_max` (212992 by default, so at most 425984 bytes). Read the size
back with `getsockopt` instead of trusting the request.

## Endurance

In July 2026 the network client, then a separate program, ran 72 hours against
19 public relays, one process each. No process crashed or was restarted by
hand; resident memory stayed flat at 17 to 21 MB per process after the first
hour and open file descriptors at 4 to 5. Together they held 10,295 sessions
through nightly server restarts, hundreds of map changes and relay outages of
several hours, and 9,414 of the sessions reached FULL. The run found four bugs,
fixed since: the unreliable part of packets was not read, one reconnect path
skipped the backoff, a duplicate fragment could complete a reliable transfer
(a count of fragments instead of a bitmask), and reading bit by bit was too
slow (shifts on whole integers, which replaced it, were 450 and 626 times
faster where measured). One
relay reached FULL in only 690 of its 1,538 sessions, while every other relay
reached it in nearly all of them; the cause was not found.
