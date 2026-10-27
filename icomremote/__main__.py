"""Entry point:  python -m icomremote [--debug] [--log-file FILE] [--check]
             python -m icomremote ctl <command> ...   control a running instance via the API
The step-1 connection test is still available:  python -m icomremote.probe

The log always goes to icomremote.log in the app's settings folder as well
(see restore.default_dir); the previous sessions are kept as icomremote.log.1
.. .5, so a session that misbehaved is still there after a restart.
"""

import argparse
import logging
import os
import platform
import sys

from . import __version__


def _frozen():
    return bool(getattr(sys, "frozen", False))


def default_log_path():
    from .restore import default_dir
    return os.path.join(default_dir(), "icomremote.log")


KEEP_LOGS = 5


def rotate_logs(path, keep=KEEP_LOGS):
    """icomremote.log -> .1 -> .2 ... (oldest dropped)."""
    for i in range(keep - 1, 0, -1):
        older = "%s.%d" % (path, i)
        if os.path.exists(older):
            os.replace(older, "%s.%d" % (path, i + 1))
    if os.path.exists(path):
        os.replace(path, path + ".1")


def check():
    """Report what this build/installation can do. Exit code 0 = everything required works."""
    lines = ["Icom Remote %s, Python %s, %s %s%s" % (
        __version__, platform.python_version(), platform.system(), platform.machine(),
        " (built app)" if _frozen() else "")]
    ok = True

    def item(name, fn, required=True):
        nonlocal ok
        try:
            detail = fn()
            lines.append("  OK    %-12s %s" % (name, detail or ""))
        except Exception as e:  # report, don't crash
            lines.append("  %s %-12s %s" % ("FAIL " if required else "n/a  ", name, e))
            if required:
                ok = False

    def qt():
        if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY")
                                                     or os.environ.get("WAYLAND_DISPLAY")):
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")       # headless CI
        import PySide6
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([sys.argv[0]])
        return "PySide6 %s, platform '%s'" % (PySide6.__version__, app.platformName())

    def audio():
        from .sinks import HAVE_SOUNDDEVICE, sounddevice_error
        if not HAVE_SOUNDDEVICE:
            raise RuntimeError(sounddevice_error())
        import sounddevice as sd
        devs = sd.query_devices()
        n_out = sum(1 for d in devs if d["max_output_channels"] > 0)
        n_in = sum(1 for d in devs if d["max_input_channels"] > 0)
        return "%s, %d output / %d input devices" % (sd.get_portaudio_version()[1], n_out, n_in)

    def pyav():
        from .sinks import HAVE_PYAV
        if not HAVE_PYAV:
            raise RuntimeError("PyAV not installed: M4A/MP4 recording unavailable")
        import av
        av.codec.Codec("aac", "w")
        return "PyAV %s, AAC encoder present" % av.__version__

    def resampler():
        from .resample import HAVE_LIBSAMPLERATE
        return "libsamplerate" if HAVE_LIBSAMPLERATE else "linear (samplerate not installed; fine at 48 kHz)"

    def protocol():
        from .rsba1.passcode import encode
        assert encode("beer")[:4] == bytes([0x2b, 0x3f, 0x55, 0x5c])
        from . import civ, rigctld, session  # noqa: F401
        return "RS-BA1 / CI-V / rigctld modules load"

    item("Qt GUI", qt)
    item("Audio", audio)
    item("Recording", pyav, required=False)
    item("Resampler", resampler, required=False)
    item("Protocol", protocol)
    lines.append("RESULT: %s" % ("OK" if ok else "FAILED"))
    text = "\n".join(lines)
    print(text)                                  # no-op in a windowed build (stdout is None)
    if _frozen():
        logging.getLogger("icomremote").info("self-check:\n%s", text)
    return 0 if ok else 1


def main():
    if sys.argv[1:2] == ["ctl"]:               # API client: no log rotation, no Qt
        from .ctl import main as ctl_main
        return ctl_main(sys.argv[2:])
    ap = argparse.ArgumentParser(prog="icomremote", description="Icom RS-BA1 remote client")
    ap.add_argument("--debug", action="store_true", help="verbose logging, including packet hex dumps")
    ap.add_argument("--log-file", help="also write the log to this file")
    ap.add_argument("--check", action="store_true", help="report bundled components and exit")
    ap.add_argument("--version", action="version", version="Icom Remote %s" % __version__)
    args, qt_args = ap.parse_known_args()

    handlers = []
    if sys.stderr is not None:                 # None in a windowed Windows build
        handlers.append(logging.StreamHandler())
    log_files = [default_log_path()]
    if args.log_file and os.path.abspath(args.log_file) != os.path.abspath(log_files[0]):
        log_files.append(args.log_file)
    for i, log_file in enumerate(log_files):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
            if i == 0 and not args.check:
                rotate_logs(log_file)
            handlers.append(logging.FileHandler(log_file, mode="a" if args.check else "w",
                                                encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, handlers=handlers or None,
                        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("icomremote").info("Icom Remote %s starting (%s %s, Python %s), log: %s", __version__,
                                         platform.system(), platform.release(), platform.python_version(),
                                         log_files[0])

    if args.check:
        return check()
    try:
        from icomremote.gui import run
    except ImportError as e:
        sys.stderr.write("Missing dependency: %s\nInstall with:  pip install -r requirements.txt\n" % e)
        return 1
    return run([sys.argv[0]] + qt_args)


if __name__ == "__main__":
    sys.exit(main())
