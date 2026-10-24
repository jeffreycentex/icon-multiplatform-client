"""Step 4: RF power, Po/SWR while transmitting, TUNE."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from icomremote import civ  # noqa: E402
from icomremote.session import PttRefused  # noqa: E402
from test_session import wait_for  # noqa: E402
from test_tx import _TxBase  # noqa: E402


class TestCivStep4(unittest.TestCase):
    def test_frames(self):
        self.assertEqual(civ.set_rf_power(0xB6, 255).hex(" "), "fe fe b6 e0 14 0a 02 55 fd")
        self.assertEqual(civ.set_tuner(0xB6, 2).hex(" "), "fe fe b6 e0 1c 01 02 fd")
        self.assertEqual(civ.read_po(0xB6).hex(" "), "fe fe b6 e0 15 11 fd")
        self.assertEqual(civ.read_swr(0xB6).hex(" "), "fe fe b6 e0 15 12 fd")

    def test_decode(self):
        d = civ.CivDecoder(0xB6)
        ev = d.feed(civ.frame(0xE0, 0x15, 0x11, b"\x01\x43", frm=0xB6)
                    + civ.frame(0xE0, 0x15, 0x12, b"\x00\x48", frm=0xB6)
                    + civ.frame(0xE0, 0x14, 0x0A, b"\x01\x28", frm=0xB6)
                    + civ.frame(0xE0, 0x1C, 0x01, b"\x02", frm=0xB6))
        self.assertEqual(ev, [("po", 143), ("swr", 48), ("rfpower", 128), ("tuner", 2)])
        self.assertAlmostEqual(civ.po_percent(143), 50.0)
        self.assertAlmostEqual(civ.swr(48), 1.5)


class TestRfPower(_TxBase):
    def test_read_and_set(self):
        self.assertTrue(wait_for(lambda: self.session.rf_power == 128))
        self.session.set_rf_power(25)
        self.assertTrue(wait_for(lambda: self.radio.rf_power == 64))       # 25 % of 255
        self.assertEqual(self.session.rf_power, 64)
        self.radio.rf_power = 200                                           # knob turned on the radio
        self.assertTrue(wait_for(lambda: self.session.rf_power == 200, 6))
        self.assertTrue(any(k == "rf_power" and v == 200 for k, v in self.events))


class TestTxMeters(_TxBase):
    def test_po_swr_while_keyed(self):
        self.session.set_tx_device("fake", 1.0)
        self.session.set_mode("USB", True)
        self.assertTrue(wait_for(lambda: self.radio.data and self.session._tx_source is not None
                                 and self.session.data_mod == 0x05))
        self.events.clear()
        self.session.set_ptt(True)
        self.assertTrue(wait_for(lambda: any(k == "meter" and v[0] == "po" and v[1] > 0 for k, v in self.events)))
        self.assertTrue(wait_for(lambda: any(k == "meter" and v[0] == "swr" and v[1] == 48 for k, v in self.events)))
        self.session.set_ptt(False)
        self.assertTrue(wait_for(lambda: not self.radio.ptt))
        time.sleep(1.0)
        self.events.clear()
        time.sleep(1.0)
        self.assertFalse(any(k == "meter" and v[0] in ("po", "swr") for k, v in self.events))   # RX: S-meter only
        self.assertTrue(any(k == "meter" and v[0] == "smeter" for k, v in self.events))


class TestTune(_TxBase):
    def test_tune_cycle(self):
        self.assertTrue(wait_for(lambda: self.session.tuner == 1))
        self.session.tune()
        self.assertTrue(wait_for(lambda: self.radio.tuner == 2))
        with self.assertRaisesRegex(PttRefused, "tuner is tuning"):
            self.session.set_tx_device("fake", 1.0) or self.session.set_ptt(True)
        self.assertTrue(wait_for(lambda: any(k == "meter" and v[0] == "swr" for k, v in self.events)))
        self.assertTrue(wait_for(lambda: not self.session._tuning, 4))
        self.assertTrue(any("Tune finished: tuner on" in t for t in self.statuses()))
        self.assertEqual(self.radio.tune_aborted, 0)
        self.assertEqual(len(self.radio.tune_started), 1)

    def test_tune_refused_while_transmitting(self):
        self.session.set_tx_device("fake", 1.0)
        self.session.set_mode("USB", True)
        self.assertTrue(wait_for(lambda: self.radio.data and self.session._tx_source is not None
                                 and self.session.data_mod == 0x05))
        self.session.set_ptt(True)
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        with self.assertRaisesRegex(PttRefused, "TUNE refused"):
            self.session.tune()
        self.session.set_ptt(False)

    def test_disconnect_aborts_tune(self):
        self.radio.tune_seconds = 30
        self.session.tune()
        self.assertTrue(wait_for(lambda: self.radio.tuner == 2))
        self.session.disconnect()
        self.assertEqual(self.radio.tuner, 1)
        self.assertEqual(self.radio.tune_aborted, 1)


class TestTuneFails(_TxBase):
    radio_kw = {"tune_result": 0x00}

    def test_failed_tune_reported(self):
        self.assertTrue(wait_for(lambda: self.session.tuner == 1))
        self.session.tune()
        self.assertTrue(wait_for(lambda: not self.session._tuning, 4))
        self.assertTrue(any("Tune finished: tuner off" in t for t in self.statuses()))


class TestTuneTimeout(_TxBase):
    radio_kw = {"tune_seconds": 60}

    def test_timeout(self):
        import icomremote.session as S
        old = S.TUNE_TIMEOUT
        S.TUNE_TIMEOUT = 1.0
        try:
            self.session.tune()
            self.assertTrue(wait_for(lambda: self.radio.tuner == 2))
            self.assertTrue(wait_for(lambda: not self.session._tuning, 4))
        finally:
            S.TUNE_TIMEOUT = old
        self.assertEqual(self.radio.tuner, 1)
        self.assertTrue(any("Tune stopped after" in t for t in self.statuses()))


if __name__ == "__main__":
    unittest.main()
