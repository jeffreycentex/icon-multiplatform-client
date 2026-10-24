"""Windows timer resolution.

Windows' default timer tick is ~15.6 ms, so asyncio sleeps and call_later()
fire late by up to a tick. The audio stream sends a packet every ~10 ms, so
ask for 1 ms resolution while connected. No-op on other platforms.
"""

import contextlib
import sys


@contextlib.contextmanager
def high_resolution_timer():
    if sys.platform != "win32":
        yield
        return
    import ctypes
    winmm = None
    try:
        winmm = ctypes.WinDLL("winmm")
        winmm.timeBeginPeriod(1)
    except OSError:
        winmm = None
    try:
        yield
    finally:
        if winmm is not None:
            winmm.timeEndPeriod(1)
