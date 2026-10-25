"""Minimal hamlib rigctld (NET rigctl, hamlib model 2) server.

Lets WSJT-X, fldigi, JS8Call, GridTracker, Log4OM, `rigctl -m 2`, etc. read
and set the frequency and mode of the radio. Adapted from kiwisdr-remote.
It runs on its own threads and talks to the radio only through the
RadioSession control API.

Supported commands (short and long forms, plain and '+' extended responses):
  f get_freq            F set_freq <Hz>
  m get_mode            M set_mode <mode> <passband>   (passband ignored: the
                                                        radio's filter stays)
  v get_vfo             V set_vfo <vfo>
  t get_ptt             T set_ptt <0|1>        (refused unless the radio is in a DATA
                                                mode; RPRT -9 with a log line)
  s get_split_vfo       S set_split_vfo        (always split off)
  l get_level <LEVEL>   STRENGTH = S-meter in dB relative to S9
  L set_level           accepted, ignored
  \\dump_state  \\chk_vfo  \\get_powerstat  \\get_lock_mode  q/Q
A leading VFO argument ('f VFOA', 'l VFOA STRENGTH') is accepted and ignored.

PTT keyed by a client is released when that client disconnects.

Modes: PKTUSB/PKTLSB/PKTAM/PKTFM select the radio's DATA modes. Plain
USB/LSB also select DATA (USB-D/LSB-D) unless plain_ssb_is_data is off.
While disconnected, reads return the last known state and sets fail (RPRT -6).
"""

import logging
import socket
import socketserver
import threading

from . import civ
from .session import PttRefused
from .state import HAMLIB_BITS, hamlib_mode, rig_mode_from_hamlib

log = logging.getLogger(__name__)

E_INVAL = -1
E_IO = -6
E_REJECTED = -9
E_NAVAIL = -11

# short char -> (long name, takes VFO-optional args)
LONG = {
    "f": "get_freq", "F": "set_freq", "m": "get_mode", "M": "set_mode",
    "v": "get_vfo", "V": "set_vfo", "t": "get_ptt", "T": "set_ptt",
    "s": "get_split_vfo", "S": "set_split_vfo", "l": "get_level", "L": "set_level",
    "q": "quit", "Q": "quit",
}
SHORT = {v: k for k, v in LONG.items()}

# hamlib VFO names (rig_strvfo). Clients in VFO mode (fldigi, hamlib >= 4)
# put one in front of the arguments of most commands: 'f VFOA', 'l VFOA STRENGTH'.
VFO_NAMES = {"VFOA", "VFOB", "VFOC", "CURRVFO", "VFO", "MEM", "MAIN", "SUB", "TX", "RX",
             "MAINA", "MAINB", "MAINC", "SUBA", "SUBB", "SUBC", "NONE"}


def _is_vfo(tok):
    return tok.upper() in VFO_NAMES


def _dump_state(lo_hz, hi_hz):
    """Format expected by hamlib netrigctl_open(); layout from the kiwiclient
    rigctld emulation (Rik van Riel) as used in kiwisdr-remote."""
    allm = sum(HAMLIB_BITS.values())
    ssb = HAMLIB_BITS["USB"] | HAMLIB_BITS["LSB"] | HAMLIB_BITS["PKTUSB"] | HAMLIB_BITS["PKTLSB"]
    am = HAMLIB_BITS["AM"] | HAMLIB_BITS["PKTAM"]
    fm = HAMLIB_BITS["FM"] | HAMLIB_BITS["PKTFM"]
    lines = [
        "0",                    # protocol version
        "2",                    # rig model (NET rigctl)
        "0",                    # ITU region
        "%f %f 0x%x -1 -1 0x1 0x1" % (float(lo_hz), float(hi_hz), allm),
        "0 0 0 0 0 0 0",        # end rx ranges
        "%f %f 0x%x 1000 100000 0x1 0x1" % (float(max(lo_hz, 1_800_000)), float(hi_hz), allm),
        "0 0 0 0 0 0 0",        # end tx ranges
    ]
    for step in (1, 10, 100, 1000, 5000, 9000, 10000):
        lines.append("0x%x %d" % (allm, step))
    lines.append("0 0")
    lines += ["0x%x 3000" % ssb, "0x%x 2400" % ssb, "0x%x 1800" % ssb,
              "0x%x 9000" % am, "0x%x 6000" % am, "0x%x 3000" % am,
              "0x%x 15000" % fm, "0x%x 10000" % fm, "0x%x 7000" % fm, "0 0"]
    lines += ["0", "0", "0", "0"]   # max_rit max_xit max_ifshift announces
    lines += ["", ""]               # preamp, attenuator lists
    lines += ["0x0"] * 6            # get/set func, level, parm
    lines += ["vfo_ops=0x0", "ptt_type=0x1", "done"]   # RIG_PTT_RIG: PTT via CAT
    return "\n".join(lines) + "\n"


