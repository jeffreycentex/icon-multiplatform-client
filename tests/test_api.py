"""Remote control API: service (tune, record, status), HTTP server and ctl client,
against the fake radio."""

import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import wave
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_rsba1 import FakeRadio  # noqa: E402
from icomremote import ctl  # noqa: E402
from icomremote.api import ApiServer  # noqa: E402
from icomremote.service import ApiError, IcomService  # noqa: E402
from icomremote.session import ConnectionSpec, PttRefused  # noqa: E402
from test_session import wait_for  # noqa: E402
from test_tx import LAN, USB, FakeSource  # noqa: E402


class ServiceCase(unittest.TestCase):
    connect = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.radio = FakeRadio(mode=USB, data=False).start()
        self.svc = IcomService(state_dir=self.tmp, record_dir=self.tmp)
        self.session = self.svc.radio
        self.session._tx_source_factory = FakeSource
        if self.connect:
            p = self.radio.ports
            self.session.connect(ConnectionSpec("127.0.0.1", "beer", "beerbeer",
                                                p["control"], p["serial"], p["audio"]))
            self.assertTrue(wait_for(lambda: self.session.connected and self.session.rig.mode == "USB"
                                     and self.session.data_mod == LAN))

    def tearDown(self):
        self.svc.shutdown()
        self.radio.stop()

    def key_data(self):
        """USB-D with a transmit input, ready to key."""
        self.session.set_tx_device("fake")
        self.session.set_mode("USB", True)
        self.assertTrue(wait_for(lambda: self.radio.data and self.session._tx_source is not None))


class TestServiceIdle(ServiceCase):
    connect = False

    def test_status_and_tune_without_connection(self):
        st = self.svc.status()
        self.assertEqual(st["state"], "idle")
        self.assertIsNone(st["tune"])
        self.assertEqual(st["recording"], {"active": False})
        with self.assertRaises(ApiError) as cm:
            self.svc.tune(7_074_000)
        self.assertEqual((cm.exception.code, cm.exception.http_status), ("not_connected", 409))

    def test_bad_login_is_failed(self):
        p = self.radio.ports
        self.session.connect(ConnectionSpec("127.0.0.1", "beer", "wrong", p["control"], p["serial"], p["audio"]))
        self.assertTrue(wait_for(lambda: self.svc.state() == "failed"))
        self.assertTrue(self.svc.status()["last_error"])


class TestTune(ServiceCase):
    def test_frequency_and_modes(self):
        t = self.svc.tune(7_074_000, "USB")
        self.assertEqual((t["freq_hz"], t["mode"]), (7_074_000, "USB-D"))     # plain USB -> USB-D
        self.assertTrue(wait_for(lambda: self.radio.freq_hz == 7_074_000 and self.radio.data))
        self.svc.plain_ssb_is_data = False
        self.assertEqual(self.svc.tune(mode="usb")["mode"], "USB")
        self.assertTrue(wait_for(lambda: not self.radio.data))
        self.assertEqual(self.svc.tune(mode="PKTLSB")["mode"], "LSB-D")
        self.assertEqual(self.svc.tune(mode="SAM")["mode"], "AM")
        self.assertEqual(self.svc.tune(passband=500)["passband"], 9000)        # ignored: filter stays
        self.assertEqual(self.svc.status()["tune"]["mode"], "AM")

    def test_validation_changes_nothing(self):
        for kw in ({"mode": "CW"}, {"freq_hz": 144_174_000}, {"freq_hz": 144_174_000, "mode": "LSB"}):
            with self.assertRaises(ApiError) as cm:
                self.svc.tune(**kw)
            self.assertEqual(cm.exception.code, "bad_request")
        time.sleep(0.3)
        self.assertEqual((self.radio.freq_hz, self.radio.mode), (14_074_000, USB))

    def test_refused_while_transmitting(self):
        self.key_data()
        self.session.set_ptt(True)
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        with self.assertRaises(ApiError) as cm:
            self.svc.tune(7_074_000)
        self.assertEqual((cm.exception.code, cm.exception.http_status), ("transmitting", 409))
        self.session.set_ptt(False)
        self.assertTrue(wait_for(lambda: not self.radio.ptt and not self.session.tx))
        self.svc.tune(7_074_000)
        self.assertTrue(wait_for(lambda: self.radio.freq_hz == 7_074_000))
        self.assertEqual(self.radio.freq_log[-1][1], 7_074_000)

    def test_refused_while_tuning(self):
        self.session.tune()
        self.assertTrue(wait_for(lambda: self.radio.tuner == 0x02))
        with self.assertRaises(ApiError) as cm:
            self.svc.tune(7_074_000)
        self.assertEqual(cm.exception.code, "transmitting")

    def test_tune_never_reaches_the_radio_while_keyed(self):
        """Race: PTT presses and releases from another thread while the API tunes.
        Every frequency set the radio receives must arrive while it is unkeyed."""
        self.key_data()
        stop = threading.Event()

        def ptt_spam():
            on = False
            while not stop.is_set():
                on = not on
                try:
                    self.session.set_ptt(on)
                except PttRefused:
                    pass
                time.sleep(0.003 if on else 0.02)
            self.session.set_ptt(False)

        th = threading.Thread(target=ptt_spam)
        th.start()
        accepted = 0
        end = time.monotonic() + 3.0
        try:
            while time.monotonic() < end:
                try:
                    self.svc.tune(7_000_000 + accepted * 100)
                    accepted += 1
                except ApiError as e:
                    self.assertEqual(e.code, "transmitting")
                time.sleep(0.002)
        finally:
            stop.set()
            th.join()
        self.assertTrue(wait_for(lambda: not self.radio.ptt))
        time.sleep(0.3)
        keyed_at = [t for t, on in self.radio.ptt_log if on]
        self.assertTrue(keyed_at, "PTT never keyed: race not exercised")
        self.assertGreater(len(self.radio.freq_log), 0, "no tune got through: race not exercised")

        def keyed(t):
            state = False
            for when, on in self.radio.ptt_log:
                if when > t:
                    break
                state = on
            return state
        bad = [(t, hz) for t, hz in self.radio.freq_log if keyed(t)]
        self.assertEqual(bad, [])


