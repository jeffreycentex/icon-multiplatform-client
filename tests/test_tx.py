"""Step 3: TX audio, PTT safety rules, DATA MOD switch and restore, rigctld PTT."""

import os
import socket
import sys
import tempfile
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_rsba1 import FakeRadio  # noqa: E402
from icomremote import civ  # noqa: E402
from icomremote.restore import RestoreStore  # noqa: E402
from icomremote.rigctld import RigctldServer  # noqa: E402
from icomremote.session import ConnectionSpec, PttRefused, RadioSession  # noqa: E402
from icomremote.txaudio import FRAME, TxFramer  # noqa: E402
from test_session import wait_for  # noqa: E402

MIC = 0x00
USB = 0x01
LAN = 0x05


class FakeSource:
    """Stands in for SoundCardSource: a 'sound card' playing a 700 Hz tone at 44.1 kHz."""

    def __init__(self, key, gain):
        self.key, self.gain = key, gain
        self.tone = 400 if "voice" in key else 700
        self.framer = None
        self._stop = threading.Event()
        self._phase = 0

    def open(self, on_frame):
        self.framer = TxFramer(44100, self.gain, on_frame)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        # Like a sound card: samples follow wall-clock time, whatever sleep() does
        # (CI machines can oversleep a lot).
        t0 = time.perf_counter()
        while not self._stop.is_set():
            due = int((time.perf_counter() - t0) * 44100) - self._phase
            if due > 0:
                t = (np.arange(due) + self._phase) / 44100.0
                self._phase += due
                self.framer.push((0.5 * np.sin(2 * np.pi * self.tone * t)).astype(np.float32))
            time.sleep(0.01)

    def start(self):
        self.framer.start()

    def stop(self):
        self.framer.stop()

    def set_gain(self, g):
        self.framer.gain = g

    @property
    def peak(self):
        return self.framer.peak

    def close(self):
        self._stop.set()


class TestFramer(unittest.TestCase):
    def test_resample_and_frame(self):
        frames = []
        f = TxFramer(44100, 1.0, frames.append)
        x = np.sin(2 * np.pi * 1000 * np.arange(44100) / 44100).astype(np.float32)
        f.push(x[:4410])
        self.assertEqual(frames, [])                    # inactive: nothing delivered
        self.assertGreater(f.peak, 0.9)                 # but the level meter works
        f.start()
        for i in range(0, 44100, 441):
            f.push(x[i:i + 441])
        self.assertAlmostEqual(len(frames), 50, delta=1)          # 1 s = 50 frames of 20 ms
        self.assertTrue(all(len(p) == FRAME * 2 for p in frames))
        y = np.frombuffer(b"".join(frames[2:-2]), dtype="<i2").astype(float)
        self.assertLess(np.max(np.abs(np.diff(y))), 32767 * 2 * np.pi * 1000 / 48000 * 1.1)

    def test_stop_drops_partial_and_limiter(self):
        frames = []
        f = TxFramer(48000, 4.0, frames.append)         # gain way over unity
        f.start()
        f.push(np.ones(500, dtype=np.float32) * 0.5)
        f.stop()
        f.start()
        f.push(np.ones(960, dtype=np.float32) * 0.5)
        self.assertEqual(len(frames), 1)                # the 500 stale samples were dropped
        y = np.frombuffer(frames[0], dtype="<i2")
        self.assertTrue(np.all(y > 30000))      # soft-limited near full scale, no int16 wraparound


class _TxBase(unittest.TestCase):
    radio_kw = {}

    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.radio = FakeRadio(mode=USB, data=False, **self.radio_kw).start()
        self.events = []
        self.session = self._session()
        self.session.connect(self._spec())
        self.assertTrue(wait_for(lambda: self.session.connected and self.session.rig.mode == "USB"))

    def _session(self):
        s = RadioSession(lambda k, v: self.events.append((k, v)), state_dir=self.state_dir)
        s._tx_source_factory = FakeSource
        return s

    def _spec(self):
        p = self.radio.ports
        return ConnectionSpec("127.0.0.1", "beer", "beerbeer", p["control"], p["serial"], p["audio"])

    def tearDown(self):
        self.session.shutdown()
        self.radio.stop()

    def statuses(self):
        return [v[0] for k, v in self.events if k == "status"]

    def pending(self):
        return RestoreStore(self.state_dir).pending()


