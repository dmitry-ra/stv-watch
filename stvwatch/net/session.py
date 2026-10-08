#!/usr/bin/env python3
"""One SourceTV session: drive the signon ladder to FULL, then hold it.

The relay pushes each signon state; we echo `net_SignonState(state, spawncount)`
back for every state >= CONNECTED, quoting the spawncount from the most recent
`svc_ServerInfo`. Miss an echo, or quote a stale spawncount, and the relay
forces a reconnect.

Design rules this file exists to enforce:
  * ONE receive path. Every datagram - plain or SNAP-inflated - goes through
    `feed()`, and the checksum is validated before any state is touched.
  * The reliable ack is decided from the latched subchannel index alone, so a
    malformed block can never cost us the ack.
  * `spawncount` is re-latched on EVERY svc_ServerInfo, not once per session.
  * Echo guards are keyed by (spawncount, state), so the relay may re-drive the
    ladder in place without a teardown.
  * `state` is the single source of truth for how far we got. There is no second
    flag that can disagree with it.
"""

import time

from . import chain, messages, netchan, receiver, wire

# Break causes, recorded verbatim into the dump.
BREAK_DISCONNECT = "disconnect"
BREAK_CHANGELEVEL = "changelevel"
BREAK_SILENCE = "silence"
BREAK_SIGNON_STALL = "signon_stall"
BREAK_SOCKET = "socket_error"
BREAK_RELIABLE_DEAD = "reliable_dead"

# Receiver fates -> the counter names this client has always reported.
_FATE_COUNTER = {"nested_split": "drop_nested_split", "oob": "drop_oob"}


