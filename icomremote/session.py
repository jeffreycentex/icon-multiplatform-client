"""Radio session: one worker thread running asyncio, owning the three UDP streams.

Threading model
---------------
* connect()/disconnect()/send_civ() may be called from any thread (GUI,
  rigctld). Work is handed to the session's event loop with
  call_soon_threadsafe, so only the loop thread touches sockets.
* Received audio is decoded and written to the sinks on the loop thread.
* Events go to one callback, (kind, payload); the GUI forwards them into Qt
  signals so they arrive on the GUI thread.

Events: status (text, level) | connected bool | radio dict | rig RigState |
        meter (name, raw) | stats dict | stopped None
"""

import asyncio
import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Optional

import numpy as np

from . import civ, netcompat
from .restore import RestoreStore
from .state import freq_limits
from .rsba1.stream import StreamError
from .rsba1.streams import AudioStream, AuthError, ControlStream, SerialStream
from .sinks import SoundCardSink, make_file_sink
from .timerres import high_resolution_timer

log = logging.getLogger(__name__)

# Python 3.15 imports some stdlib modules lazily. Touch the thread-pool class
# here, on the importing (main) thread, so asyncio never has to finish that
# import from the session thread (seen failing on 3.15.0 with
# "'lazy_import' object is not callable").
_ = concurrent.futures.ThreadPoolExecutor

SAMPLE_RATE = 48000
FAST_RETRIES = 5
FAST_RETRY_DELAY = 1.0
SLOW_RETRY_DELAY = 65.0     # the IC-705 drops audio if you re-login too often
POLL_INTERVAL = 0.25                # TX meters every tick, S-meter every 2nd, state every 8th
TUNE_TIMEOUT = 30.0
CIV_SILENT_STANDBY = 3.0            # no CI-V answer for this long: radio is in standby
LINK_TIMEOUT = 6.0                  # nothing at all from the radio for this long: connection lost
POWER_ON_TIMEOUT = 25.0
RF_POWER_HOLD = 1.5
STATS_INTERVAL = 1.0
DEFAULT_PTT_TIMEOUT = 180.0
# Menu settings the app switches to LAN while connected: key -> (label, item table)
MOD_SETTINGS = {"data_mod": ("DATA MOD", "data_mod"),        # label, key in civ.MODELS
                "data_off_mod": ("DATA OFF MOD", "data_off_mod")}
FENCE_SECONDS = 1.5
CIV_ANSWER_TIMEOUT = 1.0


@dataclass(frozen=True)
class ConnectionSpec:
    host: str
    user: str
    password: str
    control_port: int = 50001
    serial_port: int = 50002
    audio_port: int = 50003
    civ_address: Optional[int] = None     # None = take it from the radio
    tx_buffer_ms: int = 300
    bind_same_ports: bool = False         # local UDP ports = radio ports, like kappanhang

    def label(self):
        return "%s:%d" % (self.host, self.control_port)


class _Disconnected(Exception):
    pass


class NotConnected(RuntimeError):
    pass


class PttRefused(RuntimeError):
    """PTT not allowed right now; the message says why."""


