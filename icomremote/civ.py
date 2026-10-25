"""Icom CI-V encoding/decoding (the subset this app uses).

Frame: FE FE <to> <from> <cmd> [<sub>] [data...] FD
The PC is controller address E0. Meter and level values are 4-digit BCD
(0000-0255); frequencies are 10-digit BCD, least significant byte first.
"""

from dataclasses import dataclass, replace

PREAMBLE = b"\xfe\xfe"
EOM = 0xFD
CONTROLLER = 0xE0
BROADCAST = 0x00
OK = 0xFB
NG = 0xFA

# Mode codes (command 01/04/06).
MODE_CODES = {
    "LSB": 0x00, "USB": 0x01, "AM": 0x02, "CW": 0x03, "RTTY": 0x04, "FM": 0x05,
    "WFM": 0x06, "CW-R": 0x07, "RTTY-R": 0x08, "DV": 0x17,
}
MODE_NAMES = {v: k for k, v in MODE_CODES.items()}

# Default CI-V address per model (the radio menu can change it).
DEFAULT_ADDRESSES = {
    "IC-7300MK2": 0xB6,
    "IC-705": 0xA4,
    "IC-9700": 0xA2,
    "IC-7610": 0x98,
    "IC-7300": 0x94,
}


def frame(to, cmd, sub=None, data=b"", frm=CONTROLLER):
    body = bytes([to, frm, cmd]) + (bytes([sub]) if sub is not None else b"") + bytes(data)
    return PREAMBLE + body + bytes([EOM])


def split_frames(buf):
    """Split a byte string into complete CI-V frames; returns (frames, leftover)."""
    frames = []
    while True:
        start = buf.find(PREAMBLE)
        if start < 0:
            return frames, b"" if not buf.endswith(b"\xfe") else b"\xfe"
        # skip extra FE bytes (wake-up preamble)
        s = start
        while s + 2 < len(buf) and buf[s + 2] == 0xFE:
            s += 1
        end = buf.find(bytes([EOM]), s + 2)
        if end < 0:
            return frames, buf[s:]
        frames.append(buf[s:end + 1])
        buf = buf[end + 1:]


