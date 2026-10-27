"""Qt-free application service for the remote-control API: tuning, recording
and status on top of RadioSession. The GUI records through this class too, so
whatever the API does the window shows, and the other way round.

Adapted from kiwisdr-remote's KiwiService. Connecting stays in the GUI (the
API has no /connect here): the radio's address and login are the owner's.

Control only: no PTT endpoint, and nothing here restricts transmitting from
the GUI or rigctld. The one interaction is the other way round: /tune is
refused while the radio is keyed or tuning (checked and applied under the
session's PTT lock, held only for that moment, so a key-down can't slip in
between).
"""

import logging
import os
import re
import threading
import time

from . import __version__, civ
from .session import NotConnected, RadioSession
from .state import rig_mode_from_hamlib, ui_label

log = logging.getLogger(__name__)

TICK_SECONDS = 0.5
API_MODES = ("USB", "LSB", "AM", "FM", "PKTUSB", "PKTLSB", "PKTAM", "PKTFM",
             "USB-D", "LSB-D", "AM-D", "FM-D", "SAM")
DEFAULT_NAME = "icom_{date}T{time}_{freq_khz}kHz_{mode}"
S9_DBM = -73.0                     # HF S9; the S-meter reading is relative to it


class ApiError(Exception):
    """Error with a stable machine-readable code (used by the HTTP API)."""

    def __init__(self, code, message, http_status=400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


_SAFE = re.compile(r"[^A-Za-z0-9._+-]+")


def _safe(text):
    return _SAFE.sub("_", str(text)).strip("._") or "x"


def default_record_dir():
    return os.path.join(os.path.expanduser("~"), "Music", "IcomRemote")


class IcomService:
    def __init__(self, on_event=None, state_dir=None, record_dir=None, clock=time.monotonic,
                 start_ticker=True):
        self.radio = RadioSession(on_event=on_event, state_dir=state_dir)
        self.record_dir = record_dir or default_record_dir()
        self.plain_ssb_is_data = True      # same rule as rigctl: plain USB/LSB -> USB-D/LSB-D
        self._clock = clock
        self._lock = threading.RLock()
        self._rec_deadline = None
        self._rec_max = None
        self._stop = threading.Event()
        self._ticker = None
        if start_ticker:
            self._ticker = threading.Thread(target=self._tick_loop, name="icom-service", daemon=True)
            self._ticker.start()

    # ------------------------------------------------------------------ tuning
    def tune(self, freq_hz=None, mode=None, passband=None):
        """Set frequency and/or mode. passband is accepted and ignored: the
        radio keeps its selected filter (FIL1/2/3), as with rigctl."""
        r = self.radio
        want = None
        if mode is not None:
            want = rig_mode_from_hamlib(mode, self.plain_ssb_is_data)
            if want is None:
                raise ApiError("bad_request", "unsupported mode %r (use %s)" % (mode, ", ".join(API_MODES)))
        if freq_hz is not None:
            lo, hi = r.freq_limits
            if not lo <= freq_hz <= hi:
                raise ApiError("bad_request", "%.3f kHz is outside %.0f-%.0f kHz"
                               % (freq_hz / 1e3, lo / 1e3, hi / 1e3))
        with r._ptt_lock:                    # no key-down between this check and the change
            if not r.connected:
                raise ApiError("not_connected", "not connected to the radio (connect in the Icom Remote "
                                                "window)", 409)
            if r.power != "on":
                raise ApiError("not_connected", "the radio is not powered on", 409)
            if r.ptt or r.tx or r._keyed or r._keying:
                raise ApiError("transmitting", "the radio is transmitting", 409)
            if r._tuning or r.tuner == civ.TUNER_TUNING:
                raise ApiError("transmitting", "the antenna tuner is tuning", 409)
            try:
                if want is not None and (r.rig.mode, r.rig.data) != want:
                    r.set_mode(want[0], want[1])
                if freq_hz is not None:
                    r.set_frequency(freq_hz)
            except NotConnected as e:
                raise ApiError("not_connected", str(e), 409)
            except ValueError as e:
                raise ApiError("bad_request", str(e))
        return self.tune_info()

    def tune_info(self):
        st = self.radio.rig
        if not st.mode:
            return {"freq_hz": st.freq_hz or None, "mode": None, "data": None, "filter": None,
                    "passband": None}
        return {"freq_hz": st.freq_hz, "mode": ui_label(st.mode, st.data), "data": st.data,
                "filter": st.filter, "passband": st.width_hz or civ.nominal_width_hz(st.mode, st.filter)}

    # ------------------------------------------------------------------ recording
    def recording_path_for(self, path=None, directory=None, name=None, ext=".wav", formats=(".wav",)):
        if path:
            path = os.path.expanduser(path)
            root, e = os.path.splitext(path)
            if e.lower() not in formats:
                if e:
                    raise ApiError("bad_request", "only %s recordings are supported" % "/".join(formats))
                path = root + ext
        else:
            directory = os.path.expanduser(directory or self.record_dir)
            st = self.radio.rig
            spec = self.radio.spec
            now = time.gmtime()
            fields = {
                "date": time.strftime("%Y%m%d", now),
                "time": time.strftime("%H%M%SZ", now),
                "freq_khz": "%.3f" % (st.freq_hz / 1000.0),
                "mode": ui_label(st.mode, st.data) if st.mode else "rx",
                "radio": self.radio.info.radio_name if self.radio.info else "radio",
                "server": spec.host if spec else "none",
            }
            try:
                base = (name or DEFAULT_NAME).format(**fields)
            except (KeyError, IndexError, ValueError) as e:
                raise ApiError("bad_request", "bad name template (fields: %s): %s"
                               % (", ".join(sorted(fields)), e))
            root, e = os.path.splitext(base)
            if e.lower() in formats:
                base, ext = root, e.lower()
            path = os.path.join(directory, _safe(base) + ext)
        root, e = os.path.splitext(path)
        n = 1
        while os.path.exists(path):            # never overwrite an earlier recording
            path = "%s_%d%s" % (root, n, e)
            n += 1
        return path

    def start_recording(self, path=None, directory=None, name=None, max_seconds=None,
                        ext=".wav", formats=(".wav",)):
        """The API records WAV only; the GUI passes formats to also allow .m4a/.mp4."""
        if max_seconds is not None and (isinstance(max_seconds, bool) or
                                        not isinstance(max_seconds, (int, float)) or max_seconds <= 0):
            raise ApiError("bad_request", "max_seconds must be a number > 0")
        with self._lock:
            if self.radio.recording_path() is not None:
                raise ApiError("conflict", "already recording to %s" % self.radio.recording_path(), 409)
            path = self.recording_path_for(path, directory, name, ext, formats)
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                ok = self.radio.start_recording(path)
            except (OSError, RuntimeError, ValueError) as e:
                raise ApiError("io_error", "cannot record to %s: %s" % (path, e), 500)
            if not ok:
                raise ApiError("io_error", "cannot record to %s" % path, 500)
            self._rec_max = float(max_seconds) if max_seconds else None
            self._rec_deadline = self._clock() + self._rec_max if self._rec_max else None
        log.info("recording started: %s%s", path,
                 " (stops after %.0f s)" % self._rec_max if self._rec_max else "")
        return self.recording_info()

    def stop_recording(self, reason="requested"):
        with self._lock:
            self._rec_deadline = self._rec_max = None
            info = self.radio.stop_recording()
        if info is not None:
            info["seconds"] = round(info["seconds"], 1)
            info["gap_seconds"] = round(info.get("gap_seconds", 0.0), 1)
            info["reason"] = reason
            log.info("recording stopped (%s): %s, %.1f s", reason, info["path"], info["seconds"])
        return info

    def recording_info(self):
        r = self.radio
        path = r.recording_path()
        if path is None:
            return {"active": False}
        d = {"active": True, "path": path,
             "seconds": round(r.recording_seconds() or 0.0, 1),
             "gap_seconds": round(r.gap_seconds, 1),
             "waiting_for_audio": not (r.connected and r.power == "on")}
        if self._rec_deadline is not None:
            d["max_seconds"] = self._rec_max
            d["remaining_s"] = max(0, round(self._rec_deadline - self._clock()))
        return d

    # ------------------------------------------------------------------ status
    def state(self):
        r = self.radio
        if r.connected:
            return "connected"
        if r.running:
            return "reconnecting" if r.was_connected else "connecting"
        if r.failed:
            return "failed"
        return "idle"

    def status(self):
        r = self.radio
        on = r.connected and r.power == "on"
        raw = r.meters.get("smeter")
        receiving = on and raw is not None and not (r.tx or r._keyed)
        return {
            "app_version": __version__,
            "state": self.state(),
            "connected": r.connected,
            "server": r.spec.label() if r.spec and r.running else None,
            "last_error": r.last_error,
            "radio": ({"name": r.info.radio_name, "civ_address": "%02Xh" % r.civ_address}
                      if r.connected and r.info else None),
            "power": r.power if r.connected else None,
            "tune": self.tune_info() if on else None,
            "s_meter_dbm": round(civ.s_meter_db(raw) + S9_DBM, 1) if receiving else None,
            "s_meter": civ.s_meter_text(civ.s_meter_db(raw)) if receiving else None,
            "transmitting": bool(r.connected and (r.tx or r._keyed)),
            "session": None,
            "recording": self.recording_info(),
            "audio_output": "card" in r._sinks,
        }

    # ------------------------------------------------------------------ timers
    def _tick_loop(self):
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                log.exception("service tick failed")

    def tick(self):
        """Stop a recording that reached its max_seconds."""
        with self._lock:
            due = self._rec_deadline is not None and self._clock() >= self._rec_deadline
        if due:
            info = self.stop_recording(reason="max_seconds")
            self.radio._status("Recording reached its maximum length%s"
                               % (": %s" % os.path.basename(info["path"]) if info else ""))

    # ------------------------------------------------------------------ lifecycle
    def shutdown(self):
        self._stop.set()
        self.stop_recording(reason="shutdown")
        self.radio.shutdown()
        if self._ticker is not None:
            self._ticker.join(2.0)
