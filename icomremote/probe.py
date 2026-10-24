"""Step-1 connection test: log in to an RS-BA1 radio, receive audio, read the
frequency and mode over CI-V, and print what happened.

    python -m icomremote.probe --host 192.168.1.50 -u USER -p PASS
    python -m icomremote.probe --host ic-7300mk2.local -u USER -p PASS --play "BlackHole 2ch"
    python -m icomremote.probe --list-devices

Exit code 0 = logged in and audio flowed; non-zero otherwise. With --debug,
every control/serial packet is hex-dumped to the log (send that log file if
the login fails).
"""

import argparse
import logging
import os
import sys
import threading
import time

from .session import ConnectionSpec, RadioSession


def _parse_addr(text):
    if text is None:
        return None
    t = text.strip().lower().rstrip("h")
    if t.startswith("0x"):
        t = t[2:]
    v = int(t, 16)
    if not 0 < v < 0xE0:
        raise argparse.ArgumentTypeError("CI-V address must be 01h-DFh")
    return v


def _find_device(name):
    from .sinks import list_output_devices
    devices = list_output_devices()
    low = name.lower()
    for key, label in devices:
        if key == name or label.lower() == low:
            return key
    for key, label in devices:
        if low in label.lower():
            return key
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m icomremote.probe",
                                 description="RS-BA1 connection test (login, CI-V, RX audio)")
    ap.add_argument("--host", help="radio IP address or host name")
    ap.add_argument("-u", "--user", help="network user name set in the radio")
    ap.add_argument("-p", "--password", help="network password set in the radio "
                    "(or set ICOM_PASSWORD in the environment)")
    ap.add_argument("--civ", type=_parse_addr, default=None,
                    help="CI-V address in hex, e.g. B6 (default: what the radio reports)")
    ap.add_argument("--port", type=int, default=50001, help="control UDP port (default 50001)")
    ap.add_argument("--serial-port", type=int, default=50002)
    ap.add_argument("--audio-port", type=int, default=50003)
    ap.add_argument("--bind-same-ports", action="store_true",
                    help="use local UDP ports 50001-50003 like kappanhang (try this if login stalls)")
    ap.add_argument("--seconds", type=float, default=20.0, help="how long to stay connected")
    ap.add_argument("--play", metavar="DEVICE", help="play received audio to this output device "
                    "(name or part of it, see --list-devices)")
    ap.add_argument("--wav", metavar="FILE", help="also record received audio to a WAV file")
    ap.add_argument("--list-devices", action="store_true", help="list audio output devices and exit")
    ap.add_argument("--debug", action="store_true", help="hex-dump control/serial packets")
    ap.add_argument("--log-file", help="write the log here as well (default with --debug: "
                    "icomremote-probe.log)")
    args = ap.parse_args(argv)

    level = logging.DEBUG if args.debug else logging.INFO
    handlers = [logging.StreamHandler()]
    log_file = args.log_file or ("icomremote-probe.log" if args.debug else None)
    if log_file:
        handlers.append(logging.FileHandler(log_file, mode="w", encoding="utf-8"))
    logging.basicConfig(level=level, handlers=handlers,
                        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")

    if args.list_devices:
        from .sinks import HAVE_SOUNDDEVICE, list_output_devices, sounddevice_error
        if not HAVE_SOUNDDEVICE:
            print("sounddevice/PortAudio not available: %s" % sounddevice_error())
            return 2
        for key, label in list_output_devices():
            print(label)
        return 0

    password = args.password if args.password is not None else os.environ.get("ICOM_PASSWORD")
    if not args.host or not args.user or password is None:
        ap.error("--host, --user and --password are required")

    out = sys.stdout
    done = threading.Event()
    result = {"connected": False, "rig": None, "radio": None, "last_error": None}

    def on_event(kind, payload):
        if kind == "status":
            text, lvl = payload
            if lvl == "error":
                result["last_error"] = text
                if "Login failed" in text:
                    done.set()
        elif kind == "connected" and payload:
            result["connected"] = True
        elif kind == "radio":
            result["radio"] = payload
        elif kind == "rig" and payload.mode:
            result["rig"] = payload
            print("  rig: %.3f kHz  %s  FIL%d" % (payload.freq_hz / 1e3, payload.mode_label,
                                                  payload.filter), file=out, flush=True)
        elif kind == "stopped":
            done.set()

    session = RadioSession(on_event)
    if args.play:
        key = _find_device(args.play)
        if key is None:
            print("Audio device %r not found; use --list-devices" % args.play)
            return 2
        session.set_audio_device(key)
    if args.wav:
        session.start_recording(args.wav)

    spec = ConnectionSpec(args.host, args.user, password, args.port, args.serial_port,
                          args.audio_port, args.civ, bind_same_ports=args.bind_same_ports)
    session.connect(spec)
    t_end = time.monotonic() + args.seconds
    last = {}
    try:
        while time.monotonic() < t_end and not done.is_set():
            time.sleep(1.0)
            st = session.stats()
            if st.get("connected"):
                last = st
                meter = session.meters.get("smeter")
                from .civ import s_meter_db, s_meter_text
                sm = s_meter_text(s_meter_db(meter)) if meter is not None else "--"
                print("  up %4.0fs  rtt %5.1f ms  audio pkts %6d  lost %d  retx-req %d  "
                      "S %-6s rx %.0f kB" % (st.get("uptime", 0), st.get("rtt_ms", 0),
                                             st.get("audio_packets", 0), st.get("audio_lost", 0),
                                             st.get("audio_retx_requests", 0), sm,
                                             st.get("rx_bytes", 0) / 1024), file=out, flush=True)
    except KeyboardInterrupt:
        pass
    final = session.stats()
    if "audio_packets" not in final:
        final = last
    session.shutdown()

    print()
    radio = result["radio"]
    if not result["connected"]:
        print("RESULT: FAILED - %s" % (result["last_error"] or "did not connect"))
        if not args.debug:
            print("Re-run with --debug and send icomremote-probe.log.")
        return 1
    print("RESULT: connected to %s (CI-V %02Xh%s)" % (
        radio["name"], radio["civ_address"],
        "" if radio["reported_civ"] in (None, radio["civ_address"])
        else ", radio reports %02Xh" % radio["reported_civ"]))
    rig = result["rig"]
    print("  CI-V:  %s" % ("%.3f kHz %s" % (rig.freq_hz / 1e3, rig.mode_label) if rig
                          else "no answer to frequency/mode queries"))
    pk = final.get("audio_packets", 0)
    print("  Audio: %d packets (%.1f s of audio), lost %d" % (pk, final.get("audio_seconds", 0.0),
                                                              final.get("audio_lost", 0)))
    if pk == 0:
        print("RESULT: logged in, but no audio was received")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
