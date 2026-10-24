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
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from . import civ
from .rsba1.stream import StreamError
from .rsba1.streams import AudioStream, AuthError, ControlStream, SerialStream
from .sinks import SoundCardSink, make_file_sink
from .timerres import high_resolution_timer

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
FAST_RETRIES = 5
FAST_RETRY_DELAY = 1.0
SLOW_RETRY_DELAY = 65.0     # the IC-705 drops audio if you re-login too often
POLL_INTERVAL = 0.5
STATS_INTERVAL = 1.0


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


class RadioSession:
    def __init__(self, on_event=None):
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
        self.meters = {}
        self._sink_lock = threading.Lock()
        self._sinks = {}
        self._connected_at = None

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

    def disconnect(self, timeout=4.0):
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

            for f in (civ.read_freq(addr), civ.read_mode(addr), civ.read_data_mode(addr)):
                serial.send_civ(f)
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
            if kind in ("freq", "mode"):
                if dec.state != self.rig:
                    self.rig = dec.state
                    self._emit("rig", self.rig)
            elif kind in ("smeter", "po", "swr", "rfpower"):
                self.meters[kind] = value
                self._emit("meter", (kind, value))
            elif kind == "foreign":
                log.debug("CI-V frame from unexpected address %02Xh", value)
            elif kind == "ack" and not value:
                log.info("radio answered NG to a CI-V command")

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