class TestDataMod(_TxBase):
    def test_switch_and_restore(self):
        self.assertTrue(wait_for(lambda: self.session.data_mod == LAN
                                 and self.session.mods["data_off_mod"]["value"] == LAN))
        self.assertEqual(self.radio.menu[b"\x00\x85"], LAN)
        self.assertEqual(self.radio.menu[b"\x00\x84"], LAN)
        self.assertEqual(self.pending(), {"IC-7300MK2@127.0.0.1": {"data_mod": USB, "data_off_mod": MIC}})
        self.assertTrue(wait_for(lambda: any("DATA MOD switched from USB to LAN" in t for t in self.statuses())))
        self.session.disconnect()
        self.assertEqual(self.radio.menu[b"\x00\x85"], USB)
        self.assertEqual(self.radio.menu[b"\x00\x84"], MIC)
        self.assertEqual(self.pending(), {})
        self.assertTrue(wait_for(lambda: any("DATA MOD restored to USB" in t for t in self.statuses())))
        self.assertTrue(wait_for(lambda: any("DATA OFF MOD restored to MIC" in t for t in self.statuses())))

    def test_crash_then_recover(self):
        self.assertTrue(wait_for(lambda: self.session.data_mod == LAN))

        async def lost():                                # app dies before it can restore
            return False
        self.session._restore_data_mod = lost
        self.session.disconnect()
        self.assertEqual(self.radio.menu[b"\x00\x85"], LAN)
        self.assertEqual(self.pending()["IC-7300MK2@127.0.0.1"]["data_mod"], USB)   # survived on disk

        self.events.clear()
        self.session = self._session()                    # next run of the app
        self.session.connect(self._spec())
        self.assertTrue(wait_for(lambda: self.session.data_mod == LAN))
        self.assertTrue(wait_for(lambda: any("previous session left DATA MOD changed" in t for t in self.statuses())))
        self.session.disconnect()
        self.assertEqual(self.radio.menu[b"\x00\x85"], USB)                       # original, not LAN
        self.assertEqual(self.pending(), {})

    def test_manual_restore_blocks_tx(self):
        self.assertTrue(wait_for(lambda: self.session.data_mod == LAN))
        self.session.restore_data_mod_now()
        self.assertTrue(wait_for(lambda: self.radio.menu[b"\x00\x85"] == USB and self.session.data_mod == USB))
        self.assertTrue(wait_for(lambda: self.pending() == {}))          # DATA OFF MOD is restored second
        self.session.set_tx_device("fake")
        self.session.set_mode("USB", True)
        self.assertTrue(wait_for(lambda: self.session.rig.data and self.session._tx_source))
        with self.assertRaisesRegex(PttRefused, "DATA MOD is USB"):
            self.session.set_ptt(True)


class TestDataModAlreadyLan(_TxBase):
    radio_kw = {"data_mod": LAN, "data_off_mod": LAN, "lan_mod_level": 0}

    def test_untouched_and_level_warning(self):
        self.assertTrue(wait_for(lambda: self.session.data_mod == LAN))
        self.assertTrue(wait_for(lambda: any("LAN MOD Level is 0" in t for t in self.statuses())))
        self.session.disconnect()
        self.assertEqual(self.radio.menu_writes, [])
        self.assertEqual(self.pending(), {})


class TestUnknownMenu(_TxBase):
    radio_kw = {"menu_ng": True}

    def test_connects_anyway(self):
        self.assertTrue(wait_for(lambda: any("Could not read DATA MOD" in t for t in self.statuses()), 15))
        self.assertTrue(self.session.connected)
        self.assertIsNone(self.session.data_mod)