class RadioSession:
    def __init__(self, on_event=None, state_dir=None):
        self._on_event = on_event or (lambda kind, payload: None)
        self._thread = None
        self._loop = None
        self._stop = None
        self._spec = None
        self._connected = False
        self._serial = None
        self._audio = None
        self._control = None
        self._decoder = None
        self.info = None
        self.civ_address = None
        self.rig = civ.RigState()
        self._rig_lock = threading.Lock()
        self.meters = {}
        self.freq_limits = freq_limits(None)
        self._sink_lock = threading.Lock()
        self._sinks = {}
        self._connected_at = None
        self._civ_waiters = []
        # Stale-reply fences: reads sent / replies received per group, and per
        # group (target reply count, deadline) after a change made by the app.
        self._reads_sent = dict.fromkeys(civ.READ_GROUPS, 0)
        self._replies = dict.fromkeys(civ.READ_GROUPS, 0)
        self._fence = {}
        self._keying = False             # key-down requested, waiting for the DATA check
        # PTT requests: the newest one wins. Callers set the desired state
        # (self.ptt, owner) under the lock and bump the sequence; the loop applies
        # a request only if it is still the newest, so a stale release can never
        # clear a newer press (or a stale press re-key after a release).
        self._ptt_lock = threading.RLock()
        self._ptt_seq = 0
        # RF power and tuner
        self.rf_power = None             # 0..255 as the radio reports it
        self._rf_hold_until = 0.0        # ignore reports briefly after a change from the app
        self.tuner = None                # 0 off, 1 on, 2 tuning
        self._tuning = False             # a TUNE started from the app is running
        self._tune_timer = None
        # power: "on", "standby" (network up, CI-V silent) or None (unknown)
        self.power = None
        self.remote_power_setting = None   # 1A 05 01 09 value, None = unknown
        self._last_civ_rx = 0.0
        self._powering_on = False
        self._power_on_deadline = 0.0
        self._ignore_civ_until = 0.0
        self._mods_ready = False           # DATA MOD setup done for this power-on
        self._tuner_reads = 0            # 1C 01 reads sent / answers received, to ignore
        self._tuner_replies = 0          # answers to reads sent before a tune started
        self._tune_fence = 0
        # transmit
        self.ptt = False                 # what we commanded
        self.tx = False                  # what the radio reports (1C 00)
        self.ptt_timeout = DEFAULT_PTT_TIMEOUT
        self._keyed = False              # loop-thread view of self.ptt
        self._ptt_owner = None
        self._ptt_timer = None
        self._tx_devices = {"data": None, "voice": None}   # kind -> (key, gain)
        self._tx_sources = {}            # kind -> open source
        self._tx_kind = None             # "data" or "voice" while keyed
        self._tx_src = None              # which source is feeding the radio while keyed
        self._key_voice_ok = False       # current key-down request allows voice
        self._tx_source_factory = None   # tests inject a fake source
        # DATA MOD / DATA OFF MOD management: setting -> state
        self.mods = {k: {"value": None, "original": None, "item": None, "manage": True}
                     for k in MOD_SETTINGS}
        self._store = RestoreStore(state_dir)

    # ------------------------------------------------------------------ events
    def _emit(self, kind, payload=None):
        try:
            self._on_event(kind, payload)
        except Exception:
            log.exception("event handler failed for %s", kind)

    def _status(self, text, level="info"):
        (log.warning if level == "error" else log.info)(text)
        self._emit("status", (text, level))

    @property
    def connected(self):
        return self._connected

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _set_connected(self, flag):
        if flag != self._connected:
            self._connected = flag
            self._connected_at = time.monotonic() if flag else None
            self._emit("connected", flag)

    # ------------------------------------------------------------------ lifecycle
    def connect(self, spec: ConnectionSpec):
        self.disconnect()
        self._spec = spec
        ready = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, args=(ready,),
                                        name="rsba1-session", daemon=True)
        self._thread.start()
        ready.wait(2.0)

    def disconnect(self, timeout=8.0):
        t = self._thread
        if t is None:
            return
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        t.join(timeout)
        self._thread = None

    def shutdown(self):
        self.disconnect()
        self._close_tx_sources()
        self.stop_recording()
        self._install_sink("card", None)

    def _thread_main(self, ready):
        with high_resolution_timer():
            netcompat.run(self._main(ready))

    async def _main(self, ready):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        ready.set()
        failures = 0
        try:
            while not self._stop.is_set():
                err = await self._run_once()
                if self._stop.is_set():
                    break
                if isinstance(err, AuthError):
                    self._status("Login failed: %s" % err, "error")
                    break
                if err is None:
                    failures = 0
                    delay = FAST_RETRY_DELAY
                else:
                    failures += 1
                    delay = FAST_RETRY_DELAY if failures <= FAST_RETRIES else SLOW_RETRY_DELAY
                    if failures > FAST_RETRIES:
                        failures = 0
                    self._status("%s; reconnecting in %.0f s" % (err, delay), "error")
                try:
                    await asyncio.wait_for(self._stop.wait(), delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._set_connected(False)
            self._status("Disconnected")
            self._emit("stopped")

    async def _run_once(self):
        """One connection attempt and its lifetime. Returns the error that ended it."""
        spec = self._spec
        loop = self._loop
        failed = loop.create_future()

        def on_error(exc):
            if not failed.done():
                failed.set_result(exc)

        control = serial = audio = None
        tasks = []
        try:
            self._status("Connecting to %s ..." % spec.label())
            control = ControlStream(spec.host, spec.control_port, spec.user, spec.password,
                                    spec.serial_port, spec.audio_port, SAMPLE_RATE,
                                    spec.tx_buffer_ms, on_error=on_error,
                                    local_port=self._local(spec.control_port))
            self._control = control
            info = await self._until_failed(control.start(), failed)
            self.info = info
            self.freq_limits = freq_limits(info.radio_name)
            addr = self._choose_civ_address(info)
            self._decoder = civ.CivDecoder(addr)

            serial = SerialStream(spec.host, spec.serial_port, self._on_civ, on_error=on_error,
                                  local_port=self._local(spec.serial_port))
            self._serial = serial
            await self._until_failed(serial.start(), failed)
            audio = AudioStream(spec.host, spec.audio_port, self._on_audio, on_error=on_error,
                                local_port=self._local(spec.audio_port))
            self._audio = audio
            await self._until_failed(audio.start(), failed)

            with self._sink_lock:
                for name, sink in list(self._sinks.items()):
                    self._sink_set_rate(name, sink)
            self._emit("radio", {"name": info.radio_name, "civ_address": addr,
                                 "reported_civ": info.civ_address,
                                 "audio": info.capabilities.audio_name if info.capabilities else ""})
            self._set_connected(True)
            self._status("Connected to %s (%s, CI-V %02Xh)" % (spec.label(), info.radio_name, addr))

            for f in (civ.read_freq(addr), civ.read_mode(addr), civ.read_data_mode(addr),
                      civ.read_if_width(addr), civ.read_ptt(addr), civ.read_rf_power(addr),
                      civ.read_tuner(addr)):
                self._tx_civ(f)
            self.power = None
            self._mods_ready = False
            self._last_civ_rx = 0.0
            r = await self._civ_ask(civ.read_freq(addr), lambda k, v: k == "reply" and v == "freq",
                                    timeout=1.5, attempts=2)
            if r is not None:
                await self._power_is_on(info.radio_name, spec.host)
            else:
                self._set_power("standby")
            self._open_tx_sources()
            tasks.append(loop.create_task(self._poll_loop(addr)))
            tasks.append(loop.create_task(self._stats_loop()))

            stop_wait = loop.create_task(self._stop.wait())
            done, _ = await asyncio.wait({failed, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            stop_wait.cancel()
            return failed.result() if failed in done else None
        except _Disconnected as e:
            return e.args[0]
        except (StreamError, OSError) as e:
            return e
        except Exception as e:  # pragma: no cover - unexpected
            log.exception("session error")
            return e
        finally:
            for t in tasks:
                t.cancel()
            # Unkey and put radio settings back while the serial stream may still work.
            if serial is not None and not serial.closed:
                try:
                    self._stop_tune()
                    self._unkey()
                    if self.power == "on":
                        await self._restore_data_mod()
                except Exception:
                    log.exception("cleanup before logout failed")
            else:
                self._unkey()
            self._close_tx_sources()
            self.power = None
            self._powering_on = False
            self._set_connected(False)
            # Same order as kappanhang: deauth + close control, then serial, then audio.
            if control is not None:
                try:
                    await control.logout()
                except Exception:
                    control.close()
            if serial is not None:
                serial.close()
            if audio is not None:
                audio.close()
            self._audio = self._serial = self._control = None

    def _local(self, port):
        return port if self._spec.bind_same_ports else 0

    async def _until_failed(self, coro, failed):
        """Await coro, but abort if a stream reported a fatal error meanwhile."""
        task = asyncio.ensure_future(coro)
        stop_wait = asyncio.ensure_future(self._stop.wait())
        done, _ = await asyncio.wait({task, failed, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
        stop_wait.cancel()
        if task in done:
            return task.result()
        task.cancel()
        if failed in done:
            raise _Disconnected(failed.result())
        raise _Disconnected(None)

    def _choose_civ_address(self, info):
        reported = info.civ_address
        wanted = self._spec.civ_address
        if wanted is None:
            addr = reported if reported else civ.DEFAULT_ADDRESSES.get(info.radio_name, 0xA4)
        else:
            addr = wanted
            if reported and reported != wanted:
                self._status("Note: the radio reports CI-V address %02Xh but %02Xh is configured"
                             % (reported, wanted), "error")
        self.civ_address = addr
        return addr

    # ------------------------------------------------------------------ periodic
    async def _poll_loop(self, addr):
        n = 0
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                s = self._serial
                if s is None:
                    return
                n += 1
                now = time.monotonic()
                c = self._control
                if c is not None and c.last_rx and now - c.last_rx > LINK_TIMEOUT:
                    c._fail(StreamError("the radio stopped responding (no packets for %.0f s)"
                                        % LINK_TIMEOUT))
                    return
                if self.power == "on" and self._last_civ_rx and now - self._last_civ_rx > CIV_SILENT_STANDBY:
                    self._set_power("standby")
                if self._powering_on and now > self._power_on_deadline:
                    self._powering_on = False
                    self._emit("powering_on", False)
                    self._status("The radio did not respond to power on", "error")
                if self.power == "standby":
                    if n % 4 == 0:                # cheap probe: does CI-V answer again?
                        self._tx_civ(civ.read_freq(addr))
                    continue
                if self.tx or self._keyed or self._tuning:
                    self._tx_civ(civ.read_po(addr))
                    self._tx_civ(civ.read_swr(addr))
                elif n % 2 == 0:
                    self._tx_civ(civ.read_smeter(addr))
                if self._tuning:
                    self._tx_civ(civ.read_tuner(addr))
                if n % 8 == 0:            # the radio also pushes changes (CI-V transceive)
                    self._tx_civ(civ.read_freq(addr))
                    self._tx_civ(civ.read_mode(addr))
                    self._tx_civ(civ.read_data_mode(addr))
                    if self.rig.mode != "FM":
                        self._tx_civ(civ.read_if_width(addr))
                    self._tx_civ(civ.read_rf_power(addr))
                    if not self._tuning:
                        self._tx_civ(civ.read_tuner(addr))
                rtt = self._control.rtt if self._control else 0.0
                for st in (self._serial, self._audio):
                    if st is not None:
                        st.rx.rtt = rtt
        except asyncio.CancelledError:
            pass

    async def _stats_loop(self):
        try:
            while True:
                await asyncio.sleep(STATS_INTERVAL)
                self._emit("stats", self.stats())
        except asyncio.CancelledError:
            pass

    def stats(self):
        out = {"connected": self._connected}
        if self._connected_at is not None:
            out["uptime"] = time.monotonic() - self._connected_at
        c, s, a = self._control, self._serial, self._audio
        if c is not None:
            out["rtt_ms"] = 1000.0 * c.rtt
        tx = rx = 0
        for st in (c, s, a):
            if st is not None:
                tx += st.stats.tx_bytes
                rx += st.stats.rx_bytes
        out["tx_bytes"], out["rx_bytes"] = tx, rx
        if a is not None:
            out["audio_packets"] = a.rx_audio_packets
            out["audio_seconds"] = a.rx_audio_bytes / (2.0 * SAMPLE_RATE)
            out["audio_lost"] = a.rx.lost
            out["audio_retx_requests"] = a.rx.retransmit_requests
        if s is not None:
            out["serial_lost"] = s.rx.lost
            out["serial_retx_requests"] = s.rx.retransmit_requests
        return out

    # ------------------------------------------------------------------ CI-V
    def _on_civ(self, data):
        dec = self._decoder
        if dec is None:
            return
        events = dec.feed(data)
        # Liveness: answers that carry state. Not bare OK/NG (the radio
        # acknowledges our power-off), and not right after a power-off.
        if (any(k not in ("foreign", "other", "ack") for k, _ in events)
                and time.monotonic() >= self._ignore_civ_until):
            self._last_civ_rx = time.monotonic()
            if self.power == "standby" and self._mods_ready is not None:
                self._loop.create_task(self._woke_up())
        for kind, value in events:
            for match, fut in list(self._civ_waiters):
                if not fut.done() and match(kind, value):
                    fut.set_result((kind, value))
            if kind == "reply":
                self._replies[value] += 1
            elif kind in ("freq", "mode"):
                st = self._fenced_state(dec.state)
                with self._rig_lock:
                    changed = st != self.rig
                    self.rig = st
                if changed:
                    self._emit("rig", st)
                # (a key-down still waiting for its check is refused by _key_after_check)
                if self._keyed and st.mode and st.data != (self._tx_kind == "data"):
                    self._unkey(reason=(
                        "Unkeyed: the radio left DATA mode (now %s)" % st.mode_label
                        if self._tx_kind == "data" else
                        "Unkeyed: the radio switched to %s while transmitting voice" % st.mode_label))
            elif kind == "ptt":
                tx = value != 0
                if tx != self.tx:
                    self.tx = tx
                    self._emit("tx", tx)
            elif kind in ("smeter", "po", "swr"):
                self.meters[kind] = value
                self._emit("meter", (kind, value))
            elif kind == "rfpower":
                if time.monotonic() >= self._rf_hold_until and value != self.rf_power:
                    self.rf_power = value
                    self._emit("rf_power", value)
            elif kind == "tuner":
                self._on_tuner(value)
            elif kind == "foreign":
                log.debug("CI-V frame from unexpected address %02Xh", value)
            elif kind == "ack" and not value:
                # Normal in standby (the radio answers every command with NG)
                # and for items a mode doesn't have (1A 03 in FM).
                log.debug("radio answered NG to a CI-V command")

    def _tx_civ(self, frame):
        """Loop thread: send a CI-V frame, counting reads per group."""
        if self._serial is None or self._serial.closed:
            return
        g = civ.read_group(frame)
        if g is not None:
            self._reads_sent[g] += 1
        elif len(frame) == 7 and frame[4] == 0x1C and frame[5] == 0x01:
            self._tuner_reads += 1
        self._serial.send_civ(frame)

    def _fenced_state(self, st):
        """Replace fields whose fence is up with the app's own (newer) values."""
        now = time.monotonic()
        for g, (target, deadline) in list(self._fence.items()):
            if self._replies[g] >= target or now >= deadline:
                del self._fence[g]
                continue
            st = replace(st, **{f: getattr(self.rig, f) for f in civ.READ_GROUPS[g]})
        return st

    async def _civ_ask(self, frame, match, timeout=CIV_ANSWER_TIMEOUT, attempts=2):
        """Send a CI-V frame and wait for the first decoded event match(kind, value)
        accepts. Returns (kind, value) or None. Loop thread only."""
        for _ in range(attempts):
            if self._serial is None or self._serial.closed:
                return None
            fut = self._loop.create_future()
            entry = (match, fut)
            self._civ_waiters.append(entry)
            try:
                self._tx_civ(frame)
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                continue
            finally:
                if entry in self._civ_waiters:
                    self._civ_waiters.remove(entry)
        return None

    async def _read_menu(self, item):
        # Only the matching answer counts: a late NG (e.g. to a probe sent while
        # the radio was in standby) must not be taken for this read's answer.
        r = await self._civ_ask(civ.read_menu(self.civ_address, item),
                                lambda k, v: k == "menu" and v[0] == item)
        if r is None or r[0] != "menu" or not r[1][1]:
            return None
        return civ.bcd_value(r[1][1][0]) if len(r[1][1]) == 1 else civ.decode_level(r[1][1][:2])

    async def _write_menu(self, item, value):
        """Set a one-byte menu value and confirm by reading it back."""
        await self._civ_ask(civ.set_menu(self.civ_address, item, [civ.bcd_byte(value)]),
                            lambda k, v: k == "ack")
        return await self._read_menu(item) == value

    def send_civ(self, frame):
        """Thread-safe: queue a raw CI-V frame for the radio."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return False

        def _send():
            self._tx_civ(frame)
        loop.call_soon_threadsafe(_send)
        return True

    # ------------------------------------------------------------------ control API
    # Thread-safe. Each setter updates self.rig at once (so a rigctl 'f' right
    # after 'F' sees the new value), queues the CI-V command and a read-back;
    # the radio's answer then confirms or corrects the state.

    def _require_on(self):
        if not self._connected:
            raise NotConnected("not connected to the radio")
        if self.power != "on":
            raise NotConnected("the radio is not powered on")

    def _apply(self, new_state, frames):
        self._require_on()
        with self._rig_lock:
            self.rig = new_state
        self._emit("rig", new_state)

        def _send():
            if self._decoder is not None:
                self._decoder.state = new_state
            if self._serial is not None:
                for f in frames:
                    self._tx_civ(f)
                deadline = time.monotonic() + FENCE_SECONDS
                for f in frames:
                    g = civ.read_group(f)
                    if g is not None:            # ignore replies to reads sent before this change
                        self._fence[g] = (self._reads_sent[g], deadline)
        self._loop.call_soon_threadsafe(_send)
        return new_state

    def set_frequency(self, hz):
        hz = int(round(float(hz)))
        lo, hi = self.freq_limits
        if not lo <= hz <= hi:
            raise ValueError("%.3f kHz is outside %.0f-%.0f kHz" % (hz / 1e3, lo / 1e3, hi / 1e3))
        a = self.civ_address
        st = replace(self.rig, freq_hz=hz)
        return self._apply(st, [civ.set_freq(a, hz), civ.read_freq(a)])

    def set_mode(self, civ_mode, data, filt=None):
        """civ_mode: LSB/USB/AM/FM; data: DATA mode on/off; filt: 1-3 or None = keep."""
        if civ_mode not in civ.MODE_CODES:
            raise ValueError("unsupported mode %r" % civ_mode)
        filt = filt or self.rig.filter or 1
        if filt not in (1, 2, 3):
            raise ValueError("filter must be 1-3")
        if self.ptt and bool(data) != self.rig.data:
            self.set_ptt(False, reason="Unkeyed: switching to %s while transmitting"
                         % (civ_mode + ("-D" if data else "")))
        a = self.civ_address
        st = replace(self.rig, mode=civ_mode, data=bool(data), filter=filt, if_width_index=-1)
        frames = [civ.set_mode(a, civ_mode, filt), civ.set_data_mode(a, data, filt),
                  civ.read_mode(a), civ.read_data_mode(a)]
        if civ_mode != "FM":
            frames.append(civ.read_if_width(a))
        return self._apply(st, frames)

    def set_filter(self, filt):
        if not self.rig.mode:
            raise NotConnected("mode not known yet")
        return self.set_mode(self.rig.mode, self.rig.data, filt)

    # ------------------------------------------------------------------ PTT
    # Safety rules
    #   Data (radio in USB-D etc.): data TX input selected, DATA MOD = LAN.
    #   Voice (radio in LSB/USB/AM/FM): only when the caller allows voice (the
    #   GUI's armed space bar; never rigctl or the PTT button), voice TX input
    #   selected, DATA OFF MOD = LAN.
    #   Key-down asks the radio for its DATA state first and decides from the
    #   radio's answer, not from the app's copy.
    #   Unkeyed after ptt_timeout, on any voice<->data switch, on disconnect /
    #   shutdown, when the TX input in use changes, and (via rigctld) when the
    #   client that keyed disconnects.

    @property
    def data_mod(self):
        return self.mods["data_mod"]["value"]

    @property
    def manage_data_mod(self):
        return self.mods["data_mod"]["manage"]

    @manage_data_mod.setter
    def manage_data_mod(self, on):
        self.mods["data_mod"]["manage"] = bool(on)

    @property
    def manage_data_off_mod(self):
        return self.mods["data_off_mod"]["manage"]

    @manage_data_off_mod.setter
    def manage_data_off_mod(self, on):
        self.mods["data_off_mod"]["manage"] = bool(on)

    def _rule_violation(self, data, mode, voice_ok):
        """Why keying is not allowed for a radio in (mode, data), or None."""
        kind = "data" if data else "voice"
        if kind == "voice":
            if not voice_ok:
                return ("transmit from programs and the PTT button is only allowed in data modes "
                        "(USB-D, LSB-D, AM-D, FM-D); the radio is in %s" % civ.RigState(mode=mode).mode_label)
            if mode not in ("LSB", "USB", "AM", "FM"):
                return "voice transmit is only offered in LSB/USB/AM/FM; the radio is in %s" % mode
        if self._source_kind(kind) is None:
            return "no transmit audio input selected"
        setting = "data_mod" if kind == "data" else "data_off_mod"
        v = self.mods[setting]["value"]
        if v is not None and v != self._lan:
            return "%s is %s, not LAN" % (MOD_SETTINGS[setting][0], self._mod_name(v))
        return None

    def ptt_refusal(self, voice_ok=False):
        """Why PTT can't be keyed now, or None if it can."""
        if not self._connected:
            return "not connected to the radio"
        if self.power != "on":
            return "the radio is not powered on"
        if not self.rig.mode:
            return "the radio's mode is not known yet"
        if self._tuning or self.tuner == civ.TUNER_TUNING:
            return "the antenna tuner is tuning"
        return self._rule_violation(self.rig.data, self.rig.mode, voice_ok)

    def set_ptt(self, on, owner=None, reason=None, voice_ok=False):
        """Thread-safe. Raises PttRefused when keying isn't allowed.
        voice_ok: allow transmitting in a voice mode (GUI space bar only)."""
        with self._ptt_lock:
            if on:
                why = self.ptt_refusal(voice_ok)
                if why:
                    raise PttRefused("PTT refused: " + why)
            elif not self.ptt:
                return
            self._ptt_seq += 1
            seq = self._ptt_seq
            self.ptt = bool(on)
            self._ptt_owner = owner if on else None
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._ptt_request, seq, bool(on), reason, voice_ok)

    def release_ptt(self, owner, reason="Unkeyed: the rigctl client that keyed PTT disconnected"):
        """Unkey if `owner` keyed (rigctld calls this when a client disconnects)."""
        with self._ptt_lock:
            if self.ptt and self._ptt_owner is owner:
                self.set_ptt(False, reason=reason)

    def _ptt_request(self, seq, on, reason, voice_ok):
        with self._ptt_lock:
            if seq != self._ptt_seq:
                return                       # superseded by a newer request or an automatic unkey
        self._ptt_apply(on, reason, voice_ok)

    def _unkey(self, reason=None):
        """Loop thread (or teardown): unkey now, and invalidate requests in flight."""
        with self._ptt_lock:
            self._ptt_seq += 1
            self.ptt = False
            self._ptt_owner = None
        self._ptt_apply(False, reason)

    def _ptt_apply(self, on, reason=None, voice_ok=False):
        """Loop thread. Key-down / unkey the radio and start/stop TX audio."""
        if on:
            self._key_voice_ok = voice_ok
            if self._ptt_timer is not None:
                self._ptt_timer.cancel()
            self._ptt_timer = self._loop.call_later(self.ptt_timeout, self._ptt_timed_out)
            if self._keyed or self._keying:
                return
            self._keying = True
            self._loop.create_task(self._key_after_check())
            return
        if self._ptt_timer is not None:
            self._ptt_timer.cancel()
            self._ptt_timer = None
        self._keying = False                 # cancels a key-down still waiting for its check
        if not self._keyed:
            return
        self._keyed = False
        self._tx_civ(civ.set_ptt(self.civ_address, False))
        self._tx_civ(civ.read_ptt(self.civ_address))
        src = self._tx_sources.get(self._tx_src)
        if src is not None:
            src.stop()
        log.info("PTT off (%s)", self._tx_kind)
        self._tx_kind = None
        self._tx_src = None
        self._emit("ptt", False)
        if reason:
            self._status(reason, "error")

    async def _key_after_check(self):
        a = self.civ_address
        r = await self._civ_ask(civ.read_data_mode(a), lambda k, v: k == "reply" and v == "data")
        if not self._keying:                 # unkeyed or disconnected meanwhile
            return
        self._keying = False
        dec = self._decoder
        if r is None or dec is None:
            why = "the radio did not confirm its DATA mode"
        else:
            why = self._rule_violation(dec.state.data, dec.state.mode, self._key_voice_ok)
            if why and not dec.state.data and not self._key_voice_ok:
                why = "the radio reports %s, not a DATA mode" % dec.state.mode_label
        if why:
            self._unkey()
            self._emit("ptt", False)
            self._status("PTT refused: " + why, "error")
            return
        self._tx_kind = "data" if dec.state.data else "voice"
        self._tx_src = self._source_kind(self._tx_kind)
        self._keyed = True
        self._tx_sources[self._tx_src].start()
        self._tx_civ(civ.set_ptt(a, True))
        self._tx_civ(civ.read_ptt(a))
        log.info("PTT on (%s)", self._tx_kind)
        self._emit("ptt", True)

    def _ptt_timed_out(self):
        self._ptt_timer = None
        self._unkey(reason="Unkeyed: PTT timeout (%.0f s)" % self.ptt_timeout)

    # ------------------------------------------------------------------ power
    def _set_power(self, state):
        if state == self.power:
            return
        self.power = state
        if self._audio is not None:
            self._audio.require_audio = state == "on"
        if state == "standby":
            self._unkey(reason="Unkeyed: the radio went to standby" if self._keyed else None)
            if self._tuning:
                self._end_tune("Tune stopped: the radio went to standby", "error")
        self._emit("power", state)
        if state == "standby":
            self._status("Radio is in standby (network up, radio off)")

    async def _power_is_on(self, radio_name, host):
        """Radio answers CI-V: read the remote power setting, switch DATA MOD etc."""
        self._mods_ready = None                     # setup running
        self._set_power("on")
        a = self.civ_address
        for f in (civ.read_mode(a), civ.read_data_mode(a), civ.read_if_width(a), civ.read_ptt(a),
                  civ.read_rf_power(a), civ.read_tuner(a)):
            self._tx_civ(f)
        item = civ.model_info(radio_name).get("remote_power")
        if item is not None:
            v = await self._read_menu(item)
            if v is not None and v != self.remote_power_setting:
                log.info("Power OFF Setting (for Remote Control): %s", civ.REMOTE_POWER_NAMES.get(v, v))
                self.remote_power_setting = v
                self._emit("remote_power_setting", v)
        await self._setup_mods(radio_name, host)
        self._mods_ready = True
        if self._powering_on:
            self._powering_on = False
            self._emit("powering_on", False)
            self._status("Radio powered on")

    async def _woke_up(self):
        if self.power != "standby" or self._mods_ready is None:
            return
        info = self.info
        await self._power_is_on(info.radio_name, self._spec.host)

    def power_off(self):
        """Thread-safe: unkey, put DATA MOD / DATA OFF MOD back, then switch the radio off."""
        if not self._connected or self.power != "on":
            raise NotConnected("the radio is not on")

        async def _do():
            self._stop_tune()
            self._unkey()
            await self._restore_data_mod()           # the radio may be used locally next
            self._ignore_civ_until = time.monotonic() + 2.0
            self._tx_civ(civ.power_off(self.civ_address))
            self._mods_ready = False
            self._set_power("standby")
            self._status("Radio switched off")
        asyncio.run_coroutine_threadsafe(_do(), self._loop)

    def power_on(self):
        """Thread-safe: wake a radio in standby."""
        if not self._connected:
            raise NotConnected("not connected to the radio's network server")
        if self.power == "on":
            return

        def _do():
            self._powering_on = True
            self._power_on_deadline = time.monotonic() + POWER_ON_TIMEOUT
            self._emit("powering_on", True)
            self._status("Switching the radio on …")
            self._tx_civ(civ.power_on(self.civ_address))
        self._loop.call_soon_threadsafe(_do)

    # ------------------------------------------------------------------ RF power and tuner
    def set_rf_power(self, percent):
        """Thread-safe: RF power 0-100 %."""
        self._require_on()
        level = int(round(max(0.0, min(100.0, float(percent))) * 255 / 100))
        self.rf_power = level
        self._rf_hold_until = time.monotonic() + RF_POWER_HOLD
        a = self.civ_address

        def _send():
            self._tx_civ(civ.set_rf_power(a, level))
            self._tx_civ(civ.read_rf_power(a))
        self._loop.call_soon_threadsafe(_send)
        return level

    def tune(self):
        """Thread-safe: run the internal antenna tuner's tune cycle (like the TUNER button)."""
        self._require_on()
        if not civ.has_tuner(self._model):
            raise PttRefused("TUNE: the %s has no antenna tuner command" % self._model)
        if self.ptt or self._keyed:
            raise PttRefused("TUNE refused: transmitting")
        if self._tuning:
            return
        self._tuning = True
        self._loop.call_soon_threadsafe(self._start_tune)

    def _start_tune(self):
        if self._tune_timer is not None:
            self._tune_timer.cancel()
        self._tune_timer = self._loop.call_later(TUNE_TIMEOUT, self._tune_timed_out)
        self._tune_fence = self._tuner_reads         # answers to earlier reads don't count
        self._tx_civ(civ.set_tuner(self.civ_address, civ.TUNER_TUNING))
        self._emit("tuning", True)
        self._status("Tuning …")

    def _on_tuner(self, value):
        self._tuner_replies += 1
        if self._tuning and self._tuner_replies <= self._tune_fence:
            return                               # stale: read was sent before the tune started
        self.tuner = value
        self._emit("tuner", value)
        if self._tuning and value != civ.TUNER_TUNING:
            # The radio answers 02 while tuning; anything else means the cycle ended.
            self._end_tune("Tune finished: tuner %s" % ("on" if value == civ.TUNER_ON else "off"),
                           "info" if value == civ.TUNER_ON else "error")

    def _tune_timed_out(self):
        self._tune_timer = None
        self._tx_civ(civ.set_tuner(self.civ_address, civ.TUNER_ON))      # stops the cycle
        self._end_tune("Tune stopped after %.0f s" % TUNE_TIMEOUT, "error")

    def _stop_tune(self):
        """Disconnect/teardown: abort a running tune cycle."""
        if self._tuning:
            self._tx_civ(civ.set_tuner(self.civ_address, civ.TUNER_ON))
            self._end_tune("Tune stopped: disconnecting", "error")

    def _end_tune(self, text, level):
        if self._tune_timer is not None:
            self._tune_timer.cancel()
            self._tune_timer = None
        if not self._tuning:
            return
        self._tuning = False
        self._emit("tuning", False)
        self._status(text, level)

    # ------------------------------------------------------------------ TX audio
    def set_tx_device(self, device_key, gain=1.0, kind="data"):
        """Select the data or voice transmit input (None = none). Opens it now if connected."""
        self._tx_devices[kind] = (device_key, gain) if device_key else None
        loop = self._loop
        if self._connected and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._reopen_tx_source, kind)

    def set_tx_gain(self, gain, kind="data"):
        dev = self._tx_devices.get(kind)
        if dev:
            self._tx_devices[kind] = (dev[0], gain)
        src = self._tx_sources.get(kind)
        if src is not None:
            src.set_gain(gain)

    def tx_level(self, kind="data"):
        src = self._tx_sources.get(kind)
        return src.peak if src is not None else None

    @property
    def _tx_source(self):                    # the data input (kept for older callers/tests)
        return self._tx_sources.get("data")

    def _source_kind(self, kind):
        """Which open source serves `kind`: voice falls back to the one input
        the GUI uses for both data and voice."""
        if kind in self._tx_sources:
            return kind
        if kind == "voice" and "data" in self._tx_sources:
            return "data"
        return None

    def _reopen_tx_source(self, kind):
        if self._keyed and self._tx_src == kind:
            self._unkey(reason="Unkeyed: transmit audio input changed")
        self._close_tx_source(kind)
        self._open_tx_source(kind)

    def _open_tx_sources(self):
        for kind in ("data", "voice"):
            self._open_tx_source(kind)

    def _open_tx_source(self, kind):
        dev = self._tx_devices.get(kind)
        if dev is None or kind in self._tx_sources:
            return
        key, gain = dev
        try:
            if self._tx_source_factory is not None:
                src = self._tx_source_factory(key, gain)
            else:
                from .txaudio import SoundCardSource
                src = SoundCardSource(key, gain)
            src.open(lambda pcm, k=kind: self._on_tx_frame(pcm, k))
        except Exception as e:
            log.exception("TX input open failed")
            self._status("%s transmit input failed: %s" % (kind.capitalize(), e), "error")
            self._emit("tx_source_failed", kind)
            return
        self._tx_sources[kind] = src

    def _close_tx_source(self, kind):
        src = self._tx_sources.pop(kind, None)
        if src is not None:
            try:
                src.close()
            except Exception:
                pass

    def _close_tx_sources(self):
        for kind in list(self._tx_sources):
            self._close_tx_source(kind)

    def _on_tx_frame(self, pcm, kind):
        """Called on the capture thread with one 20 ms frame."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._send_tx_frame, pcm, kind)
            except RuntimeError:
                pass

    def _send_tx_frame(self, pcm, kind):
        if self._keyed and kind == self._tx_src and self._audio is not None and not self._audio.closed:
            self._audio.send_frame(pcm)

    # ------------------------------------------------------------------ DATA MOD / DATA OFF MOD
    @property
    def _model(self):
        return self.info.radio_name if self.info else None

    @property
    def _lan(self):
        return civ.lan_value(self._model)

    def _mod_name(self, v):
        return civ.mod_name(self._model, v)

    def _radio_key(self, radio_name, host):
        return "%s@%s" % (radio_name, host)

    async def _setup_mods(self, radio_name, host):
        """Switch DATA MOD and DATA OFF MOD to LAN, remembering originals on disk first."""
        key = self._radio_key(radio_name, host)
        info = civ.model_info(radio_name)
        for setting, (label, item_key) in MOD_SETTINGS.items():
            await self._setup_mod(setting, label, info.get(item_key), radio_name, key)
        self._emit_mods()
        lvl_item = info.get("lan_mod_level")
        if lvl_item is not None:
            lvl = await self._read_menu(lvl_item)
            if lvl == 0:
                self._status("LAN MOD Level is 0 %: transmit audio will be silent", "error")

    async def _setup_mod(self, setting, label, item, radio_name, key):
        m = self.mods[setting]
        m.update(value=None, original=None, item=item, key=key)
        if item is None:
            self._status("%s switching is not known for %s: set %s to LAN on the radio "
                         "for transmit audio" % (label, radio_name, label), "error")
            return
        cur = await self._read_menu(item)
        if cur is None:
            self._status("Could not read %s from the radio; transmit may use the wrong input" % label,
                         "error")
            return
        m["value"] = cur
        pending = self._store.get(key, setting)
        if pending is not None and pending != cur:
            self._status("A previous session left %s changed; it will be restored to %s"
                         % (label, self._mod_name(pending)))
        original = pending if pending is not None else cur
        if m["manage"] and cur != self._lan:
            self._store.put(key, setting, original)          # on disk before touching the radio
            m["original"] = original
            if await self._write_menu(item, self._lan):
                m["value"] = self._lan
                self._status("%s switched from %s to LAN (restored when you disconnect)"
                             % (label, self._mod_name(original)))
            else:
                self._status("Could not switch %s to LAN" % label, "error")
        elif original != self._lan:
            m["original"] = original                         # pending from an earlier session
        elif pending is not None:
            self._store.clear(key, setting)                  # original was LAN: nothing to do

    def _emit_mods(self):
        out = {}
        for setting, (label, _) in MOD_SETTINGS.items():
            m = self.mods[setting]
            out[setting] = {"label": label, "value": m["value"], "original": m["original"],
                            "original_name": self._mod_name(m["original"]) if m["original"] is not None else None,
                            "name": self._mod_name(m["value"]) if m["value"] is not None else None}
        self._emit("data_mod", out)

    async def _restore_mods(self):
        ok = True
        for setting, (label, _) in MOD_SETTINGS.items():
            m = self.mods[setting]
            orig = m["original"]
            if orig is None:
                continue
            if await self._write_menu(m["item"], orig):
                self._store.clear(m["key"], setting)
                m["original"] = None
                m["value"] = orig
                self._status("%s restored to %s" % (label, self._mod_name(orig)))
            else:
                ok = False
                self._status("Could not restore %s to %s; it will be restored at the next connection"
                             % (label, self._mod_name(orig)), "error")
        self._emit_mods()
        return ok

    # name kept for callers and tests
    async def _restore_data_mod(self):
        return await self._restore_mods()

    def restore_data_mod_now(self):
        """Thread-safe: put DATA MOD / DATA OFF MOD back now (transmit is then
        refused until reconnect)."""
        loop = self._loop
        if not self._connected or loop is None:
            raise NotConnected("not connected to the radio")

        async def _do():
            if self._keyed:
                self._unkey(reason="Unkeyed: restoring DATA MOD")
            await self._restore_data_mod()
        asyncio.run_coroutine_threadsafe(_do(), loop)

    def pending_restores(self):
        return self._store.pending()

    # ------------------------------------------------------------------ audio
    def _on_audio(self, pcm):
        samples = np.frombuffer(pcm, dtype="<i2")
        with self._sink_lock:
            for name, sink in list(self._sinks.items()):
                try:
                    sink.write(samples)
                except Exception as e:
                    log.exception("sink %s failed", name)
                    self._drop_sink(name, "Audio output '%s' stopped: %s" % (name, e))

    def _sink_set_rate(self, name, sink):
        try:
            sink.set_input_rate(SAMPLE_RATE)
        except Exception as e:
            log.exception("sink %s open failed", name)
            self._drop_sink(name, "Audio output '%s' failed: %s" % (name, e))

    def _drop_sink(self, name, message):
        sink = self._sinks.pop(name, None)
        if sink is not None:
            try:
                sink.close()
            except Exception:
                pass
        self._status(message, "error")
        self._emit("sink_failed", name)

    def _install_sink(self, name, sink):
        with self._sink_lock:
            old = self._sinks.pop(name, None)
            if old is not None:
                old.close()
            if sink is not None:
                self._sinks[name] = sink
                if self._connected:
                    self._sink_set_rate(name, sink)
        return name in self._sinks if sink is not None else True

    def set_audio_device(self, device_key, gain=1.0):
        """device_key None -> stop sound-card output."""
        sink = SoundCardSink(device_key, gain) if device_key else None
        return self._install_sink("card", sink)

    def start_recording(self, path):
        """Record received audio (.wav, .m4a or .mp4). Waits for audio if not connected."""
        ok = self._install_sink("file", make_file_sink(path))
        if ok:
            self._emit("recording", {"active": True, "path": path})
        return ok

    def stop_recording(self):
        with self._sink_lock:
            sink = self._sinks.pop("file", None)
        if sink is None:
            return None
        sink.close()
        info = {"active": False, "path": sink.current_path or sink.path, "seconds": sink.seconds()}
        self._emit("recording", info)
        return info

    def recording_seconds(self):
        sink = self._sinks.get("file")
        return sink.seconds() if sink else None

    def audio_stats(self):
        sink = self._sinks.get("card")
        return sink.stats() if sink else {}
