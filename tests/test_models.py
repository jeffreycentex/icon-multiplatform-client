"""Per-model differences: DATA MOD item numbers, value codes (IC-705 WLAN = 03,
IC-9700 has ACC before USB), tuner presence, IC-7610 not written to."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_rsba1 import FakeRadio  # noqa: E402
from icomremote import civ  # noqa: E402
from icomremote.session import ConnectionSpec, PttRefused, RadioSession  # noqa: E402
from icomremote.state import PROFILES, freq_limits  # noqa: E402
from test_session import wait_for  # noqa: E402


class TestTables(unittest.TestCase):
    def test_profiles(self):
        self.assertEqual(PROFILES["IC-9700 (A2h)"], 0xA2)
        self.assertEqual(PROFILES["IC-7610 (98h)"], 0x98)

    def test_values(self):
        self.assertEqual(civ.lan_value("IC-7300MK2"), 0x05)
        self.assertEqual(civ.lan_value("IC-705"), 0x03)
        self.assertEqual(civ.mod_name("IC-705", 0x03), "WLAN")
        self.assertEqual(civ.mod_name("IC-9700", 0x01), "ACC")
        self.assertEqual(civ.mod_name("IC-7300MK2", 0x01), "USB")
        self.assertFalse(civ.has_tuner("IC-9700"))
        self.assertIsNone(civ.model_info("IC-7610")["data_mod"])
        self.assertEqual(freq_limits("IC-9700"), (136_000_000, 1_320_000_000))


class _ModelBase(unittest.TestCase):
    model = None
    radio_kw = {}

    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.radio = FakeRadio(name=self.model, freq_hz=self.freq, **self.radio_kw).start()
        self.events = []
        self.session = RadioSession(lambda k, v: self.events.append((k, v)), state_dir=self.state_dir)
        p = self.radio.ports
        self.session.connect(ConnectionSpec("127.0.0.1", "beer", "beerbeer", p["control"], p["serial"], p["audio"]))
        self.assertTrue(wait_for(lambda: self.session.connected and self.session.power == "on"))

    def tearDown(self):
        self.session.shutdown()
        self.radio.stop()

    def statuses(self):
        return [v[0] for k, v in self.events if k == "status"]


class TestIC9700(_ModelBase):
    model = "IC-9700"
    freq = 144_174_000
    radio_kw = {"data_mod": 0x03, "data_off_mod": 0x01}         # USB, ACC in IC-9700 codes

    def test_data_mod_items_and_names(self):
        self.assertTrue(wait_for(lambda: self.session.data_mod == 0x05
                                 and self.session.mods["data_off_mod"]["value"] == 0x05))
        self.assertEqual(self.radio.menu[b"\x01\x16"], 0x05)     # DATA MOD = 1A 05 01 16
        self.assertEqual(self.radio.menu[b"\x01\x15"], 0x05)     # DATA OFF MOD = 1A 05 01 15
        self.assertTrue(wait_for(lambda: any("DATA MOD switched from USB to LAN" in t for t in self.statuses())))
        self.assertTrue(wait_for(lambda: any("DATA OFF MOD switched from ACC to LAN" in t for t in self.statuses())))
        self.session.disconnect()
        self.assertEqual(self.radio.menu[b"\x01\x16"], 0x03)
        self.assertEqual(self.radio.menu[b"\x01\x15"], 0x01)

    def test_vhf_tuning_and_no_tuner(self):
        self.session.set_frequency(432_065_000)
        self.assertTrue(wait_for(lambda: self.radio.freq_hz == 432_065_000))
        with self.assertRaises(ValueError):
            self.session.set_frequency(14_074_000)               # no HF on the IC-9700
        with self.assertRaisesRegex(PttRefused, "no antenna tuner"):
            self.session.tune()


class TestIC705(_ModelBase):
    model = "IC-705"
    freq = 7_074_000
    radio_kw = {"data_mod": 0x01, "data_off_mod": 0x00}

    def test_wlan_is_03(self):
        self.assertTrue(wait_for(lambda: self.session.data_mod == 0x03
                                 and self.session.mods["data_off_mod"]["value"] == 0x03))
        self.assertEqual(self.radio.menu[b"\x01\x19"], 0x03)     # DATA MOD = 1A 05 01 19, WLAN = 03
        self.assertEqual(self.radio.menu[b"\x01\x18"], 0x03)
        self.assertTrue(wait_for(lambda: any("DATA MOD switched from USB to LAN" in t for t in self.statuses())))
        self.session.disconnect()
        self.assertEqual(self.radio.menu[b"\x01\x19"], 0x01)
        self.assertEqual(self.radio.menu[b"\x01\x18"], 0x00)


class TestIC7610(_ModelBase):
    model = "IC-7610"
    freq = 14_074_000

    def test_menu_never_written(self):
        self.assertTrue(wait_for(lambda: any("DATA MOD switching is not known for IC-7610" in t
                                             for t in self.statuses())))
        self.session.disconnect()
        self.assertEqual(self.radio.menu_writes, [])


if __name__ == "__main__":
    unittest.main()
