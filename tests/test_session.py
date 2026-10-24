"""End-to-end: RadioSession against tests/fake_rsba1.py over real UDP sockets."""

import os
import sys
import tempfile
import time
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_rsba1 import FakeRadio  # noqa: E402
from icomremote.session import ConnectionSpec, RadioSession  # noqa: E402


def wait_for(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class _Events:
    def __init__(self):
        self.items = []

    def __call__(self, kind, payload):
        self.items.append((kind, payload))

    def kinds(self, kind):
        return [p for k, p in self.items if k == kind]


class TestSession(unittest.TestCase):
    def setUp(self):
        self.radio = None
        self.session = None

    def tearDown(self):
        if self.session is not None:
            self.session.shutdown()
        if self.radio is not None:
            self.radio.stop()

    def _start(self, password="beerbeer", civ_address=None, **radio_kw):
        self.radio = FakeRadio(**radio_kw).start()
        self.events = _Events()
        self.session = RadioSession(self.events)
        p = self.radio.ports
        spec = ConnectionSpec("127.0.0.1", "beer", password, p["control"], p["serial"], p["audio"],
                              civ_address)
        return spec

    def test_login_audio_and_civ(self):
        spec = self._start()
        wav = os.path.join(tempfile.mkdtemp(), "rx.wav")
        self.session.start_recording(wav)
        self.session.connect(spec)
        self.assertTrue(wait_for(lambda: self.session.connected), self.events.items)
        radio = self.events.kinds("radio")[0]
        self.assertEqual(radio["name"], "IC-7300MK2")
        self.assertEqual(radio["civ_address"], 0xB6)          # taken from the capabilities packet
        self.assertIn(("control", "stream_request:IC-7300MK2"), self.radio.events)
        self.assertEqual(self.radio.requested_rate, 48000)
        self.assertEqual(self.radio.requested_ports, (spec.serial_port, spec.audio_port))
        self.assertIn(("serial", "open"), self.radio.events)

        self.assertTrue(wait_for(lambda: self.session.rig.freq_hz == 14_074_000
                                 and self.session.rig.mode_label == "USB-D"))
        self.assertTrue(wait_for(lambda: self.session.stats().get("audio_seconds", 0) > 1.0))
        self.assertTrue(wait_for(lambda: "smeter" in self.session.meters))
        self.assertEqual(self.session.meters["smeter"], 120)

        # VFO knob on the radio -> transceive broadcast -> rig event
        self.radio.push_freq(7_074_000)
        self.assertTrue(wait_for(lambda: self.session.rig.freq_hz == 7_074_000))

        # pings: the client answers the radio's and the radio answers ours
        self.assertTrue(wait_for(lambda: self.radio.audio.pings_answered > 0
                                 and self.radio.control.pings_answered > 0, 5))
        self.assertTrue(wait_for(lambda: self.session.stats().get("rtt_ms", 0) > 0, 5))

        info = self.session.stop_recording()
        self.assertGreater(info["seconds"], 1.0)
        with wave.open(wav) as w:
            self.assertEqual(w.getframerate(), 48000)
            x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float)
        # the fake sends a continuous 1 kHz tone: dominant FFT bin at 1 kHz,
        # and no discontinuities (lost or reordered packets would show up here)
        spec_ = np.abs(np.fft.rfft(x[:48000]))
        self.assertAlmostEqual(np.argmax(spec_) * 48000 / 48000, 1000, delta=2)
        self.assertLess(np.max(np.abs(np.diff(x))), 8000 * 2 * np.pi * 1000 / 48000 * 1.05)

    def test_retransmit_recovers_dropped_audio(self):
        spec = self._start(drop_audio={20, 21, 35, 60})
        wav = os.path.join(tempfile.mkdtemp(), "rx.wav")
        self.session.start_recording(wav)
        self.session.connect(spec)
        self.assertTrue(wait_for(lambda: self.session.stats().get("audio_packets", 0) > 120))
        st = self.session.stats()
        self.assertEqual(st["audio_lost"], 0)
        self.assertGreaterEqual(st["audio_retx_requests"], 3)
        self.assertGreaterEqual(self.radio.audio.retransmits_served, 4)
        self.session.stop_recording()
        with wave.open(wav) as w:
            x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float)
        self.assertLess(np.max(np.abs(np.diff(x))), 8000 * 2 * np.pi * 1000 / 48000 * 1.05)

    def test_configured_civ_address_mismatch_is_reported(self):
        spec = self._start(civ_address=0xA4)
        self.session.connect(spec)
        self.assertTrue(wait_for(lambda: self.session.connected))
        texts = [t for t, lvl in self.events.kinds("status")]
        self.assertTrue(any("reports CI-V address B6h but A4h" in t for t in texts), texts)

    def test_radio_in_use_by_other_client(self):
        spec = self._start(in_use_by=("SHACK-PC", (10, 0, 0, 7)))
        self.session.connect(spec)
        self.assertTrue(wait_for(lambda: any("in use by another client ('SHACK-PC' at 10.0.0.7)" in t
                                             for t, _ in self.events.kinds("status")), 6))
        self.assertFalse(self.session.connected)
        # never asked for streams it can't have, and logged out cleanly
        self.assertFalse(any(e[1].startswith("stream_request") for e in self.radio.events))
        self.assertIn(("control", "auth_01"), self.radio.events)

    def test_bad_password_stops(self):
        spec = self._start(password="wrong")
        self.session.connect(spec)
        self.assertTrue(wait_for(lambda: self.events.kinds("stopped"), 6))
        self.assertFalse(self.session.connected)
        self.assertEqual(self.radio.logins, 1)                # no retry storm
        self.assertTrue(any("invalid username/password" in t for t, _ in self.events.kinds("status")))

    def test_clean_disconnect_deauths(self):
        spec = self._start()
        self.session.connect(spec)
        self.assertTrue(wait_for(lambda: self.session.connected))
        self.session.disconnect()
        ev = self.radio.events
        self.assertIn(("control", "auth_01"), ev)
        self.assertIn(("serial", "close"), ev)
        for name in ("control", "serial", "audio"):
            self.assertIn((name, "disconnect"), ev)

    def test_audio_timeout_reconnects(self):
        spec = self._start(send_audio=False)
        self.session.connect(spec)
        self.assertTrue(wait_for(lambda: any("no audio from the radio" in t
                                             for t, _ in self.events.kinds("status")), 9))
        self.assertTrue(wait_for(lambda: self.radio.logins >= 2, 5))

    def test_host_name_and_bad_name(self):
        spec = self._start()
        self.session.connect(ConnectionSpec("localhost", "beer", "beerbeer", spec.control_port,
                                            spec.serial_port, spec.audio_port))
        self.assertTrue(wait_for(lambda: self.session.connected))
        self.session.disconnect()
        self.session.connect(ConnectionSpec("no-such-radio.invalid", "u", "p"))
        self.assertTrue(wait_for(lambda: any("cannot resolve no-such-radio.invalid" in t
                                             for t, _ in self.events.kinds("status")), 6))

    def test_no_radio(self):
        self.events = _Events()
        self.session = RadioSession(self.events)
        self.session.connect(ConnectionSpec("127.0.0.1", "u", "p", 9))   # discard port: nothing there
        self.assertTrue(wait_for(lambda: any("did not answer" in t for t, _ in self.events.kinds("status")), 6))


if __name__ == "__main__":
    unittest.main()
