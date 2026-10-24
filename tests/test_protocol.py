"""Unit tests: passcode, packet layouts (checked against kappanhang's radio
captures), reorder buffer, CI-V.   Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icomremote import civ  # noqa: E402
from icomremote.rsba1 import packets as P  # noqa: E402
from icomremote.rsba1.passcode import encode  # noqa: E402
from icomremote.rsba1.seqbuf import ReorderBuffer, seq_diff  # noqa: E402

# 0xA8 capabilities packet from an IC-705, as captured in kappanhang's controlstream.go.
IC705_CAPS = bytes([
    0xa8, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02, 0x00, 0x01, 0x13, 0x11, 0x18, 0x38, 0xff, 0x55, 0x7d,
    0x00, 0x00, 0x00, 0x98, 0x02, 0x02, 0x00, 0x07, 0x00, 0x00, 0x7f, 0x91, 0x00, 0x00, 0x4f, 0x0d,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x01, 0x93, 0x8a, 0x01, 0x24, 0x17, 0x64, 0xbc, 0x4b, 0xa3, 0xa0, 0x13, 0x58, 0x41, 0x04,
    0x58, 0x2d, 0x49, 0x43, 0x2d, 0x37, 0x30, 0x35, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x49, 0x43, 0x4f, 0x4d, 0x5f, 0x56, 0x41, 0x55, 0x44, 0x49, 0x4f, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x3f, 0x3f, 0xa4, 0x01, 0xff, 0x01, 0xff, 0x01, 0x01, 0x01, 0x00, 0x00, 0x4b, 0x00,
    0x01, 0x50, 0x00, 0xb8, 0x0b, 0x00, 0x00, 0x00])


class TestPasscode(unittest.TestCase):
    def test_known_mapping(self):
        # b(98)+0 -> 0x2b, e(101)+1 -> 0x3f, e+2 -> 0x55, r(114)+3 -> 0x5c
        self.assertEqual(encode("beer")[:5], bytes([0x2b, 0x3f, 0x55, 0x5c, 0x00]))
        self.assertEqual(len(encode("beer")), 16)

    def test_wraparound(self):
        # '~'(126) at position 5 -> 131 -> 32 + 131 % 127 = 36 -> 0x66
        self.assertEqual(encode("aaaaa~")[5], 0x66)

    def test_truncates_to_16(self):
        self.assertEqual(encode("x" * 20), encode("x" * 16))

    def test_rejects_control_chars(self):
        with self.assertRaises(ValueError):
            encode("ab\tc")


class TestPackets(unittest.TestCase):
    def test_header_roundtrip(self):
        h = P.header(0x10, P.T_I_AM_HERE, 0x1234, 0xAABBCCDD, 0x11223344)
        self.assertEqual(h[:8], bytes([0x10, 0, 0, 0, 0x04, 0, 0x34, 0x12]))
        self.assertEqual(P.parse_header(h), (0x10, 4, 0x1234, 0xAABBCCDD, 0x11223344))

    def test_login_layout(self):
        p = P.login(1, 2, 0x0203, b"\x55\x66", encode("u"), encode("p"))
        self.assertEqual(len(p), 0x80)
        self.assertEqual(p[16:28], bytes([0, 0, 0, 0x70, 0x01, 0x00, 0x00, 0x03, 0x02, 0x00, 0x55, 0x66]))
        self.assertEqual(p[64:80], encode("u"))
        self.assertEqual(p[80:96], encode("p"))
        self.assertEqual(p[96:104], b"icom-pc\x00")

    def test_password_never_logged(self):
        p = P.login(1, 2, 0, b"\x00\x00", encode("user"), encode("secret"))
        self.assertNotIn(encode("secret").hex(" ")[:20], P.hexdump(p, 200))
        self.assertIn(encode("user").hex(" ")[:8], P.hexdump(p, 200))

    def test_auth_layout(self):
        p = P.auth(1, 2, 7, b"ABCDEF", 0x05)
        self.assertEqual(len(p), 0x40)
        self.assertEqual(p[16:32], bytes([0, 0, 0, 0x30, 0x01, 0x05, 0x00, 0x07, 0x00, 0x00]) + b"ABCDEF")

    def test_stream_request_layout(self):
        p = P.stream_request(1, 2, 3, b"ABCDEF", bytes(range(16)), "IC-7300MK2", encode("u"),
                             48000, 50002, 50003, 300)
        self.assertEqual(len(p), 0x90)
        self.assertEqual(p[16:26], bytes([0, 0, 0, 0x80, 0x01, 0x03, 0x00, 0x03, 0x00, 0x00]))
        self.assertEqual(p[32:48], bytes(range(16)))
        self.assertEqual(P.cstring(p[64:96]), "IC-7300MK2")
        self.assertEqual(p[96:112], encode("u"))
        # kappanhang: 0x01 0x01 0x04 0x04 0 0 rate rate | 0 0 rate rate | 0 0 civport | 0 0 audioport
        #             | 0 0 txbuf | 0x01 0...
        self.assertEqual(p[112:137], bytes([1, 1, 4, 4, 0, 0, 0xBB, 0x80, 0, 0, 0xBB, 0x80,
                                            0, 0, 0xC3, 0x52, 0, 0, 0xC3, 0x53, 0, 0, 0x01, 0x2C, 1]))
        self.assertEqual(p[137:], bytes(7))

    def test_serial_data_layout(self):
        f = civ.read_freq(0xB6)
        p = P.serial_data(1, 2, 0x0102, f)
        self.assertEqual(p[0], 0x15 + len(f))
        self.assertEqual(p[16:21], bytes([0xC1, len(f), 0x00, 0x01, 0x02]))
        self.assertEqual(p[21:], f)
        self.assertTrue(P.is_serial_data(p))

    def test_serial_open(self):
        p = P.serial_open_close(1, 2, 0, False)
        self.assertEqual(len(p), 0x16)
        self.assertEqual(p[16:], bytes([0xC0, 0x01, 0x00, 0x00, 0x00, 0x05]))

    def test_audio_layout(self):
        p = P.audio_data(1, 2, 0, bytes(1364))
        self.assertEqual(p[:6], bytes([0x6c, 0x05, 0, 0, 0, 0]))     # same as kappanhang
        self.assertEqual(p[16:24], bytes([0x80, 0, 0, 0, 0, 0, 0x05, 0x54]))
        p2 = P.audio_data(1, 2, 1, bytes(556))
        self.assertEqual(p2[:2], bytes([0x44, 0x02]))
        self.assertTrue(P.is_audio_data(p) and P.is_audio_data(p2))

    def test_capabilities_from_capture(self):
        self.assertTrue(P.is_capabilities(IC705_CAPS))
        caps = P.parse_capabilities(IC705_CAPS)
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0].name, "IC-705")
        self.assertEqual(caps[0].audio_name, "ICOM_VAUDIO")
        self.assertEqual(caps[0].civ_address, 0xA4)
        self.assertEqual(caps[0].guid, IC705_CAPS[66:82])     # what kappanhang echoes back

    def test_ping(self):
        p = P.ping(9, 1, 2, False, b"\x01\x02\x03\x06")
        self.assertEqual(len(p), 21)
        self.assertEqual(p[0], 0x15)
        self.assertTrue(P.is_ping(p))
        # radio pings start with 0x00 in the first byte
        self.assertTrue(P.is_ping(b"\x00" + p[1:]))

    def test_retransmit(self):
        one = P.retransmit_request(0x1234, 1, 2)
        self.assertTrue(P.is_retransmit_request(one))
        self.assertEqual(P.parse_retransmit_request(one), [(0x1234, 0x1234)])
        rng = P.retransmit_range_request([(10, 12)], 1, 2)
        self.assertEqual(rng[0], 0x14)
        self.assertEqual(P.parse_retransmit_request(rng), [(10, 12)])
        self.assertFalse(P.is_retransmit_request(P.control(P.T_DATA, 5, 1, 2)))


class TestReorderBuffer(unittest.TestCase):
    def test_in_order(self):
        b = ReorderBuffer(0.1)
        out = []
        for s in range(5):
            b.add(s, s)
            ready, req, dl = b.poll(0.0)
            out += [p for _, p in ready]
            self.assertIsNone(req)
        self.assertEqual(out, [0, 1, 2, 3, 4])

    def test_gap_filled_by_retransmit(self):
        b = ReorderBuffer(0.1)
        b.add(1, "a")
        b.poll(0)
        b.add(4, "d")
        ready, req, dl = b.poll(0.0)
        self.assertEqual(ready, [])
        self.assertEqual(req, (2, 3))
        self.assertAlmostEqual(dl, 0.1)
        b.add(3, "c")
        ready, req, dl = b.poll(0.02)
        self.assertEqual((ready, req), ([], None))          # still waiting for 2, no duplicate request
        b.add(2, "b")
        ready, req, dl = b.poll(0.05)
        self.assertEqual([p for _, p in ready], ["b", "c", "d"])
        self.assertEqual(b.lost, 0)

    def test_gap_times_out(self):
        b = ReorderBuffer(0.1)
        b.add(10, "x")
        b.poll(0)
        b.add(13, "y")
        b.poll(0)
        ready, req, dl = b.poll(0.11)
        self.assertEqual([p for _, p in ready], ["y"])
        self.assertEqual(b.lost, 2)
        self.assertIsNone(dl)

    def test_hold_follows_rtt(self):
        b = ReorderBuffer(0.1)
        b.rtt = 0.2
        b.add(0, 0)
        b.poll(0)
        b.add(2, 2)
        _, _, dl = b.poll(1.0)
        self.assertAlmostEqual(dl, 1.4)

    def test_wraparound(self):
        b = ReorderBuffer(0.1)
        out = []
        for s in (0xFFFE, 0xFFFF, 0, 1):
            b.add(s, s)
            out += [p for _, p in b.poll(0)[0]]
        self.assertEqual(out, [0xFFFE, 0xFFFF, 0, 1])
        self.assertEqual(seq_diff(0, 0xFFFF), 1)

    def test_duplicates_and_late(self):
        b = ReorderBuffer(0.1)
        b.add(5, "a")
        b.poll(0)
        self.assertFalse(b.add(5, "a"))
        self.assertFalse(b.add(3, "old"))
        self.assertEqual(b.duplicates, 2)

    def test_large_gap_not_requested(self):
        b = ReorderBuffer(0.1)
        b.add(0, 0)
        b.poll(0)
        b.add(50, 50)
        ready, req, dl = b.poll(0)
        self.assertIsNone(req)          # too many to ask for
        self.assertIsNotNone(dl)

    def test_resync_after_restart(self):
        b = ReorderBuffer(0.1)
        b.add(100, "a")
        b.poll(0)
        b.add(5000, "b")
        ready, _, _ = b.poll(0)
        self.assertEqual([p for _, p in ready], ["b"])


class TestCiv(unittest.TestCase):
    def test_freq_bcd(self):
        self.assertEqual(civ.encode_freq(14_074_000), bytes([0x00, 0x40, 0x07, 0x14, 0x00]))
        self.assertEqual(civ.decode_freq(bytes([0x00, 0x40, 0x07, 0x14, 0x00])), 14_074_000)
        self.assertEqual(civ.decode_freq(civ.encode_freq(1_234_567_890)), 1_234_567_890)

    def test_level_bcd(self):
        self.assertEqual(civ.encode_level(255), b"\x02\x55")
        self.assertEqual(civ.decode_level(b"\x01\x20"), 120)

    def test_split_frames(self):
        a = civ.read_freq(0xB6)
        b = civ.read_mode(0xB6)
        frames, rest = civ.split_frames(b"\x00" + a + b + b"\xfe\xfe\xb6")
        self.assertEqual(frames, [a, b])
        self.assertEqual(rest, b"\xfe\xfe\xb6")

    def test_decoder(self):
        d = civ.CivDecoder(0xB6)
        echo = civ.read_freq(0xB6)
        reply = civ.frame(0xE0, 0x03, None, civ.encode_freq(7_074_000), frm=0xB6)
        mode = civ.frame(0xE0, 0x04, None, b"\x01\x02", frm=0xB6)
        data = civ.frame(0xE0, 0x1A, 0x06, b"\x01\x01", frm=0xB6)
        ev = d.feed(echo + reply + mode + data)
        self.assertEqual([k for k, _ in ev], ["freq", "mode", "mode"])
        self.assertEqual(d.state.freq_hz, 7_074_000)
        self.assertEqual(d.state.mode_label, "USB-D")
        self.assertEqual(d.state.filter, 1)

    def test_transceive_broadcast(self):
        d = civ.CivDecoder(0xB6)
        ev = d.feed(civ.frame(0x00, 0x00, None, civ.encode_freq(3_573_000), frm=0xB6))
        self.assertEqual(ev, [("freq", 3_573_000)])

    def test_foreign_address(self):
        d = civ.CivDecoder(0xB6)
        ev = d.feed(civ.frame(0xE0, 0x03, None, civ.encode_freq(1), frm=0xA4))
        self.assertEqual(ev, [("foreign", 0xA4)])

    def test_meters(self):
        self.assertEqual(civ.s_meter_text(civ.s_meter_db(0)), "S0")
        self.assertEqual(civ.s_meter_text(civ.s_meter_db(120)), "S9")
        self.assertEqual(civ.s_meter_text(civ.s_meter_db(241)), "S9+60")
        self.assertAlmostEqual(civ.po_percent(213), 100.0)
        self.assertAlmostEqual(civ.swr(80), 2.0)


if __name__ == "__main__":
    unittest.main()
