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
    from PySide6.QtCore import QSettings
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

        w._connect_clicked()                                # Disconnect
        self.assertTrue(self.pump_until(lambda: not w.freq.isEnabled()
                                        and w.connect_btn.text() == "Connect"))


if __name__ == "__main__":
    unittest.main()
