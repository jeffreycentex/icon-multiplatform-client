"""Modes, rigctl mode mapping, and per-radio limits."""

# Modes offered in the UI: label -> (CI-V mode, DATA on)
UI_MODES = {
    "LSB": ("LSB", False), "USB": ("USB", False), "AM": ("AM", False), "FM": ("FM", False),
    "LSB-D": ("LSB", True), "USB-D": ("USB", True), "AM-D": ("AM", True), "FM-D": ("FM", True),
}
BASE_MODES = ("LSB", "USB", "AM", "FM")
FILTERS = (1, 2, 3)

# hamlib mode name -> (CI-V mode, DATA on). Plain USB/LSB are handled by
# rig_mode_from_hamlib() because of the 'plain SSB means data' option.
_HAMLIB_IN = {
    "PKTUSB": ("USB", True), "USBD": ("USB", True), "USB-D": ("USB", True),
    "PKTLSB": ("LSB", True), "LSBD": ("LSB", True), "LSB-D": ("LSB", True),
    "PKTAM": ("AM", True), "PKTFM": ("FM", True),
    "AM": ("AM", False), "FM": ("FM", False), "SAM": ("AM", False), "AMS": ("AM", False),
}

# hamlib RIG_MODE_* bit flags (rig.h) for dump_state
HAMLIB_BITS = {"AM": 1 << 0, "USB": 1 << 2, "LSB": 1 << 3, "FM": 1 << 5,
               "PKTLSB": 1 << 10, "PKTUSB": 1 << 11, "PKTFM": 1 << 12, "PKTAM": 1 << 22}


def rig_mode_from_hamlib(name, plain_ssb_is_data=True):
    """-> (civ_mode, data) or None if the mode isn't offered."""
    m = name.strip().upper()
    if m in ("USB", "LSB"):
        return m, plain_ssb_is_data
    return _HAMLIB_IN.get(m)


def hamlib_mode(civ_mode, data):
    if civ_mode not in BASE_MODES:
        return civ_mode or "USB"
    return ("PKT" + civ_mode) if data else civ_mode


def ui_label(civ_mode, data):
    return civ_mode + ("-D" if data else "")


# Receive range (Hz) per model, used to validate tuning requests.
FREQ_LIMITS = {
    "IC-7300MK2": (30_000, 74_800_000),
    "IC-7300": (30_000, 74_800_000),
    "IC-705": (30_000, 470_000_000),
    "IC-9700": (136_000_000, 1_320_000_000),     # 2 m, 70 cm, 23 cm (receive ranges)
    "IC-7610": (30_000, 60_000_000),
}
DEFAULT_LIMITS = (30_000, 470_000_000)


def freq_limits(radio_name):
    return FREQ_LIMITS.get(radio_name or "", DEFAULT_LIMITS)


# Radio profiles in the GUI: label -> CI-V address (None = use what the radio reports)
PROFILES = {
    "Auto (from radio)": None,
    "IC-7300MK2 (B6h)": 0xB6,
    "IC-705 (A4h)": 0xA4,
    "IC-9700 (A2h)": 0xA2,
    "IC-7610 (98h)": 0x98,
}
