"""Headless GUI smoke test (skipped when PySide6 isn't installed).

Runs the real main window offscreen against the fake radio: connect, then
drive frequency, mode, DATA and filter controls and check that both the
radio and the window follow. Settings go to a temporary file, not yours.
"""

import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from fake_rsba1 import FakeRadio  # noqa: E402
from icomremote.session import ConnectionSpec  # noqa: E402


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class TestGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from icomremote.gui import MainWindow
        self.tmp = tempfile.mkdtemp()
        settings = QSettings(os.path.join(self.tmp, "test.ini"), QSettings.IniFormat)
        settings.setValue("rig_enable", False)            # don't grab port 4532
        self.radio = FakeRadio(mode=0x01, data=False).start()
        self.w = MainWindow(settings=settings)
        from icomremote.restore import RestoreStore
        self.w.radio._store = RestoreStore(self.tmp)     # keep restore records out of your config dir
        self.w.show()

    def tearDown(self):
        self.w.close()
        self.radio.stop()

    def pump_until(self, pred, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            self.app.processEvents()
            if pred():
                return True
            time.sleep(0.02)
        return False

    def test_connect_and_control(self):
        w = self.w
        self.assertFalse(w.freq.isEnabled())               # disabled until connected
        p = self.radio.ports
        w.radio.connect(ConnectionSpec("127.0.0.1", "beer", "beerbeer", p["control"], p["serial"], p["audio"]))
        self.assertTrue(self.pump_until(lambda: w.freq.isEnabled() and w.freq.value() == 14074.0))
        self.assertTrue(w.mode_buttons["USB"].isChecked())
        self.assertFalse(w.data_btn.isChecked())
        self.assertTrue(self.pump_until(lambda: w.width_label.text() == "3.0 kHz"))
        self.assertIn("IC-7300MK2", w.info_label.text())
        self.assertTrue(self.pump_until(lambda: w.smeter.format() == "S9"))

        w.freq.setValue(7074.0)                             # like pressing Enter after typing
        self.assertTrue(self.pump_until(lambda: self.radio.freq_hz == 7_074_000))

        w.data_btn.click()
        self.assertTrue(self.pump_until(lambda: self.radio.data and w.data_btn.isChecked()))

        w.mode_buttons["LSB"].click()
        self.assertTrue(self.pump_until(lambda: self.radio.mode == 0x00 and self.radio.data))
        self.assertTrue(w.mode_buttons["LSB"].isChecked())

        w.filter_buttons[2].click()
        self.assertTrue(self.pump_until(lambda: self.radio.filt == 2 and w.width_label.text() == "2.4 kHz"))

        self.radio.push_freq(3_573_000)                     # VFO turned on the radio
        self.assertTrue(self.pump_until(lambda: w.freq.value() == 3573.0))

        # ---- recording
        w.rec_dir.setText(self.tmp)
        w.rec_btn.click()
        self.assertTrue(self.pump_until(lambda: w.rec_label.text().startswith("Recording")))
        self.assertTrue(self.pump_until(lambda: (w.radio.recording_seconds() or 0) > 1.0))
        w.rec_btn.click()
        self.assertTrue(w.rec_label.text().startswith("Saved icom_"), w.rec_label.text())
        self.assertTrue([f for f in os.listdir(self.tmp) if f.endswith(".wav")])

        # ---- transmit
        from test_tx import FakeSource
        self.assertTrue(self.pump_until(lambda: "LAN (was USB" in w.data_mod_label.text()))
        self.assertTrue(w.restore_btn.isEnabled())
        w.radio._tx_source_factory = FakeSource
        w.radio.set_tx_device("fake")
        w.mode_buttons["USB"].click()
        self.assertTrue(self.pump_until(lambda: self.radio.mode == 0x01))
        w.data_btn.click()                                  # DATA off -> plain USB
        self.assertTrue(self.pump_until(lambda: not self.radio.data and not w.data_btn.isChecked()))
        w.ptt_btn.click()                                   # refused: not a data mode
        self.assertTrue(self.pump_until(lambda: "only allowed in data modes" in w.statusBar().currentMessage()))
        self.assertFalse(w.ptt_btn.isChecked())
        self.assertFalse(self.radio.ptt)
        w.data_btn.click()
        self.assertTrue(self.pump_until(lambda: self.radio.data and w.radio._tx_source is not None))
        w.ptt_btn.click()
        self.assertTrue(self.pump_until(lambda: self.radio.ptt and w.tx_indicator.text() == "TX"))
        self.assertTrue(self.pump_until(lambda: w.tx_level.value() > 0))
        w.ptt_btn.click()
        self.assertTrue(self.pump_until(lambda: not self.radio.ptt and w.tx_indicator.text() == "RX"))

        w._connect_clicked()                                # Disconnect
        self.assertTrue(self.pump_until(lambda: not w.freq.isEnabled()
                                        and w.connect_btn.text() == "Connect"))
        self.assertEqual(self.radio.menu[b"\x00\x85"], 0x01)      # DATA MOD put back to USB


    def test_voice_space_bar(self):
        from test_tx import FakeSource
        w = self.w
        w.radio._tx_source_factory = FakeSource
        p = self.radio.ports
        w.radio.connect(ConnectionSpec("127.0.0.1", "beer", "beerbeer", p["control"], p["serial"], p["audio"]))
        self.assertTrue(self.pump_until(lambda: w.voice_arm.isEnabled() and
                                        w.radio.mods["data_off_mod"]["value"] == 0x05))
        self.assertIn("DATA OFF MOD: LAN (was MIC", w.data_mod_label.text())
        w.radio.set_tx_device("fake-voice", 1.0, kind="voice")
        self.assertTrue(self.pump_until(lambda: "voice" in w.radio._tx_sources and w.voice_level.value() > 0))
        w.activateWindow()
        self.pump_until(lambda: w.isActiveWindow(), 2)
        if not w.isActiveWindow():
            self.skipTest("this Qt platform can't make the window active")
        w.ptt_btn.setFocus()

        def space(press=True):
            (QTest.keyPress if press else QTest.keyRelease)(w, Qt.Key_Space)
            self.pump_until(lambda: False, 0.3)

        space(True)                                          # not armed: nothing
        space(False)
        self.assertFalse(self.radio.ptt)

        w.voice_arm.click()
        self.assertTrue(w.voice_arm.isChecked())
        space(True)                                          # armed, plain USB: voice
        self.assertTrue(self.pump_until(lambda: self.radio.ptt and w.tx_indicator.text() == "TX"))
        self.assertEqual(w.radio._tx_kind, "voice")
        space(False)
        self.assertTrue(self.pump_until(lambda: not self.radio.ptt))

        w.host.setFocus()                                    # typing in a field: Space is just a space
        space(True)
        space(False)
        self.assertFalse(self.radio.ptt)

        w.ptt_btn.setFocus()                                 # hold Space, focus moves into a text field
        space(True)
        self.assertTrue(self.pump_until(lambda: self.radio.ptt))
        w.host.setFocus()
        space(False)
        self.assertTrue(self.pump_until(lambda: not self.radio.ptt))

        w.ptt_btn.setFocus()                                 # disarming while keyed unkeys
        space(True)
        self.assertTrue(self.pump_until(lambda: self.radio.ptt))
        w.voice_arm.click()
        self.assertTrue(self.pump_until(lambda: not self.radio.ptt))
        space(False)

        w.voice_arm.click()
        w._connect_clicked()                                 # disconnect disarms
        self.assertTrue(self.pump_until(lambda: w.connect_btn.text() == "Connect"))
        self.assertFalse(w.voice_arm.isChecked())
        self.assertEqual(self.radio.menu[b"\x00\x84"], 0x00)     # DATA OFF MOD back to MIC


if __name__ == "__main__":
    unittest.main()
