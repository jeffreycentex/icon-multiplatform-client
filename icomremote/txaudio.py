"""Transmit audio: capture from an input device, deliver 20 ms frames of
48 kHz 16-bit mono PCM for the radio.

TxFramer        resample + gain + soft limit + cut into 960-sample frames (no I/O)
SoundCardSource PortAudio input stream feeding a TxFramer

No clock-drift controller on purpose: the radio buffers 300 ms of TX audio and
every transmission is capped by the PTT timeout, so even 100 ppm of clock
mismatch over 3 minutes is under 20 ms. Frames go out as they are captured,
and the buffer restarts at every key-down.

Frames are produced on the PortAudio thread; the session hands them to its
event loop. Capture runs whenever a device is selected, but frames are only
delivered while `active` is set (between PTT on and off).
"""

import logging
import os
import sys

import numpy as np

from .resample import StreamResampler
from .sinks import HAVE_SOUNDDEVICE, sd, sounddevice_error

log = logging.getLogger(__name__)

RATE = 48000
FRAME = 960                     # 20 ms
LIMIT_KNEE = 0.9


class TxFramer:
    def __init__(self, in_rate, gain=1.0, on_frame=None):
        self.in_rate = float(in_rate)
        self.gain = float(gain)
        self.on_frame = on_frame or (lambda pcm: None)
        self._resampler = None if abs(self.in_rate - RATE) < 1e-6 else StreamResampler(self.in_rate, RATE)
        self._buf = np.zeros(0, dtype=np.float32)
        self.active = False
        self.peak = 0.0                 # recent peak, 0..1 (for a level display)
        self.frames_sent = 0

    def start(self):
        """Key-down: drop anything captured before now and start delivering."""
        self._buf = np.zeros(0, dtype=np.float32)
        self.active = True

    def stop(self):
        self.active = False
        self._buf = np.zeros(0, dtype=np.float32)

    def push(self, mono):
        """mono: float32 samples at in_rate (-1..1)."""
        x = np.asarray(mono, dtype=np.float32) * self.gain
        if x.size:
            self.peak = max(self.peak * 0.8, float(np.max(np.abs(x))))
        if not self.active:
            return
        if self._resampler is not None:
            x = self._resampler.process(x)
        self._buf = np.concatenate((self._buf, x))
        while len(self._buf) >= FRAME:
            f, self._buf = self._buf[:FRAME], self._buf[FRAME:]
            a = np.abs(f)
            over = a > LIMIT_KNEE
            if over.any():                  # soft knee instead of hard clipping
                head = 1.0 - LIMIT_KNEE
                f = f.copy()
                f[over] = np.sign(f[over]) * (LIMIT_KNEE + head * np.tanh((a[over] - LIMIT_KNEE) / head))
            pcm = np.clip(np.round(f * 32767.0), -32768, 32767).astype("<i2").tobytes()
            self.frames_sent += 1
            self.on_frame(pcm)


# ---------------------------------------------------------------- input devices

PULSE_SOURCE_PREFIX = "pulse-source:"


def list_pulse_sources():
    """Linux only: PulseAudio/PipeWire sources, including 'X.monitor' sources."""
    import shutil
    import subprocess
    if not sys.platform.startswith("linux") or not shutil.which("pactl"):
        return []
    try:
        res = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return []
    return [c[1] for c in (line.split("\t") for line in res.stdout.splitlines()) if len(c) > 1]


def _pulse_input_index():
    if not HAVE_SOUNDDEVICE:
        return None
    for want in ("pulse", "pipewire", "default"):
        for idx, dev in enumerate(sd.query_devices()):
            if dev["name"] == want and dev["max_input_channels"] > 0:
                return idx
    return None


def list_input_devices():
    """[(key, label)] for input-capable devices; key = 'name|host api' (stable across runs)."""
    if not HAVE_SOUNDDEVICE:
        return []
    out = []
    if _pulse_input_index() is not None:
        for name in list_pulse_sources():
            out.append((PULSE_SOURCE_PREFIX + name, "PulseAudio/PipeWire source: %s" % name))
    apis = sd.query_hostapis()
    for dev in sd.query_devices():
        if dev["max_input_channels"] < 1:
            continue
        api = apis[dev["hostapi"]]["name"]
        out.append(("%s|%s" % (dev["name"], api), "%s  [%s]" % (dev["name"], api)))
    return out


def resolve_input_device(key):
    if not HAVE_SOUNDDEVICE or not key:
        return None
    if key.startswith(PULSE_SOURCE_PREFIX):
        return _pulse_input_index()
    name, _, api = key.partition("|")
    apis = sd.query_hostapis()
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and dev["name"] == name and apis[dev["hostapi"]]["name"] == api:
            return idx
    return None


def guess_tx_cable(devices):
    """Most likely 'output side of a virtual cable' among input devices."""
    needles = ("cable output", "vb-audio", "blackhole", "icomtx", "loopback", "monitor", "virtual")
    for key, label in devices:
        if any(n in label.lower() for n in needles):
            return key
    return None


class SoundCardSource:
    """Captures from an input device into a TxFramer."""

    BLOCK_SECONDS = 0.02

    def __init__(self, device_key, gain=1.0):
        if not HAVE_SOUNDDEVICE:
            raise RuntimeError("sounddevice/PortAudio not available: %s" % sounddevice_error())
        self.device_key = device_key
        self.gain = float(gain)
        self.framer = None
        self._stream = None
        self.overflows = 0

    def open(self, on_frame):
        idx = resolve_input_device(self.device_key)
        if idx is None:
            raise RuntimeError("audio input not found: %s" % self.device_key.split("|")[0])
        if self.device_key.startswith(PULSE_SOURCE_PREFIX):
            os.environ["PULSE_SOURCE"] = self.device_key[len(PULSE_SOURCE_PREFIX):]
        info = sd.query_devices(idx)
        rate = int(info["default_samplerate"]) or RATE
        channels = max(1, min(2, int(info["max_input_channels"])))
        self.framer = TxFramer(rate, self.gain, on_frame)
        self._stream = sd.InputStream(device=idx, samplerate=rate, channels=channels, dtype="float32",
                                      blocksize=int(rate * self.BLOCK_SECONDS),
                                      latency=max(0.05, float(info.get("default_low_input_latency", 0.0))),
                                      callback=self._callback)
        self._stream.start()
        log.info("TX audio input '%s' opened at %d Hz, %d ch", info["name"], rate, channels)

    def _callback(self, indata, frames, time_info, status):
        if status and status.input_overflow:
            self.overflows += 1
        mono = indata[:, 0] if indata.shape[1] == 1 else indata.mean(axis=1)
        self.framer.push(mono)

    def set_gain(self, gain):
        self.gain = float(gain)
        if self.framer is not None:
            self.framer.gain = self.gain

    def start(self):
        if self.framer is not None:
            self.framer.start()

    def stop(self):
        if self.framer is not None:
            self.framer.stop()

    @property
    def peak(self):
        return self.framer.peak if self.framer is not None else 0.0

    def close(self):
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception as e:
                log.debug("closing TX input: %s", e)
