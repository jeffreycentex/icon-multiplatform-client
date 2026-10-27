"""Step 5: power ON/OFF, standby detection, link loss."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from icomremote import civ  # noqa: E402
from icomremote.session import NotConnected, PttRefused  # noqa: E402
from test_session import wait_for  # noqa: E402
from test_tx import LAN, MIC, USB, _TxBase  # noqa: E402


class TestPowerFrames(unittest.TestCase):
    def test_frames(self):
        self.assertEqual(civ.power_off(0xB6).hex(" "), "fe fe b6 e0 18 00 fd")
        on = civ.power_on(0xB6)
        self.assertTrue(on.startswith(b"\xfe" * 25))
        self.assertTrue(on.endswith(bytes.fromhex("fefeb6e01801fd")))
        frames, _ = civ.split_frames(on)                      # the long preamble still parses as one frame
        self.assertEqual(frames, [bytes.fromhex("fefeb6e01801fd")])


class TestPowerOffOn(_TxBase):
    def test_off_then_on(self):
        self.assertTrue(wait_for(lambda: self.session.power == "on" and self.session.data_mod == LAN))
        self.assertEqual(self.session.remote_power_setting, 0x00)      # as read from the real radio
        self.session.power_off()
        self.assertTrue(wait_for(lambda: not self.radio.powered and self.session.power == "standby"))
        # DATA MOD and DATA OFF MOD were put back before switching off
        self.assertEqual(self.radio.menu[b"\x00\x85"], USB)
        self.assertEqual(self.radio.menu[b"\x00\x84"], MIC)
        self.assertEqual(self.pending(), {})
        self.assertTrue(wait_for(lambda: any("Radio switched off" in t for t in self.statuses())))
        # standby: stays connected (audio timeout suspended), controls refused
        time.sleep(6.5)
        self.assertTrue(self.session.connected)
        self.assertEqual(self.radio.logins, 1)
        with self.assertRaisesRegex(NotConnected, "not powered on"):
            self.session.set_frequency(7_074_000)
        with self.assertRaisesRegex(PttRefused, "not powered on"):
            self.session.set_ptt(True)
        # power on: CI-V answers again, DATA MOD switched to LAN again
        self.session.power_on()
        self.assertTrue(wait_for(lambda: self.radio.powered))
        self.assertTrue(wait_for(lambda: self.session.power == "on" and self.session.data_mod == LAN, 8))
        self.assertEqual(self.radio.menu[b"\x00\x85"], LAN)
        self.assertTrue(wait_for(lambda: any("Radio powered on" in t for t in self.statuses())))
        self.assertEqual(self.radio.power_log, ["off", "on"])
        self.session.set_frequency(7_074_000)                  # works again
        self.assertTrue(wait_for(lambda: self.radio.freq_hz == 7_074_000))

    def test_switched_off_on_the_radio(self):
        # Wait for the connect-time DATA MOD setup to finish: standby detection
        # starts after it (slow CI switched the radio off in the middle of it).
        self.assertTrue(wait_for(lambda: self.session.power == "on" and self.session._mods_ready is True))
        self.radio.powered = False                             # power button on the radio
        self.assertTrue(wait_for(lambda: self.session.power == "standby", 8))
        self.assertTrue(self.session.connected)
        self.radio.powered = True                              # and on again, locally
        self.assertTrue(wait_for(lambda: self.session.power == "on", 8))

    def test_power_off_unkeys_first(self):
        self.session.set_tx_device("fake", 1.0)
        self.session.set_mode("USB", True)
        self.assertTrue(wait_for(lambda: self.radio.data and self.session._tx_source is not None
                                 and self.session.data_mod == LAN))
        self.session.set_ptt(True)
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        self.session.power_off()
        self.assertTrue(wait_for(lambda: not self.radio.powered))
        on_off = [on for _, on in self.radio.ptt_log]
        self.assertEqual(on_off[-1], False)                    # unkeyed before the power-off
        self.assertFalse(self.session.ptt)


class TestStartsInStandby(_TxBase):
    radio_kw = {"powered": False}

    def setUp(self):
        import tempfile
        from fake_rsba1 import FakeRadio
        self.state_dir = tempfile.mkdtemp()
        self.radio = FakeRadio(mode=USB, data=False, **self.radio_kw).start()
        self.events = []
        self.session = self._session()
        self.session.connect(self._spec())
        self.assertTrue(wait_for(lambda: self.session.connected))

    def test_connect_in_standby_then_power_on(self):
        self.assertTrue(wait_for(lambda: self.session.power == "standby"))
        self.assertEqual(self.radio.menu[b"\x00\x85"], USB)    # untouched while off
        self.session.power_on()
        self.assertTrue(wait_for(lambda: self.session.power == "on" and self.session.rig.mode == "USB", 8))
        self.assertTrue(wait_for(lambda: self.radio.menu[b"\x00\x85"] == LAN))


class TestPowerOnDoesNothing(TestStartsInStandby):
    radio_kw = {"powered": False, "power_on_works": False}

    def test_connect_in_standby_then_power_on(self):
        import icomremote.session as S
        old = S.POWER_ON_TIMEOUT
        S.POWER_ON_TIMEOUT = 2.0
        try:
            self.assertTrue(wait_for(lambda: self.session.power == "standby"))
            self.session.power_on()
            self.assertTrue(wait_for(lambda: any("did not respond to power on" in t for t in self.statuses()), 6))
        finally:
            S.POWER_ON_TIMEOUT = old
        self.assertEqual(self.session.power, "standby")


class TestNetworkGoesDown(_TxBase):
    """A radio whose network goes down with it (IC-705 on Wi-Fi). The IC-7300MK2 keeps it up."""
    radio_kw = {"network_off_when_off": True}

    def test_network_goes_down(self):
        self.assertTrue(wait_for(lambda: self.session.power == "on"))
        self.session.power_off()
        self.assertTrue(wait_for(lambda: self.radio.network_down))
        self.assertTrue(wait_for(lambda: any("stopped responding" in t for t in self.statuses()), 12))
        self.assertTrue(wait_for(lambda: not self.session.connected))


if __name__ == "__main__":
    unittest.main()