class TestRecording(ServiceCase):
    def test_name_template_max_seconds_and_conflict(self):
        r = self.svc.start_recording(name="{date}_net {mode}", max_seconds=1.5)
        self.assertTrue(r["active"])
        self.assertTrue(os.path.basename(r["path"]).endswith("_net_USB.wav"), r["path"])
        self.assertEqual(r["max_seconds"], 1.5)
        with self.assertRaises(ApiError) as cm:
            self.svc.start_recording()
        self.assertEqual((cm.exception.code, cm.exception.http_status), ("conflict", 409))
        self.assertTrue(wait_for(lambda: not self.svc.recording_info()["active"], 5.0))
        with wave.open(r["path"]) as w:
            self.assertEqual(w.getframerate(), 48000)
            self.assertGreater(w.getnframes(), 48000)
        self.assertIsNone(self.svc.stop_recording())                 # nothing running: fine

    def test_paths(self):
        p = os.path.join(self.tmp, "net")
        r = self.svc.start_recording(path=p)
        self.assertEqual(r["path"], p + ".wav")
        self.svc.stop_recording()
        r = self.svc.start_recording(path=p + ".wav")                # never overwrites
        self.assertEqual(r["path"], p + "_1.wav")
        self.svc.stop_recording()
        for kw in ({"path": p + ".m4a"}, {"name": "{nope}"}, {"max_seconds": 0}):
            with self.assertRaises(ApiError) as cm:
                self.svc.start_recording(**kw)
            self.assertEqual(cm.exception.code, "bad_request")

    def test_gap_after_reconnect_is_filled(self):
        r = self.svc.start_recording()
        s = self.session
        self.assertTrue(wait_for(lambda: (s.recording_seconds() or 0) > 0.5))
        with s._sink_lock:                   # as if the connection had dropped 2 s ago
            s._rec_last_audio -= 2.0
            s._rec_gap_pending = True
        self.assertTrue(wait_for(lambda: s.gap_seconds >= 1.9))
        self.assertLess(s.gap_seconds, 2.5)
        info = self.svc.stop_recording()
        self.assertGreaterEqual(info["gap_seconds"], 1.9)
        with wave.open(r["path"]) as w:
            self.assertGreaterEqual(w.getnframes(), int(2.4 * 48000))


class ApiCase(ServiceCase):
    token = None

    def setUp(self):
        super().setUp()
        self.api = ApiServer(self.svc, "127.0.0.1", 0, self.token)
        self.api.start()

    def tearDown(self):
        self.api.stop()
        super().tearDown()

    def call(self, method, path, body=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.api.port, timeout=10)
        h = {"Content-Type": "application/json"} if method == "POST" else {}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body or {}) if method == "POST" else None)
        c.request(method, "/api/v1" + path, data, h)
        resp = c.getresponse()
        out = resp.status, json.loads(resp.read())
        c.close()
        return out


