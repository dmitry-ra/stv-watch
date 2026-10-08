# How stv-watch talks to a relay

A short map of the protocol as stv-watch uses it, for whoever changes the client.
It was worked out from the public Source SDK 2013 headers and from observing
relays; no SDK code is included here. Module names are under `stvwatch/`.

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

## Server builds

The SendTable CRC depends on the server build and a wrong one is refused by the
relay ("different class tables"). `net/builds.py` maps the build number from
A2S_INFO `version` to its CRC. When Valve ships a new build, stv-watch refuses
relays on it until its CRC is added. To get it:

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
