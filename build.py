#!/usr/bin/env python3
"""Build a standalone Icom Remote for this OS and zip it.

    python build.py              install PyInstaller + requirements, build, self-check, archive
    python build.py --no-install skip pip (CI, or when already installed)
    python build.py --console    Windows: keep a console window (troubleshooting)

Output in dist/:
    macOS    Icom Remote.app      + IcomRemote-<ver>-macos-<arch>.zip
    Windows  Icom Remote\\         + IcomRemote-<ver>-windows-<arch>.zip
    Linux    Icom Remote/          + IcomRemote-<ver>-linux-<arch>.tar.gz

Run it from a virtual environment. PyInstaller does not cross-compile.
"""

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")


def run(cmd, **kw):
    print("+", " ".join('"%s"' % c if " " in c else c for c in cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT, **kw)


def version():
    with open(os.path.join(ROOT, "icomremote", "__init__.py"), encoding="utf-8") as f:
        return re.search(r'__version__ = "([^"]+)"', f.read()).group(1)


def os_tag():
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")


def executable():
    if sys.platform == "darwin":
        return os.path.join(DIST, "Icom Remote.app", "Contents", "MacOS", "Icom Remote")
    name = "Icom Remote.exe" if sys.platform == "win32" else "Icom Remote"
    return os.path.join(DIST, "Icom Remote", name)


def self_check():
    """Run the built app with --check; it reports Qt, PortAudio, PyAV in the bundle."""
    log = os.path.join(ROOT, "build", "check.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    if os.path.exists(log):
        os.remove(log)
    env = dict(os.environ)
    if sys.platform.startswith("linux") and not env.get("DISPLAY"):
        env["QT_QPA_PLATFORM"] = "offscreen"
    r = run([executable(), "--check", "--log-file", log], env=env, timeout=120)
    if os.path.exists(log):                      # a windowed Windows build has no stdout
        with open(log, encoding="utf-8") as f:
            text = f.read()
        if "self-check" in text:
            print(text[text.index("self-check"):])
    if os.environ.get("GITHUB_ACTIONS") and os.path.exists(log):    # readable via the API
        with open(log, encoding="utf-8") as f:
            text = f.read()
        body = text[text.index("self-check"):] if "self-check" in text else text[-3000:]
        print("::%s title=built app --check (exit %d)::%s" % (
            "notice" if r.returncode == 0 else "error", r.returncode,
            body.replace("%", "%25").replace("\r", "").replace("\n", "%0A")))
    if r.returncode != 0:
        sys.exit("self-check FAILED (exit %d)" % r.returncode)


def archive():
    base = "IcomRemote-%s-%s-%s" % (version(), os_tag(), platform.machine().lower())
    if sys.platform == "darwin":
        out = os.path.join(DIST, base + ".zip")
        if os.path.exists(out):
            os.remove(out)
        # ditto keeps the bundle's symlinks and code signature intact
        run(["ditto", "-c", "-k", "--keepParent", os.path.join(DIST, "Icom Remote.app"), out], check=True)
    elif sys.platform == "win32":
        out = shutil.make_archive(os.path.join(DIST, base), "zip", DIST, "Icom Remote")
    else:
        out = shutil.make_archive(os.path.join(DIST, base), "gztar", DIST, "Icom Remote")
    print("\nBuilt: %s (%.0f MB)" % (out, os.path.getsize(out) / 1e6))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-install", action="store_true", help="don't pip install anything")
    ap.add_argument("--console", action="store_true", help="Windows: build with a console window")
    ap.add_argument("--no-check", action="store_true", help="skip the self-check of the built app")
    args = ap.parse_args()

    if sys.prefix == sys.base_prefix and not os.environ.get("CI"):
        print("WARNING: not running in a virtual environment; the build will bundle whatever "
              "is installed globally.")
    if not args.no_install:
        run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "pyinstaller"], check=True)
    env = dict(os.environ)
    if args.console:
        env["ICOMREMOTE_CONSOLE"] = "1"
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "icomremote.spec"],
        check=True, env=env)
    if not args.no_check:
        self_check()
    archive()


if __name__ == "__main__":
    main()
