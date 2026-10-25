"""A fake RS-BA1 radio for headless tests.

Implements the radio side of the control, serial and audio streams closely
enough to exercise the client: handshake, login/auth/capabilities/stream
request, pings both ways, tracked packets with retransmission, CI-V replies,
and a 1 kHz test tone at 48 kHz in the same 1364 + 556 byte packets the
radios use. Packets listed in drop_audio are "lost" on first transmission so
the client's retransmit path is tested.

Byte layouts mirror the radio captures documented in kappanhang.
"""

import asyncio
import os
import struct
import threading
import time

import numpy as np

from icomremote import civ
from icomremote.rsba1 import packets as P
from icomremote.rsba1.passcode import encode


class _Port(asyncio.DatagramProtocol):
    def __init__(self, radio, name):
        self.radio = radio
        self.name = name
        self.transport = None
        self.addr = None
        self.sid = int.from_bytes(os.urandom(4), "big")
        self.client_sid = 0
        self.seq = 1
        self.history = {}
        self.ready = False
        self.pings_answered = 0
        self.retransmits_served = 0
        self.retransmit_requests = []

    def connection_made(self, transport):
        self.transport = transport

    def send(self, d, track=True):
        if self.addr is None:
            return
        if track:
            d = P.set_seq(d, self.seq)
            self.history[self.seq] = d
            self.history.pop((self.seq - 200) & 0xFFFF, None)
            self.seq = (self.seq + 1) & 0xFFFF
        self.transport.sendto(d, self.addr)

    def hdr(self, length, ptype=0, seq=0):
        return P.header(length, ptype, seq, self.sid, self.client_sid)

    def datagram_received(self, d, addr):
        r = self.radio
        if r.network_down:
            return
        self.addr = addr
        if len(d) >= 16:
            self.client_sid = int.from_bytes(d[8:12], "big")
        if len(d) == 16 and d[4] == P.T_ARE_YOU_THERE:
            self.transport.sendto(self.hdr(16, P.T_I_AM_HERE), addr)
            return
        if len(d) == 16 and d[4] == P.T_ARE_YOU_READY:
            self.transport.sendto(self.hdr(16, P.T_ARE_YOU_READY, 1), addr)
            self.ready = True
            r.on_ready(self)
            return
        if len(d) == 16 and d[4] == P.T_DISCONNECT:
            r.events.append((self.name, "disconnect"))
            self.ready = False
            r.on_disconnect(self)
            return
        if P.is_ping(d):
            if d[16] == 0:
                self.transport.sendto(P.ping(int.from_bytes(d[6:8], "little"), self.sid,
                                             self.client_sid, True, d[17:21]), addr)
            else:
                self.pings_answered += 1
            return
        if P.is_retransmit_request(d):
            for first, last in P.parse_retransmit_request(d):
                self.retransmit_requests.append((first, last))
                s = first
                while True:
                    pkt = self.history.get(s)
                    if pkt is not None:
                        self.retransmits_served += 1
                        self.transport.sendto(pkt, addr)
                    if s == last:
                        break
                    s = (s + 1) & 0xFFFF
            return
        r.on_packet(self, d)


