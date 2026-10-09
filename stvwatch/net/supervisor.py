#!/usr/bin/env python3
"""Supervisor: owns the dump, runs sessions, reconnects forever.

Every lifecycle transition becomes a typed dump record, so a session boundary is
IN the file. That is the whole point: a capture that splices two sessions with
no marker looks complete while silently merging unrelated state.

Backoff never blocks the receive loop with `sleep()` - a blind sleep drops
datagrams and leaves an unannotated hole in the dump. Between attempts the
socket is closed and the wait is deadline-driven, and the wait itself is
recorded as a RECONNECT record so the gap is explained rather than mysterious.
"""

import socket
import time

from . import a2s, builds, handshake, wire
from . import dump as dumpfmt
from . import session as sess


class Options:
    def __init__(self, **kw):
        self.ip = kw.get("ip", "127.0.0.1")
        self.port = kw.get("port", 27020)
        self.name = kw.get("name", "tvdump")
        # None = auto: build from the relay's A2S_INFO, CRC from builds.py.
        self.build = kw.get("build")
        self.password = kw.get("password", "")
        self.crc = kw.get("crc")  # build-specific; 0 is rejected
        self.seconds = kw.get("seconds", 0.0)  # 0 = run forever
        self.socket_timeout = kw.get("socket_timeout", 0.1)
        self.handshake_timeout = kw.get("handshake_timeout", 6.0)
        self.silence_signon_s = kw.get("silence_signon_s", 10.0)
        self.silence_full_s = kw.get("silence_full_s", 15.0)
        self.signon_deadline_s = kw.get("signon_deadline_s", 120.0)
        self.reliable_dead_s = kw.get("reliable_dead_s", 60.0)
        self.reliable_resend_s = kw.get("reliable_resend_s", 0.5)
        self.tick_ack_s = kw.get("tick_ack_s", 0.1)
        self.backoff_s = kw.get("backoff_s", 2.0)
        self.backoff_max_s = kw.get("backoff_max_s", 30.0)
        # A server that states a standing refusal (wrong password/build) will not
        # change its mind on the timescale of the ordinary cap, but it may change
        # it eventually, so this is a slower poll rather than giving up.
        self.reject_backoff_max_s = kw.get("reject_backoff_max_s", 300.0)
        self.fsync_ms = kw.get("fsync_ms", 1000)
        # >0: resend `rate` once after FULL (see Session.poll). Off by default.
        self.rerate = kw.get("rerate", 0)
        self.leave_reason = kw.get("leave_reason", handshake.LEAVE)


