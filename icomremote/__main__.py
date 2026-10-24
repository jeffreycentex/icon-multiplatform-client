"""Entry point:  python -m icomremote [--debug]
The step-1 connection test is still available:  python -m icomremote.probe"""

import argparse
import logging
import sys


def main():
    ap = argparse.ArgumentParser(prog="icomremote", description="Icom RS-BA1 remote client")
    ap.add_argument("--debug", action="store_true", help="verbose logging, including packet hex dumps")
    ap.add_argument("--log-file", help="also write the log to this file")
    args, qt_args = ap.parse_known_args()
    handlers = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, mode="w", encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, handlers=handlers,
                        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    try:
        from icomremote.gui import run
    except ImportError as e:
        sys.stderr.write("Missing dependency: %s\nInstall with:  pip install -r requirements.txt\n" % e)
        return 1
    return run([sys.argv[0]] + qt_args)


if __name__ == "__main__":
    sys.exit(main())
