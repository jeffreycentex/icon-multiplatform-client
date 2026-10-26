"""Recovery from the intermittent failures seen on the real radio (2026-10-25):
no received audio after connecting, and keyed-but-silent data transmit that a
restart cured.

- RX: the reorder buffer follows a sequence counter that went back instead of
  dropping every packet as "already delivered" (and the audio timeout now
  watches delivered audio, so a stall reconnects).
- RX: a sound card whose callback stopped is reopened.
- TX: an input that delivers nothing while keyed is reopened.
- TX: DATA MOD that couldn't be read refuses PTT, and is read again later.
"""

import os
import sys
import threading
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from icomremote import session as session_mod  # noqa: E402
from icomremote.rsba1.seqbuf import LATE_RESYNC, ReorderBuffer  # noqa: E402
from icomremote.session import PttRefused  # noqa: E402
from test_session import wait_for  # noqa: E402
from test_tx import LAN, FakeSource, _TxBase  # noqa: E402


class TestCounterWentBack(unittest.TestCase):
    def test_follows_counter_back(self):
        b = ReorderBuffer(0.1)
        for s in range(5000, 5010):
            b.add(s, s)
        ready, _, _ = b.poll(0.0)
        self.assertEqual(len(ready), 10)
        out = []
        for s in range(100, 100 + LATE_RESYNC + 10):       # sender restarted lower
            b.add(s, s)
            out += [p for _, p in b.poll(0.0)[0]]
        self.assertEqual(b.resyncs, 1)
        self.assertEqual(out, list(range(100 + LATE_RESYNC - 1, 100 + LATE_RESYNC + 10)))

    def test_a_few_duplicates_dont_resync(self):
        b = ReorderBuffer(0.1)
        for s in range(10):
            b.add(s, s)
            b.poll(0.0)
        for s in range(5):                                  # retransmitted copies
            self.assertFalse(b.add(s, s))
        b.add(10, 10)
        self.assertEqual([p for _, p in b.poll(0.0)[0]], [10])
        self.assertEqual(b.resyncs, 0)


class StalledOnce:
    """First input opened captures nothing (a stalled CoreAudio stream); later ones work."""
    opened = 0

    def __new__(cls, key, gain):
        cls.opened += 1
        return _Dead(key, gain) if cls.opened == 1 else FakeSource(key, gain)


class _Dead(FakeSource):
    def open(self, on_frame):
        from icomremote.txaudio import TxFramer
        self.framer = TxFramer(44100, self.gain, on_frame)    # no capture thread


class TestTxInputWatchdog(_TxBase):
    def _session(self):
        s = super()._session()
        StalledOnce.opened = 0
        s._tx_source_factory = StalledOnce
        return s

    def test_stalled_input_is_reopened(self):
        self.session.set_tx_device("fake", 1.0)
        self.session.set_mode("USB", True)
        self.assertTrue(wait_for(lambda: self.radio.data and self.session._tx_source is not None
                                 and self.session.data_mod == LAN))
        self.session.set_ptt(True)
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        self.assertTrue(wait_for(lambda: self.radio.tx_audio_while_ptt > 20, 6))
        self.assertEqual(StalledOnce.opened, 2)
        self.session.set_ptt(False)
        self.assertTrue(wait_for(lambda: not self.radio.ptt))


class TestUnknownDataMod(_TxBase):
    radio_kw = {"menu_ng": True}

    def setUp(self):
        self._ticks = session_mod.MOD_RETRY_TICKS
        session_mod.MOD_RETRY_TICKS = 4
        super().setUp()

    def tearDown(self):
        super().tearDown()
        session_mod.MOD_RETRY_TICKS = self._ticks

    def test_refused_then_recovers(self):
        self.assertTrue(wait_for(lambda: any("Could not read DATA MOD" in t for t in self.statuses()), 15))
        self.assertTrue(wait_for(lambda: self.session._mods_ready is True, 15))
        self.session.set_tx_device("fake", 1.0)
        self.session.set_mode("USB", True)
        self.assertTrue(wait_for(lambda: self.radio.data and self.session._tx_source is not None))
        with self.assertRaisesRegex(PttRefused, "DATA MOD could not be read"):
            self.session.set_ptt(True)
        self.assertFalse(self.radio.ptt)
        self.radio.menu_ng = False                          # radio answers again
        self.assertTrue(wait_for(lambda: self.session.data_mod == LAN, 20))
        self.assertEqual(self.radio.menu[self.radio.item("data_mod")], LAN)
        self.session.set_ptt(True)
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        self.session.set_ptt(False)
        self.assertTrue(wait_for(lambda: not self.radio.ptt))
        self.session.disconnect()
        self.assertEqual(self.radio.menu[self.radio.item("data_mod")], 0x01)   # restored to USB


class FrozenSink:
    """A sound card whose callback stopped running."""
    made = []

    def __init__(self, key="Speakers|Core Audio", gain=1.0):
        self.device_key, self.gain = key, gain
        self.callbacks = 7
        self.writes = 0
        self.closed = False
        FrozenSink.made.append(self)

    def set_input_rate(self, rate):
        pass

    def write(self, samples):
        self.writes += 1

    def close(self):
        self.closed = True


class TestSinkStall(_TxBase):
    def test_stalled_card_is_reopened(self):
        orig = session_mod.SoundCardSink
        session_mod.SoundCardSink = FrozenSink
        try:
            FrozenSink.made = []
            self.session._install_sink("card", FrozenSink())
            self.assertTrue(wait_for(lambda: len(FrozenSink.made) >= 2, 8))
            self.assertTrue(FrozenSink.made[0].closed)
            self.assertTrue(wait_for(lambda: any("had stalled and was reopened" in t
                                                 for t in self.statuses())))
        finally:
            session_mod.SoundCardSink = orig

    def test_running_card_is_left_alone(self):
        sink = FrozenSink()
        stop = threading.Event()

        def tick():
            while not stop.wait(0.02):
                sink.callbacks += 1
        threading.Thread(target=tick, daemon=True).start()
        try:
            self.session._install_sink("card", sink)
            self.assertTrue(wait_for(lambda: sink.writes > 300, 8))
            self.assertFalse(sink.closed)
            self.assertIs(self.session._sinks.get("card"), sink)
        finally:
            stop.set()


if __name__ == "__main__":
    np.seterr(all="ignore")
    unittest.main()
