"""One RS-BA1 UDP stream (asyncio).

Responsibilities shared by the control, serial and audio streams:
  * session handshake: are-you-there(3) -> I-am-here(4), are-you-ready(6) -> (6)
  * tracked sends: every data packet gets the next sequence number and is kept
    for a few seconds so the radio can ask for it again
  * serving the radio's retransmit requests
  * idle keepalives (control and serial streams): every 100 ms while there is
    traffic, every second when quiet
  * pings (type 7): answer the radio's, send our own every 3 s, measure RTT
  * disconnect(5) on close
"""

import asyncio
import collections
import logging
import os
import socket
import time

from ..netcompat import udp_socket
from . import packets as P

log = logging.getLogger(__name__)
pktlog = logging.getLogger("icomremote.packets")

EXPECT_TIMEOUT = 1.0
HANDSHAKE_ATTEMPTS = 3
IDLE_FAST = 0.1
IDLE_SLOW = 1.0
IDLE_AFTER = 1.0
PING_INTERVAL = 3.0
TX_HISTORY_SECONDS = 3.0


class StreamError(Exception):
    """Fatal error on a stream; the session reconnects."""


class StreamStats:
    def __init__(self):
        self.tx_packets = self.rx_packets = 0
        self.tx_bytes = self.rx_bytes = 0
        self.retransmits_served = 0
        self.retransmits_missing = 0