class TestApi(ApiCase):
    def test_info_and_status(self):
        code, r = self.call("GET", "")
        self.assertEqual((code, r["api"], r["api_version"]), (200, "icom-remote", 1))
        self.assertIn("POST /api/v1/tune", r["endpoints"])
        code, r = self.call("GET", "/status")
        self.assertEqual((code, r["ok"], r["state"], r["power"]), (200, True, "connected", "on"))
        self.assertEqual(r["radio"]["name"], "IC-7300MK2")
        self.assertEqual(r["tune"]["freq_hz"], 14_074_000)

    def test_tune_and_record(self):
        code, r = self.call("POST", "/tune", {"freq_khz": 7110, "mode": "LSB"})
        self.assertEqual((code, r["tune"]["freq_hz"], r["tune"]["mode"]), (200, 7_110_000, "LSB-D"))
        code, r = self.call("POST", "/record/start", {"name": "Texas-Net_{date}T{time}", "max_seconds": 60})
        self.assertEqual(code, 200)
        path = r["recording"]["path"]
        self.assertTrue(os.path.basename(path).startswith("Texas-Net_"))
        code, r = self.call("POST", "/record/start", {})
        self.assertEqual((code, r["error"]["code"]), (409, "conflict"))
        self.assertTrue(wait_for(lambda: self.call("GET", "/status")[1]["recording"]["seconds"] > 0.5))
        code, r = self.call("POST", "/record/stop")
        self.assertEqual((code, r["was_recording"], r["recording"]["reason"]), (200, True, "api"))
        self.assertTrue(os.path.exists(path))
        code, r = self.call("POST", "/record/stop")
        self.assertEqual((code, r["ok"], r["was_recording"]), (200, True, False))

    def test_errors(self):
        self.assertEqual(self.call("POST", "/tune", {})[1]["error"]["code"], "bad_request")
        self.assertEqual(self.call("POST", "/tune", {"freq_hz": "7074"})[1]["error"]["code"], "bad_request")
        code, r = self.call("POST", "/tune", {"freq_hz": 1, "freq_khz": 1})
        self.assertEqual((code, r["error"]["code"]), (400, "bad_request"))
        code, r = self.call("POST", "/connect", {"server": "radio"})
        self.assertEqual((code, r["error"]["code"]), (404, "not_found"))
        self.assertIn("window", r["error"]["message"])
        self.assertEqual(self.call("POST", "/disconnect")[0], 404)
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("GET", "/tune")[1]["error"]["code"], "method_not_allowed")
        self.assertEqual(self.call("POST", "/tune", raw="{x")[1]["error"]["code"], "bad_json")
        self.key_data()
        self.session.set_ptt(True)
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        code, r = self.call("POST", "/tune", {"freq_hz": 7_074_000})
        self.assertEqual((code, r["error"]["code"]), (409, "transmitting"))
        self.assertTrue(self.call("GET", "/status")[1]["transmitting"])
        self.session.set_ptt(False)

    def test_browser_protections(self):
        code, r = self.call("POST", "/tune", {"freq_khz": 7074}, headers={"Content-Type": "text/plain"})
        self.assertEqual((code, r["error"]["code"]), (415, "unsupported_media_type"))
        code, r = self.call("GET", "/status", headers={"Host": "evil.example.com"})
        self.assertEqual((code, r["error"]["code"]), (403, "forbidden_host"))
        self.assertEqual(self.call("OPTIONS", "/tune")[0], 405)

    def test_ctl_client(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ctl.main(["--port", str(self.api.port), "tune", "--khz", "7074", "--mode", "pktusb"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["tune"]["mode"], "USB-D")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ctl.main(["--port", str(self.api.port), "record-stop"]), 0)
            self.assertEqual(ctl.main(["--port", str(self.api.port), "tune", "--hz", "1"]), 1)
        self.api.stop()
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ctl.main(["--port", str(self.api.port), "status"]), 2)


class TestApiToken(ApiCase):
    connect = False
    token = "s3cret"

    def test_token_required(self):
        self.assertEqual(self.call("GET", "/status")[1]["error"]["code"], "unauthorized")
        self.assertEqual(self.call("GET", "/status", headers={"Authorization": "Bearer nope"})[0], 401)
        code, r = self.call("GET", "/status", headers={"Authorization": "Bearer s3cret"})
        self.assertEqual((code, r["state"]), (200, "idle"))
        self.assertEqual(self.call("GET", "/status", headers={"X-Api-Token": "s3cret"})[0], 200)

    def test_non_loopback_needs_token(self):
        with self.assertRaises(ValueError):
            ApiServer(self.svc, "0.0.0.0", 0, None)


if __name__ == "__main__":
    unittest.main()