class Supervisor:
    def __init__(self, opt, dump_path, log=print):
        self.opt = opt
        self.log = log
        self.endpoint = f"{opt.ip}:{opt.port}"
        self.dump = dumpfmt.DumpWriter(dump_path, self.endpoint, fsync_ms=opt.fsync_ms)
        self.sessions = 0
        self.reconnects = 0
        self.reached_full = 0
        self.leaves = 0
        self.stop = False
        self.last_error = None  # text of the last failed attempt
        self.alarm = None  # set on a permanent failure
        self.build = None  # resolved for the current attempt
        self.crc = None

    # -- sink callbacks used by Session -------------------------------------
    def on_outbound(self, packet):
        self.dump.write(dumpfmt.DATAGRAM_OUT, packet)

    def on_split(self, datagram):
        # A marker, not a copy - the bytes are already in the DATAGRAM_IN record
        # written before parsing. Reassembly happens in Session.
        self.dump.event(dumpfmt.SPLIT_SEEN, length=len(datagram))
        self.log("[split] -2 part (%dB)" % len(datagram))

    def on_signon(self, state):
        self.dump.event(dumpfmt.SIGNON, state=state, name=wire.SIGNON_NAMES.get(state, "?"))
        self.log("[signon] %d:%s" % (state, wire.SIGNON_NAMES.get(state, "?")))
        if state == wire.SIGNON_FULL:
            self.reached_full += 1

    def on_break(self, cause, detail):
        self.dump.event(dumpfmt.BROKEN, cause=cause, detail=detail)
        self.log("[break] %s: %s" % (cause, detail))

    def on_mapchange(self, map_name):
        self.dump.event(dumpfmt.MAPCHANGE, map=map_name or "")
        self.log("[map] %s" % map_name)

    def on_replay_bit(self, flag):
        self.log("[detect] m_bIsReplay = %s" % flag)

    # -- run ----------------------------------------------------------------
    def run(self):
        deadline = (time.monotonic() + self.opt.seconds) if self.opt.seconds else None
        backoff = self.opt.backoff_s
        attempt = 0
        try:
            while not self.stop and (deadline is None or time.monotonic() < deadline):
                attempt += 1
                cap = self.opt.backoff_max_s
                self.last_error = None
                conn = self._handshake(attempt) if self._resolve(attempt) else None
                if conn is None:
                    productive = False
                    if builds.classify(self.last_error) == builds.PERMANENT:
                        cap = self._permanent(self.last_error)
                else:
                    self.sessions += 1
                    self.dump.event(
                        dumpfmt.SESSION_START,
                        session=self.sessions,
                        attempt=attempt,
                        endpoint=self.endpoint,
                    )
                    self.log(
                        "[session] #%d open (challenge=0x%08x)" % (self.sessions, conn.challenge)
                    )
                    try:
                        s = self._run_session(conn, deadline)
                    finally:
                        # an exception escaping here must not leak the socket
                        # to a caller that retries around this call
                        conn.close()
                    if self.sessions > 1:
                        self.reconnects += 1
                    # Productive == the ladder was climbed. A session that reached
                    # FULL and then hit a changelevel is the tool working, so it
                    # must not accumulate delay; one that died below FULL is an
                    # unproductive attempt however the handshake went.
                    productive = s.state >= wire.SIGNON_FULL
                    if self._is_standing_refusal(s.broke):
                        cap = self._permanent(s.broke[1])
                # Applied on EVERY path: a relay that accepts the connect and
                # drops us at once would otherwise be reconnected to with no
                # delay at all, a connect flood at one attempt per RTT.
                if productive:
                    self.alarm = None
                backoff = self.opt.backoff_s if productive else min(backoff * 2, cap)
                if not self._wait(backoff, deadline):
                    break
        except KeyboardInterrupt:
            self.log("[net] interrupted")
        finally:
            self.dump.close()

    def _is_standing_refusal(self, broke):
        if not broke or broke[0] != sess.BREAK_DISCONNECT:
            return False
        return builds.classify(broke[1]) == builds.PERMANENT

    def _permanent(self, reason):
        """A refusal that only a human can lift: alarm once, then poll slowly."""
        if self.alarm != reason:
            self.alarm = reason
            self.log("[alarm] permanent failure: %s" % reason)
        return self.opt.reject_backoff_max_s

    def _resolve(self, attempt):
        """Fill build/CRC for this attempt. Auto mode asks the relay every
        attempt: the server updates silently, and a stale build is a refusal."""
        build, crc = self.opt.build, self.opt.crc
        if build is None:
            try:
                build = a2s.info(self.opt.ip, self.opt.port)["version"]
            except (a2s.A2SError, OSError) as e:
                return self._resolve_failed(attempt, "a2s: %s" % e)
        if crc is None:
            try:
                crc = builds.crc_for(build)
            except builds.UnknownBuild as e:
                return self._resolve_failed(attempt, str(e))
        if (build, crc) != (self.build, self.crc):
            self.log("[build] %s crc=0x%08X" % (build, crc))
        self.build, self.crc = build, crc
        return True

    def _resolve_failed(self, attempt, error):
        self.last_error = error
        self.dump.event(dumpfmt.RECONNECT, attempt=attempt, ok=False, error=error)
        self.log("[build] attempt %d: %s" % (attempt, error))
        return False

    def _handshake(self, attempt):
        def tee(direction, data):
            self.dump.write(
                dumpfmt.DATAGRAM_OUT if direction == "out" else dumpfmt.DATAGRAM_IN, data
            )

        try:
            conn = handshake.connect(
                self.opt.ip,
                self.opt.port,
                self.opt.name,
                self.build,
                self.opt.password,
                timeout=self.opt.handshake_timeout,
                on_packet=tee,
            )
            self.dump.event(dumpfmt.RECONNECT, attempt=attempt, ok=True)
            return conn
        except (handshake.HandshakeError, OSError) as e:
            self.last_error = str(e)
            self.dump.event(dumpfmt.RECONNECT, attempt=attempt, ok=False, error=str(e))
            self.log("[handshake] attempt %d failed: %s" % (attempt, e))
            return None

    def _run_session(self, conn, deadline):
        s = sess.Session(conn, self.opt, self, crc=self.crc)
        conn.sock.settimeout(self.opt.socket_timeout)
        try:
            s.send_connected_reply()
            if conn.pending:
                # NOT written to the dump here: handshake.connect already
                # emitted it through the tee that records every handshake
                # datagram; writing it again duplicated a DATAGRAM_IN.
                s.feed(conn.pending, time.monotonic_ns())
            while s.alive and not self.stop:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                try:
                    data, _ = conn.sock.recvfrom(65535)
                except socket.timeout:
                    data = None
                except OSError as e:
                    s.fail(sess.BREAK_SOCKET, str(e))
                    break
                if data:
                    self.dump.write(dumpfmt.DATAGRAM_IN, data)
                    s.feed(data, time.monotonic_ns())
                s.poll()
        finally:
            self._leave(s)
        if s.counters:
            self.log("[counters] %s" % s.counters)
        return s

    def _leave(self, s):
        """Every way out of a session -- deadline, signal, break, exception --
        passes here. Skipped only when the relay itself dropped us: then the
        channel is already gone on its side."""
        if s.broke is not None and s.broke[0] == sess.BREAK_DISCONNECT:
            return
        why = "stop" if self.stop else (s.broke[0] if s.broke else "deadline")
        sent = s.send_disconnect(self.opt.leave_reason)
        self.leaves += 1
        self.dump.event(dumpfmt.LEAVE, why=why, sent=sent)
        self.log("[leave] net_Disconnect x%d (%s)" % (sent, why))

    def _wait(self, seconds, deadline):
        """Bounded wait between reconnect attempts. Returns False if the run's
        deadline arrives first."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stop or (deadline is not None and time.monotonic() >= deadline):
                return False
            time.sleep(0.1)
        return True