class TestPtt(_TxBase):
    def _ready_for_tx(self):
        self.session.set_tx_device("fake", 1.0)
        self.session.set_mode("USB", True)
        self.assertTrue(wait_for(lambda: self.radio.data and self.session._tx_source is not None
                                 and self.session.data_mod == LAN))

    def test_refusals(self):
        self.assertTrue(wait_for(lambda: self.session.data_mod == LAN))
        with self.assertRaisesRegex(PttRefused, "only allowed in data modes.*USB"):
            self.session.set_ptt(True)
        self.session.set_mode("USB", True)
        self.assertTrue(wait_for(lambda: self.radio.data))
        with self.assertRaisesRegex(PttRefused, "no transmit audio input"):
            self.session.set_ptt(True)
        self.assertFalse(self.radio.ptt)

    def test_key_send_audio_unkey(self):
        self._ready_for_tx()
        time.sleep(0.3)
        self.assertEqual(self.radio.tx_audio_while_rx, 0)           # nothing sent while not keyed
        self.session.set_ptt(True)
        self.assertTrue(wait_for(lambda: self.radio.ptt and self.session.tx))
        n0, t0 = len(self.radio.tx_audio), time.perf_counter()
        time.sleep(1.0)                                              # CI machines may oversleep:
        got, dt = len(self.radio.tx_audio) - n0, time.perf_counter() - t0   # compare with real time
        self.assertAlmostEqual(got / 96000.0, dt, delta=0.15)       # 48 kHz 16-bit audio in real time
        self.session.set_ptt(False)
        self.assertTrue(wait_for(lambda: not self.radio.ptt and not self.session.tx))
        time.sleep(0.3)
        n1 = self.radio.tx_audio_while_ptt + self.radio.tx_audio_while_rx
        time.sleep(0.3)
        self.assertEqual(self.radio.tx_audio_while_ptt + self.radio.tx_audio_while_rx, n1)   # stopped
        y = np.frombuffer(bytes(self.radio.tx_audio[9600:]), dtype="<i2").astype(float)
        spec = np.abs(np.fft.rfft(y[:48000]))
        self.assertAlmostEqual(np.argmax(spec) * 48000 / len(y[:48000]), 700, delta=3)

    def test_stale_reply_cannot_enable_ptt(self):
        """Regression: a poll reply in flight when DATA is switched off used to
        flip the app's state back to DATA for a moment, and PTT keyed in USB."""
        self._ready_for_tx()
        from icomremote import civ as C
        for _ in range(20):
            self.session.set_mode("USB", True)
            self.assertTrue(wait_for(lambda: self.radio.data and self.session.rig.data))
            # a poll goes out, then DATA is turned off before its answer comes back
            self.session._loop.call_soon_threadsafe(self.session._tx_civ, C.read_data_mode(0xB6))
            self.session.set_mode("USB", False)
            for _ in range(10):
                try:
                    self.session.set_ptt(True)
                except PttRefused:
                    pass
                time.sleep(0.005)
        time.sleep(0.5)
        self.assertFalse(any(on for _, on in self.radio.ptt_log), "radio was keyed in plain USB")

    def test_key_right_after_switching_to_data(self):
        """Regression (Windows CI): a poll reply sent while still in a voice mode
        answered the pre-key DATA check, and PTT was refused right after
        switching to USB-D."""
        self._ready_for_tx()
        from icomremote import civ as C
        for _ in range(15):
            self.session.set_mode("LSB", False)
            self.assertTrue(wait_for(lambda: not self.radio.data))
            self.session._loop.call_soon_threadsafe(self.session._tx_civ, C.read_data_mode(0xB6))  # poll
            self.session.set_mode("USB", True)
            self.session.set_ptt(True)                     # immediately, like a fast click
            self.assertTrue(wait_for(lambda: self.radio.ptt, 3), "PTT was refused after USB-D")
            self.session.set_ptt(False)
            self.assertTrue(wait_for(lambda: not self.radio.ptt))
        self.assertFalse(any("PTT refused" in t for t in self.statuses()))

    def test_radio_confirms_data_before_keying(self):
        self._ready_for_tx()
        self.radio.data = False          # changed on the radio; the app hasn't polled yet
        self.session.set_ptt(True)       # the app still believes USB-D, so this is accepted ...
        self.assertTrue(wait_for(lambda: any("PTT refused: the radio reports USB" in t
                                             for t in self.statuses())))
        time.sleep(0.2)
        self.assertEqual(self.radio.ptt_log, [])     # ... but the radio was never keyed
        self.assertFalse(self.session.ptt)

    def test_timeout(self):
        self._ready_for_tx()
        self.session.ptt_timeout = 0.5
        self.session.set_ptt(True)
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        self.assertTrue(wait_for(lambda: not self.radio.ptt and not self.session.ptt, 3))
        self.assertTrue(wait_for(lambda: any("PTT timeout" in t for t in self.statuses())))

    def test_leaving_data_mode_unkeys(self):
        self._ready_for_tx()
        self.session.set_ptt(True)
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        self.session.set_mode("LSB", False)                         # from the app
        self.assertTrue(wait_for(lambda: not self.radio.ptt))
        self.session.set_mode("USB", True)
        self.assertTrue(wait_for(lambda: self.session.rig.data))
        self.session.set_ptt(True)
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        self.radio.data = False                                     # DATA turned off on the radio
        self.assertTrue(wait_for(lambda: not self.radio.ptt, 4))
        self.assertTrue(wait_for(lambda: any("left DATA mode" in t for t in self.statuses())))

    def test_disconnect_unkeys_and_restores(self):
        self._ready_for_tx()
        self.session.set_ptt(True)
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        self.session.disconnect()
        self.assertFalse(self.radio.ptt)
        self.assertEqual(self.radio.menu[b"\x00\x85"], USB)
        # unkey happened before DATA MOD was put back
        self.assertFalse(self.session.ptt)