class Session:
    def __init__(self, conn, opt, sink, crc=None):
        self.conn = conn
        self.opt = opt
        self.sink = sink  # callbacks into the supervisor/dump
        self.crc = opt.crc if crc is None else crc

        self.out_seq = 1
        self.in_reliable_state = 0  # what we echo back
        self.out_reliable_state = 0  # our model of our own sent blocks
        self.next_subchannel = 1  # 0 is spent on the CONNECTED reply

        self.state = wire.SIGNON_NONE
        self.spawncount = None
        self.map_name = None
        self.server_tick = 0

        # One connection, so no challenge inference; the replay bit (until
        # svc_ServerInfo discriminates) lives in the receiver with the walk.
        self.rx = receiver.Receiver()
        self.rx.challenge = conn.challenge
        self._replay_bit_seen = self.rx.replay_bit
        self.echoed = set()  # (spawncount, state) already echoed
        self.pending = None  # one reliable in flight
        self.pending_sent_at = 0.0
        self.pending_subchannel = None

        self.started = time.monotonic()
        self.last_inbound = self.started
        self._last_progress_state = self.state
        self._last_progress_at = self.started
        # Second progress axis, armed at EVERY state including FULL. The ladder
        # axis above stops moving once FULL is reached, which left one wedge
        # invisible: if reliable parsing fails on the same fragment every time we
        # never flip, the relay resends forever, and those resends keep the
        # silence breaker fed while the lifecycle channel is dead.
        self._reliable_seen_at = None
        self._reliable_done_at = None
        self.last_tick_ack = 0.0
        self.broke = None  # (cause, detail) once broken
        self.counters = {}
        self.rerated = False

    # -- counters instead of silent swallows --------------------------------
    def bump(self, name):
        self.counters[name] = self.counters.get(name, 0) + 1

    # -- outbound -----------------------------------------------------------
    def _send(self, packet):
        try:
            self.conn.sock.sendto(packet, self.conn.server)
        except OSError as e:
            self.fail(BREAK_SOCKET, str(e))
            return False
        self.out_seq += 1
        self.sink.on_outbound(packet)
        return True

    def send_ack(self):
        self._send(
            netchan.build_ack(
                self.out_seq, self.in_seq, self.conn.challenge, self.in_reliable_state
            )
        )

    def send_reliable(self, body, subchannel=None):
        """Queue one reliable message stream. Only one is in flight at a time:
        the relay acks by flipping its relstate bit for that subchannel, and
        until it does, re-sending on a fresh subchannel would desynchronize us."""
        if subchannel is None:
            subchannel = self.next_subchannel
            self.next_subchannel = (self.next_subchannel + 1) % wire.SUBCHANNEL_COUNT
        region = messages.reliable_region(body, subchannel)
        self.pending = region
        self.pending_subchannel = subchannel
        self.out_reliable_state = wire.flip_bit(self.out_reliable_state, subchannel)
        self._transmit_pending()

    def _transmit_pending(self):
        if self.pending is None:
            return
        self.pending_sent_at = time.monotonic()
        self._send(
            netchan.build_packet(
                self.out_seq,
                self.in_seq,
                self.conn.challenge,
                self.in_reliable_state,
                reliable_region=self.pending,
            )
        )

    def send_connected_reply(self):
        body = messages.connected_reply_body(self.opt.name, spawncount=-1)
        self.state = wire.SIGNON_CONNECTED
        self.send_reliable(body, subchannel=0)

    def send_disconnect(self, reason, copies=2):
        """Release our relay slot now. Without it the relay keeps the slot
        until its 300 s signon timeout (net.h SIGNON_TIME_OUT; HLTV clients
        never get the shorter sv_timeout). Unreliable and unacked, so sent
        twice: one lost datagram must not cost a 5-minute slot."""
        body = messages.disconnect_body(reason)
        sent = 0
        for _ in range(copies):
            if self._send(
                netchan.build_packet(
                    self.out_seq,
                    self.in_seq,
                    self.conn.challenge,
                    self.in_reliable_state,
                    unreliable=body,
                )
            ):
                sent += 1
        return sent

    def send_tick_ack(self):
        self._send(
            netchan.build_packet(
                self.out_seq,
                self.in_seq,
                self.conn.challenge,
                self.in_reliable_state,
                unreliable=messages.tick_body(self.server_tick),
            )
        )

    # -- break --------------------------------------------------------------
    def fail(self, cause, detail=""):
        if self.broke is None:
            self.broke = (cause, detail)
            self.sink.on_break(cause, detail)

    @property
    def alive(self):
        return self.broke is None

    # -- the single receive path --------------------------------------------
    @property
    def in_seq(self):
        return self.rx.in_seq

    @property
    def reasm(self):
        return self.rx.reasm

    @property
    def replay_bit(self):
        return self.rx.replay_bit

    def feed(self, datagram, t_ns=None):
        """Consume one inbound datagram. Never raises on malformed input.
        `t_ns` (monotonic) times out unfinished -2 groups; a replay of the dump
        uses its wall-clock stamps, which differ only across a clock step."""
        if t_ns is None:
            t_ns = time.monotonic_ns()
        self.last_inbound = time.monotonic()
        if wire.classify(datagram) == "split":
            # Parts are paced ~160 ms apart and carry whole packets: snapshots
            # with voice, and reliable fragments whose loss stalls the transfer.
            self.bump("split_seen")
            self.sink.on_split(datagram)
        pkt = self.rx.feed(datagram, t_ns)
        if pkt is None:
            return
        if pkt.via_split:
            self.bump("split_joined")
        if pkt.fate != receiver.OK:
            self.bump(_FATE_COUNTER.get(pkt.fate, pkt.fate))
            return
        if pkt.header.reliable:
            # FLIPBIT semantics, per CNetChan::ProcessPacket (engine
            # net_chan.cpp): toggle one bit of m_nInReliableState per RECEIVED
            # RELIABLE PACKET, for the subchannel index it carries - NOT once
            # per completed block (that deadlocks: the flip is what releases
            # the next batch). The engine does NOT flip on a failure inside
            # ReadSubChannelData, leaving the packet unacked so the relay
            # resends it; acking a block we could not parse would lose every
            # reliable message inside it silently and permanently. The index
            # is latched before the parse, so a malformed block cannot cost it.
            self._reliable_seen_at = time.monotonic()
            if pkt.subchannel is None:
                self.bump("no_subchannel_index")
            elif not pkt.reliable_ok:
                self.bump("reliable_parse_error")
            else:
                self.in_reliable_state = wire.flip_bit(self.in_reliable_state, pkt.subchannel)
                self._reliable_done_at = time.monotonic()
        if self.rx.replay_bit != self._replay_bit_seen:
            self._replay_bit_seen = self.rx.replay_bit
            self.sink.on_replay_bit(self.rx.replay_bit)
        for w in pkt.walks:
            if w.stream == "reliable":
                if not w.clean:
                    self.bump("chain_stop_%s" % w.stop["id"])
                for mid, fields in chain.events(w.msgs):
                    self._handle_message(mid, fields, w.clean)
            elif not w.clean:
                # Nothing from a dirty tail is applied. Measured over ~18k
                # packets on three servers, a dirty tail yielded net_Tick and
                # nothing else -- but server_tick is latched with max(), so ONE
                # garbage tick poisons it for the rest of the session. Clean
                # tails supply ticks an order of magnitude more often.
                self.bump("unreliable_chain_stop")
            else:
                for mid, fields in chain.events(w.msgs):
                    self._handle_message(mid, fields, True)
        self._check_pending_ack(pkt.header)
        self.send_ack()

    def _handle_message(self, mid, fields, clean=True):
        if mid == wire.NET_TICK:
            self.server_tick = max(self.server_tick, fields["tick"])
        elif mid == wire.NET_DISCONNECT:
            # Only honour a teardown from a chain that walked cleanly to its
            # end. A misaligned walk reads table bytes as a message id and
            # invents a disconnect, which would tear down a healthy session on
            # garbage. A genuine disconnect arrives in a small clean chain; if
            # one is ever missed here, the silence breaker still recovers us -
            # the cost is a less precise cause label, not a stuck client.
            if not clean:
                self.bump("disconnect_from_dirty_chain")
                return
            self.fail(BREAK_DISCONNECT, fields.get("reason", ""))
        elif mid == wire.SVC_SERVERINFO:
            # Re-latched every time: a new map on the same connection issues a
            # fresh spawncount, and every echo must quote the current one.
            new_count = fields["spawncount"]
            new_map = fields.get("map")
            first = self.spawncount is None
            spawn_changed = not first and new_count != self.spawncount
            map_changed = bool(new_map) and self.map_name not in (None, new_map)

            if spawn_changed:
                self.echoed.clear()  # echoes must quote the new count
            self.spawncount = new_count
            if new_map:
                self.map_name = new_map
            # One event per change. A real map change bumps the spawncount AND
            # the name; reporting each separately wrote two MAPCHANGE records
            # per change, and the dump is the product.
            if spawn_changed or map_changed:
                self.sink.on_mapchange(new_map)
        elif mid == wire.NET_SIGNONSTATE:
            self._advance(fields["state"])

    def _advance(self, state):
        """React to a server-pushed signon state."""
        if state == wire.SIGNON_CHANGELEVEL:
            self.fail(BREAK_CHANGELEVEL, "server signalled changelevel")
            return
        if self.state >= wire.SIGNON_FULL and state <= wire.SIGNON_CONNECTED:
            self.fail(BREAK_CHANGELEVEL, f"regressed to {state} after FULL")
            return
        if state > self.state:
            self.state = state
            self.sink.on_signon(state)
        self._echo(state)

    def _echo(self, state):
        """Echo a signon state once per (spawncount, state). Keyed rather than
        one-shot so a re-drive of the ladder in place re-arms naturally."""
        if self.spawncount is None or state < wire.SIGNON_CONNECTED:
            return
        key = (self.spawncount, state)
        if key in self.echoed or self.pending is not None:
            return
        self.echoed.add(key)
        if state == wire.SIGNON_NEW:
            # NEW is where we identify ourselves. The SendTable CRC is checked:
            # a wrong value (including 0) is rejected live with
            # net_Disconnect "Server uses different class tables". It is
            # build-specific and therefore a parameter, not a constant.
            body = messages.clientinfo_body(
                self.spawncount, sendtable_crc=self.crc, replay_bit=self.replay_bit
            )
        else:
            body = messages.signonstate_body(state, self.spawncount)
        self.send_reliable(body)

    def _check_pending_ack(self, header):
        """The relay acks a reliable block by flipping its relstate bit for that
        subchannel to match what we sent."""
        if self.pending is None or self.pending_subchannel is None:
            return
        want = (self.out_reliable_state >> self.pending_subchannel) & 1
        got = (header.reliable_state >> self.pending_subchannel) & 1
        if want == got:
            self.pending = None
            self.pending_subchannel = None
            # A pending echo may have been suppressed while this was in flight.
            self._echo(self.state)

    # -- time-driven work ---------------------------------------------------
    def poll(self, now=None):
        """Called every loop turn, datagram or not. Drives resends, tick acks
        and the breakers. Breakers are armed at EVERY signon level: a stall
        during signon is just as fatal as one after FULL."""
        now = now or time.monotonic()
        if not self.alive:
            return
        silence = now - self.last_inbound
        limit = (
            self.opt.silence_full_s if self.state >= wire.SIGNON_FULL else self.opt.silence_signon_s
        )
        if silence > limit:
            self.fail(BREAK_SILENCE, f"no inbound for {silence:.1f}s at " f"state {self.state}")
            return
        # A stall means NO PROGRESS, not "time elapsed". The relay ships the
        # signon buffer (hundreds of downloadable maps on some servers) as a long burst of
        # large reliable packets; a wall-clock deadline killed sessions that were
        # transferring perfectly normally. Progress = the ladder advancing.
        # A genuinely dead session is caught by the silence breaker above.
        if self.state != self._last_progress_state:
            self._last_progress_state = self.state
            self._last_progress_at = now
        # Reliable-channel breaker, armed at every state. If reliable packets
        # keep arriving but none has been accepted for this long, our ack is
        # never advancing and the relay is resending into a void.
        if (
            self._reliable_seen_at is not None
            and now - self._reliable_seen_at < self.opt.reliable_dead_s
            and now - (self._reliable_done_at or self.started) > self.opt.reliable_dead_s
        ):
            self.fail(
                BREAK_RELIABLE_DEAD,
                "reliable packets arriving but none accepted for "
                f"{now - (self._reliable_done_at or self.started):.1f}s",
            )
            return
        if (
            self.state < wire.SIGNON_FULL
            and now - self._last_progress_at > self.opt.signon_deadline_s
        ):
            self.fail(
                BREAK_SIGNON_STALL,
                f"no ladder progress past "
                f"{wire.SIGNON_NAMES.get(self.state)} for "
                f"{now - self._last_progress_at:.1f}s",
            )
            return
        if self.pending is not None and now - self.pending_sent_at > self.opt.reliable_resend_s:
            self.bump("reliable_resend")
            self._transmit_pending()
        # FULL is not pushed by the relay: the client promotes itself once it is
        # actually receiving world data, then echoes FULL to confirm. Gate on a
        # server tick so the promotion means "data is flowing", not merely "we
        # asked nicely".
        if self.state == wire.SIGNON_SPAWN and self.pending is None and self.server_tick > 0:
            self.state = wire.SIGNON_FULL
            self.sink.on_signon(wire.SIGNON_FULL)
            self._echo(wire.SIGNON_FULL)
        # The relay clamps `rate` when userinfo arrives (CONNECTED), before
        # ClientInfo declares us a proxy: spectators get tv_maxrate, proxies
        # MAX_RATE (hltvclient.cpp SetRate). Resending it re-runs the clamp.
        if (
            getattr(self.opt, "rerate", 0)
            and not self.rerated
            and self.state >= wire.SIGNON_FULL
            and self.pending is None
        ):
            self.rerated = True
            self.send_reliable(messages.setconvar([("rate", str(self.opt.rerate))]).get_bytes())
        if self.state >= wire.SIGNON_SPAWN and now - self.last_tick_ack > self.opt.tick_ack_s:
            self.last_tick_ack = now
            self.send_tick_ack()