def bcd_byte(v):
    return ((v // 10) << 4) | (v % 10)


def bcd_value(b):
    return (b >> 4) * 10 + (b & 0x0F)


def encode_freq(hz):
    hz = int(hz)
    if not 0 <= hz < 10 ** 10:
        raise ValueError("frequency out of range")
    out = bytearray()
    for _ in range(5):
        out.append(bcd_byte(hz % 100))
        hz //= 100
    return bytes(out)


def decode_freq(d):
    hz = 0
    for i, b in enumerate(d[:5]):
        hz += bcd_value(b) * (100 ** i)
    return hz


def encode_level(v):
    """0..255 -> 2 BCD bytes (0x02 0x55 for 255)."""
    v = max(0, min(255, int(round(v))))
    return bytes([bcd_byte(v // 100), bcd_byte(v % 100)])


def decode_level(d):
    """2 BCD bytes -> 0..255."""
    return bcd_value(d[0]) * 100 + bcd_value(d[1])


# ---------------------------------------------------------------- meter scales
# From the IC-7300/IC-705 CI-V reference: piecewise linear calibration points.

def _interp(x, points):
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


S_METER_DB = [(0, -54.0), (120, 0.0), (241, 60.0)]          # dB relative to S9
PO_PERCENT = [(0, 0.0), (143, 50.0), (213, 100.0)]
SWR_POINTS = [(0, 1.0), (48, 1.5), (80, 2.0), (120, 3.0), (255, 6.0)]


def s_meter_db(raw):
    return _interp(raw, S_METER_DB)


def s_meter_text(db):
    if db >= 0.5:
        return "S9+%d" % round(db)
    return "S%d" % max(0, min(9, round(9 + db / 6.0)))


def po_percent(raw):
    return _interp(raw, PO_PERCENT)


def swr(raw):
    return _interp(raw, SWR_POINTS)


# ---------------------------------------------------------------- filters
# IF filter width (command 1A 03), IC-7300 family: one BCD byte, an index.
#   SSB/CW/RTTY: 00-09 -> 50..500 Hz in 50 Hz steps, 10-40 -> 600..3600 Hz in 100 Hz steps
#   AM:          00-49 -> 200..10000 Hz in 200 Hz steps
#   FM:          fixed per filter (no 1A 03)
FM_WIDTHS = {1: 15000, 2: 10000, 3: 7000}
# Factory defaults per filter, used until the radio has answered 1A 03.
NOMINAL_WIDTHS = {"AM": {1: 9000, 2: 6000, 3: 3000}, "FM": FM_WIDTHS,
                  "SSB": {1: 3000, 2: 2400, 3: 1800}}


def nominal_width_hz(mode, filt):
    table = NOMINAL_WIDTHS.get(mode, NOMINAL_WIDTHS["SSB"])
    return table.get(filt, 0)


def if_width_hz(mode, index):
    if mode == "AM":
        return 200 * (index + 1)
    if index <= 9:
        return 50 * (index + 1)
    return 600 + 100 * (index - 10)


# ---------------------------------------------------------------- radio state

@dataclass(frozen=True)
class RigState:
    freq_hz: int = 0
    mode: str = ""          # LSB/USB/AM/FM/... as the radio reports it
    data: bool = False      # DATA mode (USB-D etc.)
    filter: int = 0         # 1..3
    if_width_index: int = -1   # raw 1A 03 value; -1 = unknown

    @property
    def mode_label(self):
        if not self.mode:
            return "?"
        return self.mode + ("-D" if self.data else "")

    @property
    def width_hz(self):
        """Passband of the selected filter, or 0 if unknown."""
        if self.mode == "FM":
            return FM_WIDTHS.get(self.filter, 0)
        if self.if_width_index < 0:
            return 0
        return if_width_hz(self.mode, self.if_width_index)


# Which state fields each solicited read reply carries (used to fence out
# replies to reads sent before a change; see RadioSession).
READ_GROUPS = {"freq": ("freq_hz",), "mode": ("mode", "filter"), "data": ("data",),
               "width": ("if_width_index",)}


def read_group(f):
    """If f is a read request for one of READ_GROUPS, return the group name."""
    if len(f) == 6 and f[4] == 0x03:
        return "freq"
    if len(f) == 6 and f[4] == 0x04:
        return "mode"
    if len(f) == 7 and f[4] == 0x1A and f[5] == 0x06:
        return "data"
    if len(f) == 7 and f[4] == 0x1A and f[5] == 0x03:
        return "width"
    return None


class CivDecoder:
    """Applies frames from the radio to a RigState.

    feed() returns a list of (kind, value) changes: ('freq', hz), ('mode', RigState),
    ('ack', True/False), ('smeter', raw) ... Frames not addressed to us, or our
    own commands echoed back, are ignored.
    """

    def __init__(self, radio_address):
        self.radio_address = radio_address
        self.state = RigState()
        self._buf = b""

    def feed(self, data):
        frames, self._buf = split_frames(self._buf + bytes(data))
        out = []
        for f in frames:
            out.extend(self.decode(f))
        return out

    def decode(self, f):
        if len(f) < 6:
            return []
        to, frm, cmd = f[2], f[3], f[4]
        payload = f[5:-1]
        if frm == CONTROLLER or to not in (CONTROLLER, BROADCAST):
            return []                       # echo of our own command, or for someone else
        if frm != self.radio_address:
            return [("foreign", frm)]
        st = self.state
        if cmd == OK:
            return [("ack", True)]
        if cmd == NG:
            return [("ack", False)]
        if cmd in (0x00, 0x03) and len(payload) >= 5:
            self.state = replace(st, freq_hz=decode_freq(payload))
            return ([("reply", "freq")] if cmd == 0x03 else []) + [("freq", self.state.freq_hz)]
        if cmd in (0x01, 0x04) and len(payload) >= 1:
            mode = MODE_NAMES.get(payload[0], "0x%02X" % payload[0])
            filt = payload[1] if len(payload) > 1 else st.filter
            width = st.if_width_index if (mode, filt) == (st.mode, st.filter) else -1
            self.state = replace(st, mode=mode, filter=filt, if_width_index=width)
            return ([("reply", "mode")] if cmd == 0x04 else []) + [("mode", self.state)]
        if cmd == 0x1A and payload[:1] == b"\x05" and len(payload) >= 4:
            return [("menu", (bytes(payload[1:3]), bytes(payload[3:])))]
        if cmd == 0x1A and payload[:1] == b"\x03" and len(payload) >= 2:
            self.state = replace(st, if_width_index=bcd_value(payload[1]))
            return [("reply", "width"), ("mode", self.state)]
        if cmd == 0x1A and payload[:1] == b"\x06" and len(payload) >= 2:
            data = payload[1] != 0
            filt = payload[2] if data and len(payload) > 2 else st.filter
            self.state = replace(st, data=data, filter=filt)
            return [("reply", "data"), ("mode", self.state)]
        if cmd == 0x15 and len(payload) >= 3:
            return [({0x02: "smeter", 0x11: "po", 0x12: "swr"}.get(payload[0], "meter_%02x" % payload[0]),
                     decode_level(payload[1:3]))]
        if cmd == 0x14 and len(payload) >= 3:
            return [({0x0A: "rfpower"}.get(payload[0], "level_%02x" % payload[0]),
                     decode_level(payload[1:3]))]
        if cmd == 0x1C and len(payload) >= 2:
            return [({0x00: "ptt", 0x01: "tuner"}.get(payload[0], "tx_%02x" % payload[0]), payload[1])]
        return [("other", bytes(f))]


# ---------------------------------------------------------------- commands

def read_freq(addr):
    return frame(addr, 0x03)


def read_mode(addr):
    return frame(addr, 0x04)


def read_data_mode(addr):
    return frame(addr, 0x1A, 0x06)


def read_smeter(addr):
    return frame(addr, 0x15, 0x02)


def read_if_width(addr):
    return frame(addr, 0x1A, 0x03)


def set_freq(addr, hz):
    return frame(addr, 0x05, None, encode_freq(hz))


def set_mode(addr, mode, filt):
    """mode: LSB/USB/AM/FM/...; filt: 1..3. Leaves DATA mode as it was."""
    return frame(addr, 0x06, None, bytes([MODE_CODES[mode], filt]))


def set_data_mode(addr, on, filt):
    """DATA mode on (D1) with filter filt, or off."""
    return frame(addr, 0x1A, 0x06, bytes([1, filt]) if on else b"\x00\x00")


# ---------------------------------------------------------------- TX and menu items

def set_ptt(addr, on):
    return frame(addr, 0x1C, 0x00, b"\x01" if on else b"\x00")


def read_ptt(addr):
    return frame(addr, 0x1C, 0x00)


def read_menu(addr, item):
    """item: 2 bytes, e.g. b'\x00\x85' for 1A 05 00 85."""
    return frame(addr, 0x1A, 0x05, item)


def set_menu(addr, item, value):
    return frame(addr, 0x1A, 0x05, item + bytes(value))


# DATA MOD menu item per model (1A 05 xx xx), from each radio's CI-V reference.
# Values: 00 MIC, 01 USB, 02 ACC, 03 MIC+USB, 04 MIC+ACC, 05 LAN.
# Per-model menu items (1A 05 xx xx) and value codes, from each radio's Icom
# CI-V reference guide. They differ between models: item numbers, the order
# of the MIC/USB/ACC codes, and the LAN code itself (IC-705: 03 = WLAN).
# A model missing here, or with an item set to None, is never written to:
# the user sets DATA MOD on the radio.
MODELS = {
    "IC-7300MK2": dict(        # IC-7300MK2 CI-V reference guide
        data_mod=b"\x00\x85", data_off_mod=b"\x00\x84", lan_mod_level=b"\x00\x83",
        remote_power=b"\x01\x09", lan=0x05, tuner=True,
        mod_names={0x00: "MIC", 0x01: "USB", 0x02: "ACC", 0x03: "MIC+USB", 0x04: "MIC+ACC", 0x05: "LAN"}),
    "IC-705": dict(            # IC-705 CI-V reference guide (2020-07-21)
        data_mod=b"\x01\x19", data_off_mod=b"\x01\x18", lan_mod_level=b"\x01\x17",
        remote_power=b"\x00\x73", lan=0x03, tuner=True,
        mod_names={0x00: "MIC", 0x01: "USB", 0x02: "MIC+USB", 0x03: "WLAN"}),
    "IC-9700": dict(           # IC-9700 CI-V reference guide v2
        data_mod=b"\x01\x16", data_off_mod=b"\x01\x15", lan_mod_level=b"\x01\x14",
        remote_power=b"\x01\x46", lan=0x05, tuner=False,       # no 1C 01 on the IC-9700
        mod_names={0x00: "MIC", 0x01: "ACC", 0x02: "MIC+ACC", 0x03: "USB", 0x04: "MIC+USB", 0x05: "LAN"}),
    "IC-7610": dict(           # menu item numbers not verified yet: never written
        data_mod=None, data_off_mod=None, lan_mod_level=None, remote_power=None,
        lan=None, tuner=True, mod_names={}),
}


def model_info(radio_name):
    return MODELS.get(radio_name or "", {})


def mod_name(radio_name, value):
    """Display name of a DATA MOD / DATA OFF MOD value for this model."""
    if value is None:
        return "?"
    return model_info(radio_name).get("mod_names", {}).get(value, "%02Xh" % value)


def lan_value(radio_name):
    return model_info(radio_name).get("lan")


def has_tuner(radio_name):
    return model_info(radio_name).get("tuner", True)


# Compatibility views (IC-7300MK2 values)
DATA_MOD_ITEM = {k: v["data_mod"] for k, v in MODELS.items() if v.get("data_mod")}
DATA_OFF_MOD_ITEM = {k: v["data_off_mod"] for k, v in MODELS.items() if v.get("data_off_mod")}
LAN_MOD_LEVEL_ITEM = {k: v["lan_mod_level"] for k, v in MODELS.items() if v.get("lan_mod_level")}
DATA_MOD_LAN = 0x05
DATA_MOD_NAMES = MODELS["IC-7300MK2"]["mod_names"]


# ---------------------------------------------------------------- RF power, TX meters, tuner

def read_rf_power(addr):
    return frame(addr, 0x14, 0x0A)


def set_rf_power(addr, level):
    """level 0..255 (0 % .. 100 %)."""
    return frame(addr, 0x14, 0x0A, encode_level(level))


def read_po(addr):
    return frame(addr, 0x15, 0x11)


def read_swr(addr):
    return frame(addr, 0x15, 0x12)


TUNER_OFF, TUNER_ON, TUNER_TUNING = 0x00, 0x01, 0x02


def read_tuner(addr):
    return frame(addr, 0x1C, 0x01)


def set_tuner(addr, value):
    """0 = tuner off, 1 = tuner on (also stops a tune), 2 = start tuning."""
    return frame(addr, 0x1C, 0x01, bytes([value]))


# ---------------------------------------------------------------- power

WAKE_PREAMBLE = 25        # FE bytes before 18 01 (needed on serial/USB at 19200 bps;
                          # harmless on the LAN, where extra FEs are skipped)


def power_off(addr):
    return frame(addr, 0x18, 0x00)


def power_on(addr, preamble=WAKE_PREAMBLE):
    return b"\xfe" * preamble + frame(addr, 0x18, 0x01)


# "Power OFF Setting (for Remote Control)": 00 = Shutdown only, 01 =
# Standby/Shutdown. Read and logged only: on the owner's IC-7300MK2 it reads 00
# and the network still stays up after 18 00, and 18 01 over the LAN wakes it.
REMOTE_POWER_ITEM = {k: v["remote_power"] for k, v in MODELS.items() if v.get("remote_power")}
REMOTE_POWER_NAMES = {0x00: "Shutdown only", 0x01: "Standby/Shutdown"}