def dominant_hz(pcm_bytes):
    y = np.frombuffer(bytes(pcm_bytes), dtype="<i2").astype(float)[:24000]
    spec = np.abs(np.fft.rfft(y))
    return np.argmax(spec) * 48000 / len(y)


class TestVoicePtt(_TxBase):
    def _ready(self, plain=True):
        self.session.set_tx_device("fake-data", 1.0, kind="data")
        self.session.set_tx_device("fake-voice", 1.0, kind="voice")
        self.session.set_mode("USB", not plain)
        self.assertTrue(wait_for(lambda: self.radio.data == (not plain) and len(self.session._tx_sources) == 2
                                 and self.session.mods["data_off_mod"]["value"] == LAN))

    def test_voice_needs_voice_ok(self):
        self._ready()
        with self.assertRaisesRegex(PttRefused, "only allowed in data modes"):
            self.session.set_ptt(True)                       # PTT button / rigctl
        self.assertEqual(self.radio.ptt_log, [])

    def test_voice_transmit_uses_voice_input(self):
        self._ready()
        self.session.set_ptt(True, owner="space", voice_ok=True)
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        self.assertEqual(self.session._tx_kind, "voice")
        n0 = len(self.radio.tx_audio)
        time.sleep(0.8)
        self.session.set_ptt(False)
        self.assertTrue(wait_for(lambda: not self.radio.ptt))
        self.assertAlmostEqual(dominant_hz(self.radio.tx_audio[n0:]), 400, delta=5)   # the mic, not data

    def test_data_mode_still_uses_data_input(self):
        self._ready(plain=False)
        self.session.set_ptt(True, owner="space", voice_ok=True)    # space bar in USB-D: data input
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        self.assertEqual(self.session._tx_kind, "data")
        n0 = len(self.radio.tx_audio)
        time.sleep(0.8)
        self.session.set_ptt(False)
        self.assertTrue(wait_for(lambda: not self.radio.ptt))
        self.assertAlmostEqual(dominant_hz(self.radio.tx_audio[n0:]), 700, delta=5)

    def test_switching_to_data_unkeys_voice(self):
        self._ready()
        self.session.set_ptt(True, owner="space", voice_ok=True)
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        self.radio.data = True                                    # DATA pressed on the radio
        self.assertTrue(wait_for(lambda: not self.radio.ptt, 4))
        self.assertTrue(wait_for(lambda: any("while transmitting voice" in t for t in self.statuses())))

    def test_voice_without_voice_input_refused(self):
        self.session.set_mode("USB", False)
        self.assertTrue(wait_for(lambda: self.session.mods["data_off_mod"]["value"] == LAN))
        with self.assertRaisesRegex(PttRefused, "no transmit audio input selected"):
            self.session.set_ptt(True, voice_ok=True)

    def test_one_input_serves_voice_and_data(self):
        """The GUI has one 'Audio from' input: voice uses it when no separate voice input exists."""
        self.session.set_tx_device("fake-voice", 1.0)          # the data slot, as the GUI does
        self.session.set_mode("USB", False)
        self.assertTrue(wait_for(lambda: "data" in self.session._tx_sources and not self.radio.data
                                 and self.session.mods["data_off_mod"]["value"] == LAN))
        self.session.set_ptt(True, owner="space", voice_ok=True)
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        self.assertEqual((self.session._tx_kind, self.session._tx_src), ("voice", "data"))
        n0 = len(self.radio.tx_audio)
        time.sleep(0.6)
        self.session.set_ptt(False)
        self.assertTrue(wait_for(lambda: not self.radio.ptt))
        self.assertGreater(len(self.radio.tx_audio) - n0, 40000)          # audio was sent
        self.assertAlmostEqual(dominant_hz(self.radio.tx_audio[n0:]), 400, delta=5)

    def test_quick_release_never_sticks(self):
        self._ready()
        for _ in range(10):                                       # tap the space bar
            self.session.set_ptt(True, owner="space", voice_ok=True)
            self.session.release_ptt("space", reason=None)
        time.sleep(0.5)
        self.assertFalse(self.radio.ptt)
        self.assertFalse(self.session.ptt)