class FakeRadio:
    def __init__(self, user="beer", password="beerbeer", name="IC-7300MK2", civ_address=0xB6,
                 freq_hz=14_074_000, mode=0x01, data=True, filt=1, drop_audio=(), send_audio=True,
                 in_use_by=None, data_mod=0x01, data_off_mod=0x00, lan_mod_level=128, menu_ng=False,
                 tune_seconds=0.6, tune_result=0x01, swr_raw=48,
                 powered=True, remote_power=0x01, power_on_works=True, wake_seconds=1.0):
        self.user, self.password = user, password
        self.name = name
        self.civ_address = civ_address
        self.freq_hz, self.mode, self.data, self.filt = freq_hz, mode, data, filt
        self.drop_audio = set(drop_audio)
        self.send_audio = send_audio
        self.in_use_by = in_use_by          # (computer, ip) of another client holding the radio
        self.menu = {b"\x00\x85": data_mod, b"\x00\x84": data_off_mod, b"\x00\x83": lan_mod_level,
                     b"\x01\x09": remote_power}
        self.menu_ng = menu_ng               # radio that doesn't know these menu items
        self.menu_writes = []
        self.ptt = False
        self.ptt_log = []                    # (time, on)
        self.rf_power = 128                  # 0..255
        self.rf_power_writes = []
        self.tuner = 0x01
        self.tune_seconds = tune_seconds
        self.tune_result = tune_result       # tuner state after a tune cycle (1 = matched, 0 = failed)
        self.tune_started = []
        self.tune_aborted = 0
        self._tune_end = None
        self.swr_raw = swr_raw
        self.powered = powered               # False = standby: network up, CI-V silent, no audio
        self.network_down = False            # after 18 00 with "Shutdown only": nothing answers
        self.power_on_works = power_on_works
        self.wake_seconds = wake_seconds
        self.power_log = []                  # "off" / "on" commands received
        self.tx_audio_while_ptt = 0
        self.tx_audio_while_rx = 0
        self.events = []
        self.civ_received = []
        self.tx_audio = bytearray()
        self.logins = 0
        self._caps_sent = False
        self._serial_inner = 0
        self._phase = 0.0
        self._loop = None
        self._thread = None
        self._tasks = []
        self.control = self.serial = self.audio = None
        self.ports = {}

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self._thread.start()
        ready.wait(5)
        return self

    def _run(self, ready):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def setup():
            for name in ("control", "serial", "audio"):
                tr, proto = await self._loop.create_datagram_endpoint(
                    lambda n=name: _Port(self, n), local_addr=("127.0.0.1", 0))
                setattr(self, name, proto)
                self.ports[name] = tr.get_extra_info("sockname")[1]
            self._tasks.append(self._loop.create_task(self._audio_loop()))
            self._tasks.append(self._loop.create_task(self._ping_loop()))
            self._tasks.append(self._loop.create_task(self._idle_loop()))
        self._loop.run_until_complete(setup())
        ready.set()
        self._loop.run_forever()
        for t in self._tasks:
            t.cancel()
        self._loop.run_until_complete(asyncio.gather(*self._tasks, return_exceptions=True))
        for p in (self.control, self.serial, self.audio):
            if p is not None and p.transport is not None:
                p.transport.close()
        self._loop.run_until_complete(asyncio.sleep(0))
        self._loop.close()

    def stop(self):
        if self._loop is None:
            return

        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    # ------------------------------------------------------------------ dispatch
    def on_ready(self, port):
        self.events.append((port.name, "ready"))

    def on_disconnect(self, port):
        pass

    def on_packet(self, port, d):
        if port is self.control:
            self._control(d)
        elif port is self.serial:
            self._serial(d)
        elif port is self.audio:
            if P.is_audio_data(d):
                self.tx_audio += d[P.AUDIO_HEADER_LEN:]
                if self.ptt:
                    self.tx_audio_while_ptt += 1
                else:
                    self.tx_audio_while_rx += 1

    # ------------------------------------------------------------------ control
    def _control(self, d):
        c = self.control
        n = len(d)
        if n == P.LEN_LOGIN:
            self.logins += 1
            self._caps_sent = False
            ok = d[64:80] == encode(self.user) and d[80:96] == encode(self.password)
            r = bytearray(P.LEN_LOGIN_REPLY)
            r[0:16] = c.hdr(P.LEN_LOGIN_REPLY)
            r[16:26] = bytes([0, 0, 0, 0x50, 0x02, 0x00, 0, d[23], d[24], 0])
            r[26:32] = os.urandom(6)
            if not ok:
                r[48:52] = b"\xff\xff\xff\xfe"
            r[64:68] = b"FTTH"
            c.send(bytes(r))
            self.events.append(("control", "login_ok" if ok else "login_bad"))
        elif n == P.LEN_AUTH:
            magic = d[21]
            r = bytearray(P.LEN_AUTH)
            r[0:16] = c.hdr(P.LEN_AUTH)
            r[16:32] = bytes([0, 0, 0, 0x30, 0x02, magic, 0, d[23], d[24], 0]) + bytes(d[26:32])
            # Order seen from a real IC-7300MK2: capabilities, status, then the auth reply.
            if magic == 0x05 and not self._caps_sent:           # once per login, like the real radio
                self._caps_sent = True
                self._send_caps()
            c.send(bytes(r))
            self.events.append(("control", "auth_%02x" % magic))
        elif n == P.LEN_CONNINFO:
            name = P.cstring(d[64:96])
            self.events.append(("control", "stream_request:%s" % name))
            self.requested_rate = struct.unpack_from(">I", d, 116)[0]
            self.requested_ports = (struct.unpack_from(">I", d, 124)[0],
                                    struct.unpack_from(">I", d, 128)[0])
            if name == self.name and self.in_use_by is None:
                self._send_status(in_use=True, computer=P.CLIENT_NAME, ip=(192, 168, 3, 3))

    def _send_caps(self):
        c = self.control
        r = bytearray(P.CAP_HEADER_LEN + P.CAP_RADIO_LEN)
        r[0:16] = c.hdr(len(r))
        struct.pack_into(">H", r, 64, 1)
        o = P.CAP_HEADER_LEN
        r[o:o + 16] = os.urandom(16)
        nm = self.name.encode()
        r[o + 16:o + 16 + len(nm)] = nm
        r[o + 48:o + 59] = b"ICOM_VAUDIO"
        r[o + 80:o + 82] = b"\x3f\x3f"
        r[o + 82] = self.civ_address
        c.send(bytes(r))
        self.events.append(("control", "caps"))
        # Like the IC-7300MK2: an unsolicited status right after the capabilities.
        if self.in_use_by is None:
            self._send_status(in_use=False)
        else:
            self._send_status(True, *self.in_use_by)

    def _send_status(self, in_use, computer="", ip=(0, 0, 0, 0)):
        """0x90 status: in-use flag at 96, computer name at 100, IPv4 at 132."""
        c = self.control
        r = bytearray(P.LEN_CONNINFO)
        r[0:16] = c.hdr(P.LEN_CONNINFO)
        r[16:26] = bytes([0, 0, 0, 0x80, 0x03, 0x00, 0, 0, 0, 0])
        r[26:32] = os.urandom(6)
        nm = self.name.encode()
        r[64:64 + len(nm)] = nm
        r[96] = 0x01 if in_use else 0x00
        r[100:100 + len(computer)] = computer.encode()
        r[132:136] = bytes(ip)
        c.send(bytes(r))

    # ------------------------------------------------------------------ serial
    def _serial(self, d):
        if len(d) >= 22 and d[16] == P.SERIAL_OPEN_CLOSE:
            self.events.append(("serial", "close" if d[21] == 0 else "open"))
            return
        if not P.is_serial_data(d):
            return
        frames, _ = civ.split_frames(bytes(d[21:]))
        for f in frames:
            self.civ_received.append(f)
            if f[2] != self.civ_address:
                continue
            if f[4] == 0x18 and len(f) == 7:
                self._power_cmd(f[5])
                continue
            if not self.powered:
                continue                         # standby: CI-V doesn't answer
            for reply in self._civ_reply(f):
                self._serial_send(reply)

    def _power_cmd(self, on):
        self.power_log.append("on" if on else "off")
        if on:
            if not self.powered and self.power_on_works and not self.network_down:
                self._loop.call_later(self.wake_seconds, setattr, self, "powered", True)
            return
        if self.powered:
            self._serial_send(civ.frame(civ.CONTROLLER, civ.OK, frm=self.civ_address))
        self.powered = False
        self.ptt = False
        if self.menu[b"\x01\x09"] == 0x00:      # Shutdown only: the network goes down too
            self.network_down = True

    def _civ_reply(self, f):
        a = self.civ_address
        cmd, payload = f[4], f[5:-1]
        mk = lambda c, sub=None, data=b"": civ.frame(civ.CONTROLLER, c, sub, data, frm=a)
        echo = [f]                                  # radios echo the command on the LAN stream too
        if cmd == 0x03:
            return echo + [mk(0x03, None, civ.encode_freq(self.freq_hz))]
        if cmd == 0x04:
            return echo + [mk(0x04, None, bytes([self.mode, self.filt]))]
        if cmd == 0x1A and payload[:1] == b"\x06":
            if len(payload) == 1:
                return echo + [mk(0x1A, 0x06, bytes([1 if self.data else 0, self.filt if self.data else 0]))]
            self.data = payload[1] != 0
            return echo + [mk(civ.OK)]
        if cmd == 0x06 and len(payload) >= 1:
            self.mode = payload[0]
            if len(payload) > 1:
                self.filt = payload[1]
            return echo + [mk(civ.OK)]
        if cmd == 0x1A and payload[:1] == b"\x03" and len(payload) == 1:
            if self.mode == 0x05:                   # FM: no IF width
                return echo + [mk(civ.NG)]
            idx = {1: 34, 2: 28, 3: 22}[self.filt] if self.mode != 0x02 else {1: 44, 2: 29, 3: 14}[self.filt]
            return echo + [mk(0x1A, 0x03, bytes([civ.bcd_byte(idx)]))]
        if cmd == 0x1C and payload[:1] == b"\x01":
            if self._tune_end is not None and time.monotonic() >= self._tune_end:
                self.tuner, self._tune_end = self.tune_result, None
            if len(payload) == 1:
                return echo + [mk(0x1C, 0x01, bytes([self.tuner]))]
            v = payload[1]
            if v == 0x02:
                self.tuner, self._tune_end = 0x02, time.monotonic() + self.tune_seconds
                self.tune_started.append(time.monotonic())
            else:
                if self.tuner == 0x02:
                    self.tune_aborted += 1
                self.tuner, self._tune_end = v, None
            return echo + [mk(civ.OK)]
        if cmd == 0x14 and payload[:1] == b"\x0a":
            if len(payload) == 1:
                return echo + [mk(0x14, 0x0A, civ.encode_level(self.rf_power))]
            self.rf_power = civ.decode_level(payload[1:3])
            self.rf_power_writes.append(self.rf_power)
            return echo + [mk(civ.OK)]
        if cmd == 0x15 and payload[:1] in (b"\x11", b"\x12"):
            on_air = self.ptt or self.tuner == 0x02
            if payload[:1] == b"\x11":
                v = int(213 * self.rf_power / 255) if on_air else 0
            else:
                v = self.swr_raw if on_air else 0
            return echo + [mk(0x15, payload[0], civ.encode_level(v))]
        if cmd == 0x1C and payload[:1] == b"\x00":
            if len(payload) == 1:
                return echo + [mk(0x1C, 0x00, b"\x01" if self.ptt else b"\x00")]
            self.ptt = payload[1] == 1
            self.ptt_log.append((time.monotonic(), self.ptt))
            return echo + [mk(civ.OK)]
        if cmd == 0x1A and payload[:1] == b"\x05" and len(payload) >= 3:
            item = bytes(payload[1:3])
            if self.menu_ng or item not in self.menu:
                return echo + [mk(civ.NG)]
            if len(payload) == 3:
                v = self.menu[item]
                data = bytes([civ.bcd_byte(v)]) if item != b"\x00\x83" else civ.encode_level(v)
                return echo + [mk(0x1A, 0x05, item + data)]
            v = civ.bcd_value(payload[3]) if len(payload) == 4 else civ.decode_level(payload[3:5])
            self.menu[item] = v
            self.menu_writes.append((item, v))
            return echo + [mk(civ.OK)]
        if cmd == 0x15 and payload[:1] == b"\x02":
            return echo + [mk(0x15, 0x02, civ.encode_level(120))]
        if cmd == 0x05:
            self.freq_hz = civ.decode_freq(payload)
            return echo + [mk(civ.OK)]
        return echo + [mk(civ.NG)]

    def _serial_send(self, frame):
        s = self.serial
        s.send(P.serial_data(s.sid, s.client_sid, self._serial_inner, frame))
        self._serial_inner = (self._serial_inner + 1) & 0xFFFF

    def push_freq(self, hz):
        """Simulate turning the VFO knob (CI-V transceive broadcast)."""
        def _do():
            self.freq_hz = hz
            self._serial_send(civ.frame(civ.BROADCAST, 0x00, None, civ.encode_freq(hz), frm=self.civ_address))
        self._loop.call_soon_threadsafe(_do)

    # ------------------------------------------------------------------ periodic
    async def _audio_loop(self):
        try:
            sent = 0
            while True:
                await asyncio.sleep(0.02)
                a = self.audio
                if not (self.send_audio and self.powered and a.ready and self.serial.ready):
                    continue
                t = (np.arange(960) + self._phase) / 48000.0
                self._phase += 960
                pcm = (8000 * np.sin(2 * np.pi * 1000 * t)).astype("<i2").tobytes()
                for part in (pcm[:P.TX_PART1_BYTES], pcm[P.TX_PART1_BYTES:]):
                    pkt = P.set_seq(P.audio_data(a.sid, a.client_sid, sent, part), a.seq)
                    a.history[a.seq] = pkt
                    a.history.pop((a.seq - 200) & 0xFFFF, None)
                    if a.seq not in self.drop_audio:
                        a.transport.sendto(pkt, a.addr)
                    a.seq = (a.seq + 1) & 0xFFFF
                    sent += 1
        except asyncio.CancelledError:
            pass

    async def _ping_loop(self):
        try:
            seq = 0
            while True:
                await asyncio.sleep(0.5)
                if self.network_down:
                    continue
                for p in (self.control, self.serial, self.audio):
                    if p is not None and p.ready:
                        p.transport.sendto(P.ping(seq, p.sid, p.client_sid, False, os.urandom(4)), p.addr)
                seq += 1
        except asyncio.CancelledError:
            pass

    async def _idle_loop(self):
        try:
            while True:
                await asyncio.sleep(0.3)
                if self.network_down:
                    continue
                s = self.serial
                if s is not None and s.ready:
                    s.send(P.control(P.T_DATA, 0, s.sid, s.client_sid))
        except asyncio.CancelledError:
            pass