class UdpStream(asyncio.DatagramProtocol):
    def __init__(self, name, host, port, local_port=0, on_error=None, log_packets=True):
        self.name = name
        self.host = host
        self.port = port
        self.local_port = local_port
        self.on_error = on_error or (lambda exc: None)
        self.log_packets = log_packets
        self.transport = None
        self.local_sid = 0
        self.remote_sid = 0
        self.got_remote_sid = False
        self.stats = StreamStats()
        self.rtt = 0.0
        self.last_rx = 0.0
        self._loop = None
        self._send_seq = 1
        self._history = collections.OrderedDict()   # seq -> (time, packet)
        self._waiters = []
        self._idle_handle = None
        self._idle_enabled = False
        self._last_tracked = 0.0
        self._ping_task = None
        self._ping_seq = 0
        self._ping_inner = 0x8304
        self._ping_sent = {}         # ping id -> send time
        self._pinging = False
        self.closed = False

    # ------------------------------------------------------------------ socket
    async def open(self):
        self._loop = asyncio.get_running_loop()
        # Resolve in this thread rather than via loop.getaddrinfo(): that goes
        # through asyncio's thread pool, which fails on Python 3.15.0 when the
        # loop runs in a worker thread ('lazy_import' object is not callable).
        # An IP address resolves instantly; a host name blocks only this
        # session thread, only for the lookup.
        try:
            infos = socket.getaddrinfo(self.host, self.port, family=socket.AF_INET,
                                       type=socket.SOCK_DGRAM)
        except socket.gaierror as e:
            raise StreamError("cannot resolve %s: %s" % (self.host, e))
        if not infos:
            raise StreamError("cannot resolve %s" % self.host)
        addr = infos[0][4]
        try:
            sock = udp_socket(self.local_port, addr)
            await self._loop.create_datagram_endpoint(lambda: self, sock=sock)
        except OSError as e:
            if self.local_port and e.errno in (48, 98, 10048):     # EADDRINUSE mac/linux/windows
                raise StreamError("%s: local UDP port %d is in use (another RS-BA1 client running? "
                                  "try without --bind-same-ports)" % (self.name, self.local_port))
            raise
        ip, port = self.transport.get_extra_info("sockname")[:2]
        # Session id = last two octets of our IP and our port, as kappanhang/the
        # Icom software do. Only needs to be unique per client.
        octets = socket.inet_aton(ip if ip != "0.0.0.0" else "127.0.0.1")
        self.local_sid = (int.from_bytes(octets, "big") << 16 | (port & 0xFFFF)) & 0xFFFFFFFF
        log.info("%s: %s:%d -> %s:%d", self.name, ip, port, addr[0], addr[1])

    def connection_made(self, transport):
        self.transport = transport

    def connection_lost(self, exc):
        if not self.closed:
            self._fail(StreamError("%s: socket closed (%s)" % (self.name, exc)))

    def error_received(self, exc):
        # Windows/Linux report ICMP port-unreachable here (radio not listening).
        log.warning("%s: %s", self.name, exc)

    def _fail(self, exc):
        if not self.closed:
            self.on_error(exc)

    # ------------------------------------------------------------------ sending
    def send_raw(self, d):
        if self.transport is None or self.closed:
            return
        self.transport.sendto(d)
        self.stats.tx_packets += 1
        self.stats.tx_bytes += len(d)
        if self.log_packets and pktlog.isEnabledFor(logging.DEBUG):
            pktlog.debug("%s TX %3d  %s", self.name, len(d), P.hexdump(d))

    def send_twice(self, d):
        self.send_raw(d)
        self.send_raw(d)

    def send_tracked(self, d):
        """Send with the next sequence number; keep it for retransmission."""
        seq = self._send_seq
        self._send_seq = (self._send_seq + 1) & 0xFFFF
        d = P.set_seq(d, seq)
        now = time.monotonic()
        self._history[seq] = (now, d)
        while self._history:
            first = next(iter(self._history.values()))
            if now - first[0] <= TX_HISTORY_SECONDS:
                break
            self._history.popitem(last=False)
        self.send_raw(d)
        if not P.is_idle(d):
            self._last_tracked = now
            if self._idle_enabled:
                self._schedule_idle(IDLE_FAST)
        return seq

    def _idle(self, tracked=True, seq=0):
        p = P.control(P.T_DATA, seq, self.local_sid, self.remote_sid)
        if tracked:
            self.send_tracked(p)
        else:
            self.send_raw(p)

    # ------------------------------------------------------------------ handshake
    async def handshake(self):
        for attempt in range(HANDSHAKE_ATTEMPTS):
            self.send_twice(P.control(P.T_ARE_YOU_THERE, 0, self.local_sid, 0))
            r = await self.expect(lambda d: len(d) == 16 and d[4] == P.T_I_AM_HERE,
                                  EXPECT_TIMEOUT, raise_on_timeout=False)
            if r is not None:
                break
            log.info("%s: no answer to are-you-there (attempt %d)", self.name, attempt + 1)
        else:
            raise StreamError("%s: the radio did not answer on %s:%d - check the address, "
                              "that Network Control is on, and the UDP ports"
                              % (self.name, self.host, self.port))
        self.remote_sid = int.from_bytes(r[8:12], "big")
        self.got_remote_sid = True
        self.send_twice(P.control(P.T_ARE_YOU_READY, 1, self.local_sid, self.remote_sid))
        await self.expect(lambda d: len(d) == 16 and d[4] == P.T_ARE_YOU_READY, EXPECT_TIMEOUT)
        log.debug("%s: handshake done, sids local=%08x remote=%08x",
                  self.name, self.local_sid, self.remote_sid)

    async def expect(self, pred, timeout, raise_on_timeout=True):
        fut = self._loop.create_future()
        entry = (pred, fut)
        self._waiters.append(entry)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            if raise_on_timeout:
                raise StreamError("%s: timeout waiting for the radio's answer" % self.name)
            return None
        finally:
            if entry in self._waiters:
                self._waiters.remove(entry)

    # ------------------------------------------------------------------ periodic
    def start_idle(self):
        self._idle_enabled = True
        self._schedule_idle(IDLE_SLOW)

    def _schedule_idle(self, delay):
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        if not self.closed:
            self._idle_handle = self._loop.call_later(delay, self._idle_tick)

    def _idle_tick(self):
        self._idle_handle = None
        if self.closed:
            return
        self._idle(tracked=True)
        quiet = time.monotonic() - self._last_tracked >= IDLE_AFTER
        self._schedule_idle(IDLE_SLOW if quiet else IDLE_FAST)

    def start_pings(self, first_seq=1):
        self._ping_seq = first_seq
        self._pinging = True
        self._ping_task = self._loop.create_task(self._ping_loop())

    async def _ping_loop(self):
        try:
            while not self.closed:
                await asyncio.sleep(PING_INTERVAL)
                self.send_ping()
        except asyncio.CancelledError:
            pass

    def send_ping(self):
        pid = bytes([os.urandom(1)[0], self._ping_inner & 0xFF, (self._ping_inner >> 8) & 0xFF, 0x06])
        self._ping_inner = (self._ping_inner + 1) & 0xFFFF
        self._ping_sent[pid] = time.perf_counter()       # monotonic() is 15.6 ms coarse on Windows
        if len(self._ping_sent) > 20:
            self._ping_sent.pop(next(iter(self._ping_sent)))
        self.send_raw(P.ping(self._ping_seq, self.local_sid, self.remote_sid, False, pid))
        self._ping_seq = (self._ping_seq + 1) & 0xFFFF

    # ------------------------------------------------------------------ receiving
    def datagram_received(self, d, addr):
        self.stats.rx_packets += 1
        self.stats.rx_bytes += len(d)
        self.last_rx = time.monotonic()
        if self.log_packets and pktlog.isEnabledFor(logging.DEBUG):
            pktlog.debug("%s RX %3d  %s", self.name, len(d), P.hexdump(d))
        try:
            if P.is_ping(d):
                self._on_ping(d)
                return
            if P.is_retransmit_request(d):
                self._serve_retransmit(P.parse_retransmit_request(d))
                return
            if len(d) == 16 and d[4] == P.T_DISCONNECT:
                self._fail(StreamError("%s: the radio closed the session" % self.name))
                return
            for pred, fut in list(self._waiters):
                if not fut.done() and pred(d):
                    fut.set_result(d)
                    return
            self.handle(d)
        except Exception as e:  # never let a bad packet kill the loop
            log.exception("%s: error handling packet %s", self.name, P.hexdump(d))
            self._fail(StreamError("%s: %s" % (self.name, e)))

    def handle(self, d):
        """Stream-specific packets. Overridden."""

    def _on_ping(self, d):
        seq = int.from_bytes(d[6:8], "little")
        if d[16] == 0x00:
            if self._pinging:                 # radio's ping: answer only once we're up
                self.send_raw(P.ping(seq, self.local_sid, self.remote_sid, True, d[17:21]))
        else:
            sent = self._ping_sent.pop(bytes(d[17:21]), None)
            if sent is not None:
                sample = time.perf_counter() - sent
                self.rtt = sample if self.rtt == 0 else (self.rtt + sample) / 2

    def _serve_retransmit(self, ranges):
        for first, last in ranges:
            n = ((last - first) & 0xFFFF) + 1
            if n > 64:                         # nonsense range, ignore
                continue
            for i in range(n):
                seq = (first + i) & 0xFFFF
                item = self._history.get(seq)
                if item is not None:
                    self.stats.retransmits_served += 1
                    log.debug("%s: retransmitting #%d", self.name, seq)
                    self.send_twice(item[1])
                else:
                    # Not kept any more: an untracked idle with that number
                    # tells the radio to stop waiting for it.
                    self.stats.retransmits_missing += 1
                    self._idle(tracked=False, seq=seq)
                    self._idle(tracked=False, seq=seq)

    def request_retransmit(self, first, last):
        if first == last:
            self.send_twice(P.retransmit_request(first, self.local_sid, self.remote_sid))
        else:
            self.send_twice(P.retransmit_range_request([(first, last)], self.local_sid, self.remote_sid))

    # ------------------------------------------------------------------ shutdown
    def close(self):
        if self.closed:
            return
        if self.got_remote_sid and self.transport is not None:
            self.send_twice(P.control(P.T_DISCONNECT, 0, self.local_sid, self.remote_sid))
        self.closed = True
        self._idle_enabled = False
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        if self._ping_task is not None:
            self._ping_task.cancel()
        for _, fut in self._waiters:
            if not fut.done():
                fut.cancel()
        if self.transport is not None:
            self.transport.close()
