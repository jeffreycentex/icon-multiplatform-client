"""Step 2: frequency/mode/filter control and rigctld, against the fake radio."""

import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_rsba1 import FakeRadio  # noqa: E402
from icomremote import civ  # noqa: E402
from icomremote.rigctld import RigctldServer  # noqa: E402
from icomremote.session import ConnectionSpec, NotConnected, RadioSession  # noqa: E402
from icomremote.state import hamlib_mode, rig_mode_from_hamlib  # noqa: E402
from test_session import wait_for  # noqa: E402


class TestModeMapping(unittest.TestCase):
    def test_hamlib_in(self):
        self.assertEqual(rig_mode_from_hamlib("PKTUSB"), ("USB", True))
        self.assertEqual(rig_mode_from_hamlib("usb"), ("USB", True))            # default: plain SSB = data
        self.assertEqual(rig_mode_from_hamlib("USB", plain_ssb_is_data=False), ("USB", False))
        self.assertEqual(rig_mode_from_hamlib("FM"), ("FM", False))
        self.assertEqual(rig_mode_from_hamlib("PKTFM"), ("FM", True))
        self.assertIsNone(rig_mode_from_hamlib("CW"))

    def test_hamlib_out(self):
        self.assertEqual(hamlib_mode("USB", True), "PKTUSB")
        self.assertEqual(hamlib_mode("LSB", False), "LSB")

    def test_if_width(self):
        self.assertEqual(civ.if_width_hz("USB", 0), 50)
        self.assertEqual(civ.if_width_hz("USB", 9), 500)
        self.assertEqual(civ.if_width_hz("USB", 34), 3000)
        self.assertEqual(civ.if_width_hz("AM", 44), 9000)
        self.assertEqual(civ.RigState(mode="FM", filter=2).width_hz, 10000)

    def test_set_commands(self):
        self.assertEqual(civ.set_freq(0xB6, 7_074_000),
                         bytes([0xFE, 0xFE, 0xB6, 0xE0, 0x05, 0x00, 0x40, 0x07, 0x07, 0x00, 0xFD]))
        self.assertEqual(civ.set_mode(0xB6, "USB", 2), bytes([0xFE, 0xFE, 0xB6, 0xE0, 0x06, 0x01, 0x02, 0xFD]))
        self.assertEqual(civ.set_data_mode(0xB6, True, 1),
                         bytes([0xFE, 0xFE, 0xB6, 0xE0, 0x1A, 0x06, 0x01, 0x01, 0xFD]))
        self.assertEqual(civ.set_data_mode(0xB6, False, 1),
                         bytes([0xFE, 0xFE, 0xB6, 0xE0, 0x1A, 0x06, 0x00, 0x00, 0xFD]))


class _Base(unittest.TestCase):
    def setUp(self):
        self.radio = FakeRadio(mode=0x01, data=False).start()
        self.session = RadioSession()
        p = self.radio.ports
        self.session.connect(ConnectionSpec("127.0.0.1", "beer", "beerbeer", p["control"], p["serial"], p["audio"]))
        self.assertTrue(wait_for(lambda: self.session.connected and self.session.rig.width_hz))

    def tearDown(self):
        self.session.shutdown()
        self.radio.stop()


class TestControl(_Base):
    def test_set_frequency(self):
        st = self.session.set_frequency(7_074_000)
        self.assertEqual(st.freq_hz, 7_074_000)                 # optimistic, immediately
        self.assertTrue(wait_for(lambda: self.radio.freq_hz == 7_074_000))
        with self.assertRaises(ValueError):                     # IC-7300MK2 tops out at 74.8 MHz
            self.session.set_frequency(144_174_000)

    def test_set_mode_data_and_filter(self):
        self.assertEqual(self.session.rig.width_hz, 3000)       # USB FIL1
        self.session.set_mode("USB", True, 2)
        self.assertTrue(wait_for(lambda: self.radio.data and self.radio.filt == 2))
        self.assertTrue(wait_for(lambda: self.session.rig.mode_label == "USB-D"
                                 and self.session.rig.width_hz == 2400))
        self.session.set_mode("FM", False)
        self.assertTrue(wait_for(lambda: self.radio.mode == 0x05 and not self.radio.data))
        self.assertTrue(wait_for(lambda: self.session.rig.width_hz == 10000))   # FM FIL2
        self.session.set_filter(3)
        self.assertTrue(wait_for(lambda: self.radio.filt == 3 and self.session.rig.width_hz == 7000))

    def test_disconnected_raises(self):
        self.session.disconnect()
        with self.assertRaises(NotConnected):
            self.session.set_frequency(7_000_000)


