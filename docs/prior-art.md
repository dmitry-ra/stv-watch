# Prior art

Public work on the Source 1 network protocol, SourceTV and Steam voice that
stv-watch was built against. Collected in July 2026. None of it joins a live
Source 1 relay over UDP and extracts voice end to end; each covers one part:
a protocol client, a message layout, a demo parser or a voice decoder.

## Clients of the Source network protocol

- [Leystryku/leysourceengineclient](https://github.com/Leystryku/leysourceengineclient)
  (C++, Windows, MIT). The closest reference: a client written from scratch for
  the same protocol generation as Half-Life 2:
  Deathmatch, with the connect handshake, the
  reliable subchannel and sending and receiving `svc_VoiceData`. It hardcodes the
  SendTable CRC of one game build in `clc_ClientInfo` (`src/leysourceengineclient.cpp`),
  the same mechanism as `stvwatch/net/builds.py`; its issues #7 and #19 discuss
  how to obtain the value.
- [Gbps/se-rust-client](https://github.com/Gbps/se-rust-client) (Rust,
  archived). The clearest written account of the netchannel: challenge and
  connect, the reliable subchannel and its state bits, compression, message
  demultiplexing, and the remark that a client has no good way to compute the
  SendTable CRC. It targets CS:GO, whose messages are protobuf, so the messages
  differ while the transport matches. Write-up:
  [Source engine, part 2](https://ctf.re/source-engine/exploitation/2021/05/01/source-engine-2/).
- [Galaco/sourcenet](https://github.com/Galaco/sourcenet) (Go, Unlicense).
  CS:S generation, handshake only, no reliable subchannel, no voice.

## Game code and protocol references

- [ValveSoftware/source-sdk-2013](https://github.com/ValveSoftware/source-sdk-2013):
  the public game code; the user message ids and bodies and the
  `player_info_t` layout read by `stvwatch/stream/` come from here.
- [SourceTV on the Valve Developer Community](https://developer.valvesoftware.com/wiki/SourceTV):
  relay setup and its console variables.
- [xPaw/PHP-Source-Query](https://github.com/xPaw/PHP-Source-Query): the order of
  the fields in a `-2` split header (see `stvwatch/net/split.py`).

## svc_VoiceData and the Steam voice codec

- [SizzlingStats/demboyz](https://github.com/SizzlingStats/demboyz):
  `demboyz/netmessages/svc_voicedata.cpp`, the layout of the message (sender,
  proximity, length in bits, payload).
- [demostf/steam-audio-codec](https://codeberg.org/demostf/steam-audio-codec)
  (Rust, a thin libopus wrapper): the Steam voice framing that
  `stvwatch/voice/steamvoice.py` reads (SteamID64 header, sample rate, Opus and
  silence chunks, trailing CRC32).
- Zhenyang Li, [Reversing Steam Voice Codec](https://zhenyangli.me/posts/reversing-steam-voice-codec/):
  the write-up the framing comes from, including why an Opus chunk holds
  frames of `u16 length | u16 sequence | opus`.
- [rumblefrog/source-chat-relay](https://github.com/rumblefrog/source-chat-relay):
  Steam voice research the CS2 decoders refer to.
- [DandrewsDev/CS2VoiceData](https://github.com/DandrewsDev/CS2VoiceData) (Go,
  libopus): the same decoding, from CS2 demos only.
- [akiver/csgo-voice-extractor](https://github.com/akiver/csgo-voice-extractor)
  (Go and C, MIT): separate paths for the older Speex and CELT codecs and for
  Steam Opus; the reference for a server that does not use the `steam` codec.
  Demos only.
- [ericek111's CS:GO voice gist](https://gist.github.com/ericek111/abe5829f6e52e4b25b3b97a0efd0b22b)
  (C): CELT at 22050 Hz, the old `vaudio_celt` codec.

## Demo parsers

The same message stream, read from `.dem` files rather than from a relay.

- [demostf/parser](https://github.com/demostf/parser) (Rust): TF2, the same
  protocol family; messages and bit buffer.
- [demostf/demo.js](https://github.com/demostf/demo.js) (JavaScript,
  deprecated, readable).

## Related, but a different transport or no game stream

- [FlowingSPDG/gotv-plus-go](https://github.com/FlowingSPDG/gotv-plus-go) and
  other CS:GO broadcast servers: the HTTP fragment broadcast of CS:GO and CS2
  (`tv_broadcast`), not the UDP relay.
- [ValvePython/steam](https://github.com/ValvePython/steam): the Steam client
  network (login, web API), no game netchannel.
- python-a2s and python-valve: server queries and RCON only.

## The SendTable CRC

CRCs of particular builds are practically never published: they change with
game updates that touch the class tables. Searching for the value stv-watch
uses for Half-Life 2: Deathmatch found nothing. The game's 20th anniversary
update (2023) did change the class tables:

- AlliedModders forum, thread 349608 on the incorrect class table after the
  anniversary update
  ([archived copy](http://web.archive.org/web/20250226172055/https://forums.alliedmods.net/showthread.php?t=349608));
  it names the older builds 6630498 and 9333546 but no CRC.
- [ValveSoftware/Source-1-Games #6773](https://github.com/ValveSoftware/Source-1-Games/issues/6773):
  class table fixes of the anniversary update.
