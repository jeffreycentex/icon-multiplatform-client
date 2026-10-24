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

from . import civ
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
POLL_INTERVAL = 0.5
STATS_INTERVAL = 1.0
DEFAULT_PTT_TIMEOUT = 180.0
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
        # transmit
        self.ptt = False                 # what we commanded
        self.tx = False                  # what the radio reports (1C 00)
        self.ptt_timeout = DEFAULT_PTT_TIMEOUT
        self._keyed = False              # loop-thread view of self.ptt
        self._ptt_owner = None
        self._ptt_timer = None
        self._tx_device = None           # (key, gain)
        self._tx_source = None
        self._tx_source_factory = None   # tests inject a fake source
        # DATA MOD management
        self.manage_data_mod = True
        self.data_mod = None             # current value as last read, None = unknown
        self._data_mod_original = None   # value to put back, None = nothing to restore
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
        self._close_tx_source()
        self.stop_recording()
        self._install_sink("card", None)

    def _thread_main(self, ready):
        with high_resolution_timer():
            asyncio.run(self._main(ready))

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
                      civ.read_if_width(addr), civ.read_ptt(addr)):
                serial.send_civ(f)
            await self._setup_data_mod(info.radio_name, spec.host)
            self._open_tx_source()
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
                    self._ptt_apply(False, reason=None)
                    await self._restore_data_mod()
                except Exception:
                    log.exception("cleanup before logout failed")
            else:
                self._ptt_apply(False, reason=None)
            self._close_tx_source()
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
                s.send_civ(civ.read_smeter(addr))
                n += 1
                if n % 4 == 0:            # the radio also pushes changes (CI-V transceive)
                    s.send_civ(civ.read_freq(addr))
                    s.send_civ(civ.read_mode(addr))
                    s.send_civ(civ.read_data_mode(addr))
                    if self.rig.mode != "FM":
                        s.send_civ(civ.read_if_width(addr))
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
        for kind, value in dec.feed(data):
            for match, fut in list(self._civ_waiters):
                if not fut.done() and match(kind, value):
                    fut.set_result((kind, value))
            if kind in ("freq", "mode"):
                with self._rig_lock:
                    changed = dec.state != self.rig
                    self.rig = dec.state
                if changed:
                    self._emit("rig", dec.state)
                if self._keyed and dec.state.mode and not dec.state.data:
                    self._ptt_apply(False, reason="Unkeyed: the radio left DATA mode (now %s)"
                                    % dec.state.mode_label)
            elif kind == "ptt":
                tx = value != 0
                if tx != self.tx:
                    self.tx = tx
                    self._emit("tx", tx)
            elif kind in ("smeter", "po", "swr", "rfpower"):
                self.meters[kind] = value
                self._emit("meter", (kind, value))
            elif kind == "foreign":
                log.debug("CI-V frame from unexpected address %02Xh", value)
            elif kind == "ack" and not value:
                log.info("radio answered NG to a CI-V command")

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
                self._serial.send_civ(frame)
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                continue
            finally:
                if entry in self._civ_waiters:
                    self._civ_waiters.remove(entry)
        return None

    async def _read_menu(self, item):
        r = await self._civ_ask(civ.read_menu(self.civ_address, item),
                                lambda k, v: (k == "menu" and v[0] == item) or (k == "ack" and v is False))
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
            if self._serial is not None:
                self._serial.send_civ(frame)
        loop.call_soon_threadsafe(_send)
        return True

    # ------------------------------------------------------------------ control API
    # Thread-safe. Each setter updates self.rig at once (so a rigctl 'f' right
    # after 'F' sees the new value), queues the CI-V command and a read-back;
    # the radio's answer then confirms or corrects the state.

    def _apply(self, new_state, frames):
        if not self._connected:
            raise NotConnected("not connected to the radio")
        with self._rig_lock:
            self.rig = new_state
        self._emit("rig", new_state)

        def _send():
            if self._decoder is not None:
                self._decoder.state = new_state
            if self._serial is not None:
                for f in frames:
                    self._serial.send_civ(f)
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
        if self.ptt and not data:
            self.set_ptt(False, reason="Unkeyed: switching to %s (not a data mode)" % civ_mode)
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
    # Safety rules: PTT only in a DATA mode (USB-D etc.), only with a TX audio
    # input selected and DATA MOD on LAN; unkeyed after ptt_timeout seconds, on
    # leaving DATA mode, on disconnect/shutdown, when the TX input goes away,
    # and (via rigctld) when the client that keyed disconnects.

    def ptt_refusal(self):
        """Why PTT can't be keyed now, or None if it can."""
        if not self._connected:
            return "not connected to the radio"
        if not self.rig.mode:
            return "the radio's mode is not known yet"
        if not self.rig.data:
            return ("transmit is only allowed in data modes (USB-D, LSB-D, AM-D, FM-D); "
                    "the radio is in %s" % self.rig.mode_label)
        if self._tx_source is None:
            return "no transmit audio input selected"
        if self.data_mod is not None and self.data_mod != civ.DATA_MOD_LAN:
            return "DATA MOD is %s, not LAN" % civ.DATA_MOD_NAMES.get(self.data_mod, self.data_mod)
        return None

    def set_ptt(self, on, owner=None, reason=None):
        """Thread-safe. Raises PttRefused when keying isn't allowed."""
        if on:
            why = self.ptt_refusal()
            if why:
                raise PttRefused("PTT refused: " + why)
        elif not self.ptt:
            return
        self.ptt = bool(on)
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._ptt_apply, bool(on), owner, reason)

    def release_ptt(self, owner):
        """Unkey if `owner` keyed (rigctld calls this when a client disconnects)."""
        if self.ptt and self._ptt_owner is owner:
            self.set_ptt(False, reason="Unkeyed: the rigctl client that keyed PTT disconnected")

    def _ptt_apply(self, on, owner=None, reason=None):
        """Loop thread (or teardown). Sends the CI-V commands and starts/stops TX audio."""
        a = self.civ_address
        if on:
            self._ptt_owner = owner
            if self._ptt_timer is not None:
                self._ptt_timer.cancel()
            self._ptt_timer = self._loop.call_later(self.ptt_timeout, self._ptt_timed_out)
            if self._keyed:
                return
            self._keyed = True
            if self._tx_source is not None:
                self._tx_source.start()
            if self._serial is not None:
                self._serial.send_civ(civ.set_ptt(a, True))
                self._serial.send_civ(civ.read_ptt(a))
            log.info("PTT on")
            self._emit("ptt", True)
            return
        if self._ptt_timer is not None:
            self._ptt_timer.cancel()
            self._ptt_timer = None
        self._ptt_owner = None
        self.ptt = False
        if not self._keyed:
            return
        self._keyed = False
        if self._serial is not None and not self._serial.closed:
            self._serial.send_civ(civ.set_ptt(a, False))
            self._serial.send_civ(civ.read_ptt(a))
        if self._tx_source is not None:
            self._tx_source.stop()
        log.info("PTT off")
        self._emit("ptt", False)
        if reason:
            self._status(reason, "error")

    def _ptt_timed_out(self):
        self._ptt_timer = None
        self._ptt_apply(False, reason="Unkeyed: PTT timeout (%.0f s)" % self.ptt_timeout)

    # ------------------------------------------------------------------ TX audio
    def set_tx_device(self, device_key, gain=1.0):
        """Select the transmit audio input (None = none). Opens it now if connected."""
        self._tx_device = (device_key, gain) if device_key else None
        loop = self._loop
        if self._connected and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._reopen_tx_source)

    def set_tx_gain(self, gain):
        if self._tx_device:
            self._tx_device = (self._tx_device[0], gain)
        src = self._tx_source
        if src is not None:
            src.set_gain(gain)

    def tx_level(self):
        src = self._tx_source
        return src.peak if src is not None else None

    def _reopen_tx_source(self):
        if self._keyed:
            self._ptt_apply(False, reason="Unkeyed: transmit audio input changed")
        self._close_tx_source()
        self._open_tx_source()

    def _open_tx_source(self):
        if self._tx_device is None or self._tx_source is not None:
            return
        key, gain = self._tx_device
        try:
            if self._tx_source_factory is not None:
                src = self._tx_source_factory(key, gain)
            else:
                from .txaudio import SoundCardSource
                src = SoundCardSource(key, gain)
            src.open(self._on_tx_frame)
        except Exception as e:
            log.exception("TX input open failed")
            self._status("Transmit audio input failed: %s" % e, "error")
            self._emit("tx_source_failed", str(e))
            return
        self._tx_source = src

    def _close_tx_source(self):
        src, self._tx_source = self._tx_source, None
        if src is not None:
            try:
                src.close()
            except Exception:
                pass

    def _on_tx_frame(self, pcm):
        """Called on the capture thread with one 20 ms frame."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._send_tx_frame, pcm)
            except RuntimeError:
                pass

    def _send_tx_frame(self, pcm):
        if self._keyed and self._audio is not None and not self._audio.closed:
            self._audio.send_frame(pcm)

    # ------------------------------------------------------------------ DATA MOD
    def _radio_key(self, radio_name, host):
        return "%s@%s" % (radio_name, host)

    async def _setup_data_mod(self, radio_name, host):
        """Switch DATA MOD to LAN, remembering the original on disk first."""
        self.data_mod = None
        self._data_mod_original = None
        item = civ.DATA_MOD_ITEM.get(radio_name)
        if item is None:
            self._status("DATA MOD switching is not known for %s: set DATA MOD to LAN on the radio "
                         "for transmit audio" % radio_name, "error")
            return
        key = self._radio_key(radio_name, host)
        cur = await self._read_menu(item)
        if cur is None:
            self._status("Could not read DATA MOD from the radio; transmit may use the wrong input", "error")
            return
        self.data_mod = cur
        pending = self._store.get(key, "data_mod")
        if pending is not None and pending != cur:
            self._status("A previous session left DATA MOD changed; it will be restored to %s"
                         % civ.DATA_MOD_NAMES.get(pending, pending))
        original = pending if pending is not None else cur
        if self.manage_data_mod and cur != civ.DATA_MOD_LAN:
            self._store.put(key, "data_mod", original)          # on disk before touching the radio
            self._data_mod_original = original
            if await self._write_menu(item, civ.DATA_MOD_LAN):
                self.data_mod = civ.DATA_MOD_LAN
                self._status("DATA MOD switched from %s to LAN (restored when you disconnect)"
                             % civ.DATA_MOD_NAMES.get(original, original))
            else:
                self._status("Could not switch DATA MOD to LAN", "error")
        elif original != civ.DATA_MOD_LAN:
            self._data_mod_original = original                  # pending from an earlier session
        elif pending is not None:
            self._store.clear(key, "data_mod")                  # original was LAN: nothing to do
        self._data_mod_key = (item, key)
        self._emit_data_mod()
        lvl_item = civ.LAN_MOD_LEVEL_ITEM.get(radio_name)
        if lvl_item is not None:
            lvl = await self._read_menu(lvl_item)
            if lvl == 0:
                self._status("LAN MOD Level is 0 %: transmit audio will be silent", "error")

    def _emit_data_mod(self):
        self._emit("data_mod", {"value": self.data_mod, "original": self._data_mod_original,
                                "name": civ.DATA_MOD_NAMES.get(self.data_mod, "?") if self.data_mod is not None
                                else None})

    async def _restore_data_mod(self):
        if self._data_mod_original is None:
            return True
        item, key = self._data_mod_key
        orig = self._data_mod_original
        if await self._write_menu(item, orig):
            self._store.clear(key, "data_mod")
            self._data_mod_original = None
            self.data_mod = orig
            self._status("DATA MOD restored to %s" % civ.DATA_MOD_NAMES.get(orig, orig))
            self._emit_data_mod()
            return True
        self._status("Could not restore DATA MOD to %s; it will be restored at the next connection"
                     % civ.DATA_MOD_NAMES.get(orig, orig), "error")
        return False

    def restore_data_mod_now(self):
        """Thread-safe: put DATA MOD back now (transmit is then refused until reconnect)."""
        loop = self._loop
        if not self._connected or loop is None:
            raise NotConnected("not connected to the radio")

        async def _do():
            if self._keyed:
                self._ptt_apply(False, reason="Unkeyed: restoring DATA MOD")
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
        return self._install_sink("file", make_file_sink(path))

    def stop_recording(self):
        with self._sink_lock:
            sink = self._sinks.pop("file", None)
        if sink is None:
            return None
        sink.close()
        return {"path": sink.current_path or sink.path, "seconds": sink.seconds()}

    def audio_stats(self):
        sink = self._sinks.get("card")
        return sink.stats() if sink else {}