class TestVoiceDataOffModNotManaged(_TxBase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.radio = FakeRadio(mode=USB, data=False).start()
        self.events = []
        self.session = self._session()
        self.session.manage_data_off_mod = False
        self.session.connect(self._spec())
        self.assertTrue(wait_for(lambda: self.session.connected and self.session.rig.mode == "USB"))

    def test_refused_while_data_off_mod_is_mic(self):
        self.session.set_tx_device("fake-voice", kind="voice")
        self.assertTrue(wait_for(lambda: "voice" in self.session._tx_sources
                                 and self.session.mods["data_off_mod"]["value"] == MIC))
        with self.assertRaisesRegex(PttRefused, "DATA OFF MOD is MIC"):
            self.session.set_ptt(True, voice_ok=True)
        self.session.disconnect()
        self.assertEqual(self.radio.menu[b"\x00\x84"], MIC)          # never touched


class TestRigctldPtt(_TxBase):
    def setUp(self):
        super().setUp()
        self.rig = RigctldServer(self.session, "127.0.0.1", 0)
        self.rig.start()

    def tearDown(self):
        self.rig.stop()
        super().tearDown()

    def client(self):
        s = socket.create_connection(("127.0.0.1", self.rig.port), timeout=3)
        f = s.makefile("rw", newline="\n")

        def cmd(line):
            f.write(line + "\n")
            f.flush()
            return f.readline().rstrip("\n")

        class _Conn:
            def close(self_):
                f.close()               # socket.close() alone keeps the connection open
                s.close()               # while a makefile() wrapper exists
        return _Conn(), cmd

    def test_ptt_rules_and_owner_release(self):
        self.assertTrue(wait_for(lambda: self.session.data_mod == LAN))
        sock, cmd = self.client()
        self.session.set_tx_device("fake")
        self.assertEqual(cmd("M USB 0"), "RPRT 0")       # plain USB -> USB-D (default)
        self.assertTrue(wait_for(lambda: self.radio.data and self.session._tx_source is not None))
        self.assertEqual(cmd("T 1"), "RPRT 0")
        self.assertEqual(cmd("t"), "1")
        self.assertTrue(wait_for(lambda: self.radio.ptt))
        sock.close()                                       # WSJT-X crashes while transmitting
        self.assertTrue(wait_for(lambda: not self.radio.ptt, 4))
        self.assertTrue(wait_for(lambda: any("rigctl client that keyed PTT disconnected" in t for t in self.statuses())))

    def test_refused_in_plain_mode(self):
        self.rig.plain_ssb_is_data = False
        sock, cmd = self.client()
        self.session.set_tx_device("fake")
        cmd("M USB 0")
        time.sleep(0.3)
        self.assertEqual(cmd("T 1"), "RPRT -9")
        self.assertFalse(self.radio.ptt)
        sock.close()

    def test_dump_state_ptt_type(self):
        sock = socket.create_connection(("127.0.0.1", self.rig.port), timeout=3)
        f = sock.makefile("rw", newline="\n")
        f.write("\\dump_state\n")
        f.flush()
        lines = []
        while not lines or lines[-1] != "done":
            lines.append(f.readline().rstrip("\n"))
        self.assertIn("ptt_type=0x1", lines)
        self.assertTrue(lines[5].startswith("1800000.000000 74800000.000000"), lines[5])   # TX range
        f.close()
        sock.close()


if __name__ == "__main__":
    unittest.main()