class TestRigctld(_Base):
    def setUp(self):
        super().setUp()
        self.rig = RigctldServer(self.session, "127.0.0.1", 0)
        self.rig.start()
        self.sock = socket.create_connection(("127.0.0.1", self.rig.port), timeout=3)
        self.f = self.sock.makefile("rw", newline="\n")

    def tearDown(self):
        self.sock.close()
        self.rig.stop()
        super().tearDown()

    def cmd(self, line, lines=1):
        self.f.write(line + "\n")
        self.f.flush()
        return [self.f.readline().rstrip("\n") for _ in range(lines)]

    def test_freq(self):
        self.assertEqual(self.cmd("f"), ["14074000"])
        self.assertEqual(self.cmd("F 7074000"), ["RPRT 0"])
        self.assertEqual(self.cmd("f"), ["7074000"])            # read right after set
        self.assertTrue(wait_for(lambda: self.radio.freq_hz == 7_074_000))
        self.assertEqual(self.cmd("F 200000000"), ["RPRT -1"])

    def test_mode(self):
        self.assertEqual(self.cmd("m", 2), ["USB", "3000"])
        self.assertEqual(self.cmd("M USB 2400"), ["RPRT 0"])    # plain USB -> USB-D, passband ignored
        self.assertTrue(wait_for(lambda: self.radio.data and self.radio.mode == 0x01))
        self.assertEqual(self.radio.filt, 1)
        self.assertEqual(self.cmd("m", 2), ["PKTUSB", "3000"])
        self.assertEqual(self.cmd("M PKTLSB 0"), ["RPRT 0"])
        self.assertTrue(wait_for(lambda: self.radio.mode == 0x00 and self.radio.data))
        self.assertEqual(self.cmd("M CW 500"), ["RPRT -11"])

    def test_plain_ssb_option(self):
        self.rig.plain_ssb_is_data = False
        self.cmd("M LSB 0")
        self.assertTrue(wait_for(lambda: self.radio.mode == 0x00))
        time.sleep(0.2)
        self.assertFalse(self.radio.data)

    def test_ptt_refused_outside_data_mode(self):
        self.assertEqual(self.cmd("T 1"), ["RPRT -9"])    # radio is in plain USB
        self.assertEqual(self.cmd("T 0"), ["RPRT 0"])
        self.assertEqual(self.cmd("t"), ["0"])
        self.assertFalse(self.radio.ptt)

    def test_strength_and_dump_state(self):
        self.assertTrue(wait_for(lambda: "smeter" in self.session.meters))
        self.assertEqual(self.cmd("l STRENGTH"), ["0"])         # fake radio sits at S9
        self.f.write("\\dump_state\n")
        self.f.flush()
        lines = []
        while True:
            line = self.f.readline().rstrip("\n")
            lines.append(line)
            if line == "done":
                break
        self.assertTrue(lines[3].startswith("30000.000000 74800000.000000 0x401c2d"), lines[3])

    def test_vfo_mode_client(self):
        self.assertEqual(self.cmd("+\\get_freq VFOA", 3), ["get_freq:", "Frequency: 14074000", "RPRT 0"])

    def test_disconnected(self):
        self.session.disconnect()
        self.assertEqual(self.cmd("f"), ["14074000"])           # last known
        self.assertEqual(self.cmd("F 7000000"), ["RPRT -6"])


if __name__ == "__main__":
    unittest.main()