class _Handler(socketserver.StreamRequestHandler):
    timeout = None

    def handle(self):
        srv = self.server.owner
        peer = "%s:%s" % self.client_address[:2]
        srv._client_count(+1, peer)
        try:
            for raw in self.rfile:
                try:
                    text = raw.decode("ascii", "replace").strip("\r\n")
                except Exception:
                    continue
                if not text.strip():
                    continue
                reply, close = srv.handle_line(text, client=self)
                if reply:
                    self.wfile.write(reply.encode("ascii", "replace"))
                    self.wfile.flush()
                if close:
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            srv._client_gone(self)
            srv._client_count(-1, peer)


class _TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _TCPServer6(_TCPServer):
    address_family = socket.AF_INET6


class RigctldServer:
    def __init__(self, controller, host="127.0.0.1", port=4532, on_activity=None,
                 plain_ssb_is_data=True):
        self.controller = controller
        self.plain_ssb_is_data = plain_ssb_is_data
        self.host = host
        self.port = int(port)
        self._on_activity = on_activity or (lambda kind, payload: None)
        self._server = None
        self._thread = None
        self._clients = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        cls = _TCPServer6 if ":" in self.host else _TCPServer
        self._server = cls((self.host, self.port), _Handler)
        self._server.owner = self
        self.port = self._server.server_address[1]   # resolves port 0 in tests
        self._thread = threading.Thread(target=self._server.serve_forever, name="rigctld", daemon=True)
        self._thread.start()
        log.info("rigctld listening on %s:%d", self.host, self.port)

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None

    @property
    def running(self):
        return self._server is not None

    @property
    def clients(self):
        return self._clients

    def _client_gone(self, client):
        try:
            self.controller.release_ptt(client)
        except Exception:
            log.exception("releasing PTT failed")

    def _client_count(self, delta, peer):
        with self._lock:
            self._clients += delta
            n = self._clients
        self._on_activity("clients", (n, peer, delta > 0))

    # ------------------------------------------------------------------ protocol
    def handle_line(self, line, client=None):
        """Process one input line (may hold several short commands). Returns (reply, close)."""
        line = line.strip()
        extended = False
        sep = "\n"
        if line[:1] in "+;|,":
            extended = True
            if line[0] in ";|,":
                sep = line[0]
            line = line[1:].strip()

        if line.startswith("\\"):
            parts = line[1:].split()
            cmds = [(parts[0], parts[1:])] if parts else []
        else:
            cmds = self._split_short(line)

        out = []
        close = False
        for name, args in cmds:
            long_name = LONG.get(name, name)
            if long_name == "quit":
                close = True
                break
            if long_name != "set_vfo" and args and _is_vfo(args[0]):
                args = args[1:]           # single VFO: the target VFO doesn't matter
            try:
                values, rc = self._dispatch(long_name, args, client)
            except Exception as e:
                log.debug("rigctl %s %s failed: %s", long_name, args, e)
                values, rc = [], -1
            if values is None:            # raw payload (dump_state, chk_vfo)
                out.append(rc)
            elif extended:
                hdr = "%s:" % long_name + (" " + " ".join(args) if args else "")
                body = [hdr] + ["%s: %s" % kv if isinstance(kv, tuple) else str(kv) for kv in values]
                body.append("RPRT %d" % rc)
                out.append(sep.join(body) + "\n")
            elif values:
                if rc != 0:
                    out.append("RPRT %d\n" % rc)
                else:
                    out.append("".join("%s\n" % (v[1] if isinstance(v, tuple) else v) for v in values))
            else:
                out.append("RPRT %d\n" % rc)
        return "".join(out), close

    @staticmethod
    def _split_short(line):
        """'F 7074000 M USB 2400 f' -> [('F',['7074000']), ('M',['USB','2400']), ('f',[])]"""
        tokens = line.split()
        cmds = []
        nargs = {"F": 1, "M": 2, "V": 1, "T": 1, "S": 2, "l": 1, "L": 2}
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if len(tok) == 1 and tok in LONG:
                n = nargs.get(tok, 0)
                # a leading VFO argument ('F VFOA 7074000', 'f VFOA') belongs to the
                # command; for 'V' the VFO is the argument itself
                if tok != "V" and i + 1 < len(tokens) and _is_vfo(tokens[i + 1]):
                    n += 1
                args = tokens[i + 1:i + 1 + n]
                cmds.append((tok, args))
                i += 1 + len(args)
            else:
                cmds.append((tok, tokens[i + 1:]))
                break
        return cmds

    def _dispatch(self, cmd, args, client=None):
        c = self.controller
        st = c.rig
        known = st.freq_hz > 0
        if cmd == "get_freq":
            return ([("Frequency", "%d" % st.freq_hz)], 0) if known else ([], E_IO)
        if cmd == "set_freq":
            hz = float(args[0])
            if not c.connected or c.power != "on":
                return [], E_IO
            try:
                c.set_frequency(hz)
            except ValueError as e:
                log.info("rigctl: %s", e)
                return [], E_INVAL
            self._on_activity("command", "F %.0f" % hz)
            return [], 0
        if cmd == "get_mode":
            if not st.mode:
                return [], E_IO
            return [("Mode", hamlib_mode(st.mode, st.data)), ("Passband", "%d" % st.width_hz)], 0
        if cmd == "set_mode":
            want = rig_mode_from_hamlib(args[0], self.plain_ssb_is_data)
            if want is None:
                log.info("rigctl: unsupported mode %s", args[0])
                return [], E_NAVAIL
            if not c.connected or c.power != "on":
                return [], E_IO
            if (st.mode, st.data) != want:          # passband ignored: keep the radio's filter
                c.set_mode(want[0], want[1])
            self._on_activity("command", "M %s" % args[0])
            return [], 0
        if cmd == "get_vfo":
            return [("VFO", "VFOA")], 0
        if cmd == "set_ptt":
            on = bool(args) and args[0] != "0"
            try:
                c.set_ptt(on, owner=client)
            except PttRefused as e:
                log.info("rigctl: %s", e)
                self._on_activity("refused", str(e))
                return [], E_REJECTED
            self._on_activity("command", "T %d" % on)
            return [], 0
        if cmd in ("set_vfo", "set_split_vfo", "set_level", "set_func", "set_parm",
                   "set_powerstat", "set_lock_mode", "set_rit", "set_xit"):
            return [], 0
        if cmd == "get_ptt":
            return [("PTT", "1" if c.ptt else "0")], 0
        if cmd == "get_split_vfo":
            return [("Split", "0"), ("TX VFO", "VFOA")], 0
        if cmd == "get_level":
            level = (args[0] if args else "").upper()
            if level == "STRENGTH":
                raw = c.meters.get("smeter")
                db = civ.s_meter_db(raw) if raw is not None and c.connected else -54.0
                return [("Level", "%d" % round(db))], 0
            return [("Level", "0")], 0
        if cmd == "dump_state":
            lo, hi = c.freq_limits
            return None, _dump_state(lo, hi)
        if cmd == "chk_vfo":
            return None, "CHKVFO 0\n"     # same reply kiwiclientd uses with WSJT-X/fldigi
        if cmd == "get_powerstat":
            # hamlib: 0 off, 1 on, 2 standby. set_powerstat is accepted but ignored:
            # programs don't switch the radio on or off.
            return [("Power Status", {"on": "1", "standby": "2"}.get(c.power, "0"))], 0
        if cmd == "get_lock_mode":
            return [("Locked", "0")], 0
        if cmd in ("get_rit", "get_xit"):
            return [("RIT", "0")], 0
        if cmd == "get_info":
            return [("Info", "Icom Remote")], 0
        log.info("rigctl: unsupported command %r %s", cmd, args)
        return [], E_NAVAIL
