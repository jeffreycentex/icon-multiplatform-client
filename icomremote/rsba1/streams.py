"""Control, serial (CI-V) and audio streams."""

import asyncio
import logging
import os
import time

from . import packets as P
from .passcode import encode as encode_credential
from .seqbuf import ReorderBuffer
from .stream import StreamError, UdpStream

log = logging.getLogger(__name__)

REAUTH_INTERVAL = 60.0
REAUTH_TIMEOUT = 3.0
STREAM_REQUEST_TIMEOUT = 5.0
AUDIO_TIMEOUT = 5.0
RX_HOLD = 0.1


class AuthError(StreamError):
    """Bad username/password: don't retry automatically."""


class RadioBusyError(StreamError):
    """The radio refused the streams (another client connected?)."""


class SessionInfo:
    def __init__(self, radio_name, capabilities):
        self.radio_name = radio_name
        self.capabilities = capabilities       # RadioCapabilities or None

    @property
    def civ_address(self):
        return self.capabilities.civ_address if self.capabilities else None


class ControlStream(UdpStream):
    def __init__(self, host, port, user, password, serial_port, audio_port,
                 sample_rate=48000, tx_buffer_ms=300, **kw):
        super().__init__("control", host, port, **kw)
        self.user = user
        self.password = password
        self.serial_port = serial_port
        self.audio_port = audio_port
        self.sample_rate = sample_rate
        self.tx_buffer_ms = tx_buffer_ms
        self._inner_seq = 0
        self.auth_id = bytes(6)
        self.got_auth_id = False
        self._auth_ok = False
        self.capabilities = None
        self._radio_guid = bytes(16)
        self._streams_requested = False
        self._ready = None
        self._reauth_task = None
        self._reauth_reply = None

    def _next_inner(self):
        s = self._inner_seq
        self._inner_seq = (self._inner_seq + 1) & 0xFFFF
        return s

    async def start(self):
        """Log in and request the serial/audio streams. Returns SessionInfo."""
        await self.open()
        await self.handshake()
        self._ready = self._loop.create_future()

        user_enc = encode_credential(self.user)
        pass_enc = encode_credential(self.password)
        self.send_tracked(P.login(self.local_sid, self.remote_sid, self._next_inner(),
                                  os.urandom(2), user_enc, pass_enc))
        r = await self.expect(lambda d: len(d) == P.LEN_LOGIN_REPLY and d[:6] == b"\x60\x00\x00\x00\x00\x00",
                              2.0)
        if r[48:52] == b"\xff\xff\xff\xfe":
            raise AuthError("invalid username/password")
        log.info("login accepted")

        self.start_pings(first_seq=2)
        self.auth_id = bytes(r[26:32])
        self.got_auth_id = True
        self.send_tracked(P.auth(self.local_sid, self.remote_sid, self._next_inner(), self.auth_id, 0x02))
        self.start_idle()
        self.send_tracked(P.auth(self.local_sid, self.remote_sid, self._next_inner(), self.auth_id, 0x05))

        try:
            info = await asyncio.wait_for(asyncio.shield(self._ready), STREAM_REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            what = ("no capabilities packet" if self.capabilities is None else
                    "auth not confirmed" if not self._auth_ok else "no answer to the stream request")
            raise StreamError("login/stream request timeout (%s); try rebooting the radio" % what)
        self._reauth_task = self._loop.create_task(self._reauth_loop())
        return info

    def _maybe_request_streams(self):
        if self._streams_requested or not self._auth_ok or self.capabilities is None:
            return
        if self.closed or self._ready is None or self._ready.done():
            return
        self._streams_requested = True
        name = self.capabilities.name or "IC-705"
        log.info("requesting serial and audio streams from %s", name)
        self.send_tracked(P.stream_request(
            self.local_sid, self.remote_sid, self._next_inner(), self.auth_id, self._radio_guid,
            name, encode_credential(self.user), self.sample_rate,
            self.serial_port, self.audio_port, self.tx_buffer_ms))

    def handle(self, d):
        n = len(d)
        if P.is_idle(d):
            return
        if n == P.LEN_AUTH and d[:6] == b"\x40\x00\x00\x00\x00\x00":
            if self._reauth_reply is not None and not self._reauth_reply.done():
                self._reauth_reply.set_result(True)
            if d[21] == 0x05 and not self._auth_ok:
                log.info("auth ok")
                self._auth_ok = True
                self._maybe_request_streams()
            return
        if P.is_capabilities(d):
            caps = P.parse_capabilities(d)
            if caps:
                self.capabilities = caps[0]
                self._radio_guid = caps[0].guid
                log.info("radio: %s, CI-V address 0x%02X, audio '%s'%s", caps[0].name,
                         caps[0].civ_address, caps[0].audio_name,
                         "" if len(caps) == 1 else " (+%d more, using the first)" % (len(caps) - 1))
                self._maybe_request_streams()
            return
        if n == P.LEN_STATUS and d[:6] == b"\x50\x00\x00\x00\x00\x00":
            if d[48:51] == b"\xff\xff\xff":
                self._fail(StreamError("auth failed" + ("" if self._ready and self._ready.done()
                                                        else ", try rebooting the radio")))
            elif d[48:51] == b"\x00\x00\x00" and d[64] == 0x01:
                self._fail(StreamError("the radio reports it was disconnected"))
            return
        if n == P.LEN_CONNINFO and d[:6] == b"\x90\x00\x00\x00\x00\x00":
            self._on_conninfo(d)
            return
        if n == 16 and d[4] in (P.T_I_AM_HERE, P.T_ARE_YOU_READY):
            return                        # duplicate handshake answers (the radio sends them twice)
        log.debug("control: unhandled %d-byte packet %s", n, P.hexdump(d))

    def _on_conninfo(self, d):
        """0x90 status: byte 96 = in use, 100..115 = name of the computer using the
        radio, 132..135 = its IPv4 address. The radio sends one with in-use 0
        right after login (radio free), and one with in-use 1 and our client
        name once it has granted our stream request."""
        in_use = d[96]
        computer = P.cstring(d[100:116])
        ip = ".".join(str(b) for b in d[132:136])
        if self._ready is None or self._ready.done():
            return
        if in_use == 0:
            log.info("radio status: free")
            return
        if self._streams_requested and computer == P.CLIENT_NAME:
            self.remote_sid = int.from_bytes(d[8:12], "big")
            self.local_sid = int.from_bytes(d[12:16], "big")
            self.auth_id = bytes(d[26:32])
            name = P.cstring(d[64:96])
            log.info("streams granted, device '%s'", name)
            self._ready.set_result(SessionInfo(name, self.capabilities))
            return
        log.warning("radio in use by '%s' (%s): %s", computer, ip, P.hexdump(d, 144))
        self._ready.set_exception(RadioBusyError(
            "the radio is in use by another client ('%s' at %s)" % (computer or "?", ip)))

    async def _reauth_loop(self):
        try:
            while not self.closed:
                await asyncio.sleep(REAUTH_INTERVAL)
                self._reauth_reply = self._loop.create_future()
                self.send_tracked(P.auth(self.local_sid, self.remote_sid, self._next_inner(),
                                         self.auth_id, 0x05))
                try:
                    await asyncio.wait_for(self._reauth_reply, REAUTH_TIMEOUT)
                except asyncio.TimeoutError:
                    log.warning("re-auth not answered; audio/serial may stop")
        except asyncio.CancelledError:
            pass

    async def logout(self):
        if self._reauth_task is not None:
            self._reauth_task.cancel()
        if self.got_auth_id and self.got_remote_sid and not self.closed:
            log.info("sending deauth")
            self.send_tracked(P.auth(self.local_sid, self.remote_sid, self._next_inner(),
                                     self.auth_id, 0x01))
            await asyncio.sleep(0.5)      # let the radio ask for retransmits
        self.close()


class _ReceivingStream(UdpStream):
    """Adds an RX reorder buffer driven by the event loop."""

    def __init__(self, name, host, port, **kw):
        super().__init__(name, host, port, **kw)
        self.rx = ReorderBuffer(RX_HOLD)
        self._rx_timer = None

    def _rx_add(self, seq, d):
        self.rx.add(seq, d)
        self._rx_poll()

    def _rx_poll(self):
        if self._rx_timer is not None:
            self._rx_timer.cancel()
            self._rx_timer = None
        ready, request, deadline = self.rx.poll(time.monotonic())
        if request is not None:
            log.debug("%s: requesting retransmit #%d-#%d", self.name, request[0], request[1])
            self.request_retransmit(*request)
        for seq, d in ready:
            self.deliver(seq, d)
        if deadline is not None and not self.closed:
            self._rx_timer = self._loop.call_at(
                self._loop.time() + max(0.0, deadline - time.monotonic()), self._rx_poll)

    def deliver(self, seq, d):
        pass

    def close(self):
        if self._rx_timer is not None:
            self._rx_timer.cancel()
        super().close()


class SerialStream(_ReceivingStream):
    def __init__(self, host, port, on_civ, **kw):
        super().__init__("serial", host, port, **kw)
        self.on_civ = on_civ
        self._inner_seq = 0

    def _next_inner(self):
        s = self._inner_seq
        self._inner_seq = (self._inner_seq + 1) & 0xFFFF
        return s

    async def start(self):
        await self.open()
        await self.handshake()
        self.start_pings(first_seq=1)
        self.start_idle()
        self.send_tracked(P.serial_open_close(self.local_sid, self.remote_sid, self._next_inner(), False))
        log.info("serial stream open")

    def send_civ(self, frame):
        self.send_tracked(P.serial_data(self.local_sid, self.remote_sid, self._next_inner(), frame))

    def handle(self, d):
        # Idle packets share the sequence space with CI-V data, so both go
        # through the reorder buffer.
        if P.is_idle(d) or P.is_serial_data(d):
            self._rx_add(int.from_bytes(d[6:8], "little"), d)

    def deliver(self, seq, d):
        if P.is_serial_data(d):
            self.on_civ(bytes(d[21:]))

    def close(self):
        if self.got_remote_sid and not self.closed and self.transport is not None:
            self.send_tracked(P.serial_open_close(self.local_sid, self.remote_sid, self._next_inner(), True))
        super().close()


class AudioStream(_ReceivingStream):
    """RX: PCM16LE mono at sample_rate, ~10 ms per packet. TX: send_frame()."""

    def __init__(self, host, port, on_audio, **kw):
        kw.setdefault("log_packets", False)       # 100 packets/s; logged as counters
        super().__init__("audio", host, port, **kw)
        self.on_audio = on_audio
        self._tx_inner = 1
        self._timeout_handle = None
        self.rx_audio_packets = 0
        self.rx_audio_bytes = 0
        self.require_audio = True

    async def start(self):
        await self.open()
        self.log_packets = True                    # handshake is worth logging
        await self.handshake()
        self.log_packets = False
        self.start_pings(first_seq=1)
        self._arm_timeout()
        log.info("audio stream open")

    def _arm_timeout(self):
        if self._timeout_handle is not None:
            self._timeout_handle.cancel()
        if not self.closed:
            self._timeout_handle = self._loop.call_later(AUDIO_TIMEOUT, self._timed_out)

    def _timed_out(self):
        if not self.require_audio:              # radio in standby: no audio is expected
            self._arm_timeout()
            return
        self._fail(StreamError("no audio from the radio for %.0f s (check DATA MOD / LAN audio "
                               "settings, or reboot the radio)" % AUDIO_TIMEOUT))

    def handle(self, d):
        if P.is_audio_data(d):
            self._arm_timeout()
            self._rx_add(int.from_bytes(d[6:8], "little"), d)
        elif not P.is_idle(d):
            log.debug("audio: unhandled %d-byte packet %s", len(d), P.hexdump(d))

    def deliver(self, seq, d):
        pcm = bytes(d[P.AUDIO_HEADER_LEN:])
        self.rx_audio_packets += 1
        self.rx_audio_bytes += len(pcm)
        self.on_audio(pcm)

    def send_frame(self, pcm):
        """Send one 20 ms frame (1920 bytes of PCM16LE @ 48 kHz) as two packets."""
        for part in (pcm[:P.TX_PART1_BYTES], pcm[P.TX_PART1_BYTES:]):
            if not part:
                continue
            self.send_tracked(P.audio_data(self.local_sid, self.remote_sid, (self._tx_inner - 1) & 0xFFFF, part))
            self._tx_inner = (self._tx_inner + 1) & 0xFFFF

    def close(self):
        if self._timeout_handle is not None:
            self._timeout_handle.cancel()
        super().close()
