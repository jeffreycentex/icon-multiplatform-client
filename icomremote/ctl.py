"""Command-line client for the Icom Remote API (stdlib only; from kiwisdr-remote).

    python run_icomremote.py ctl status
    python run_icomremote.py ctl tune --khz 7074 --mode USB-D
    python run_icomremote.py ctl record-start --name "{date}_net" --max-seconds 3600
    python run_icomremote.py ctl record-stop

Prints the JSON response. Exit codes: 0 ok, 1 the API reported an error,
2 the API could not be reached.  --token or $ICOMREMOTE_API_TOKEN supplies a token.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from .api import DEFAULT_PORT, PREFIX
from .service import API_MODES


class ApiClient:
    """Tiny client usable from other Python programs (e.g. FLDigi Net Scheduler)."""

    def __init__(self, host="127.0.0.1", port=DEFAULT_PORT, token=None, timeout=10.0):
        h = "[%s]" % host if ":" in host else host
        self.base = "http://%s:%d%s" % (h, port, PREFIX)
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via a proxy

    def request(self, method, path, body=None):
        """Returns (http_status, response dict). Raises OSError if unreachable."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                try:
                    return e.code, json.loads(e.read() or b"{}")
                except ValueError:
                    return e.code, {"ok": False, "error": {"code": "http_%d" % e.code, "message": str(e)}}

    def info(self):
        return self.request("GET", "")

    def status(self):
        return self.request("GET", "/status")

    def tune(self, **kw):
        return self.request("POST", "/tune", kw)

    def record_start(self, **kw):
        return self.request("POST", "/record/start", kw)

    def record_stop(self):
        return self.request("POST", "/record/stop", {})


def _drop_none(d):
    return {k: v for k, v in d.items() if v is not None}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="icomremote ctl", description="Control a running Icom Remote")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--token", default=os.environ.get("ICOMREMOTE_API_TOKEN"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info")
    sub.add_parser("status")
    t = sub.add_parser("tune")
    g = t.add_mutually_exclusive_group()
    g.add_argument("--hz", type=float)
    g.add_argument("--khz", type=float)
    t.add_argument("--mode", type=str.upper, choices=API_MODES)
    r = sub.add_parser("record-start")
    r.add_argument("--path")
    r.add_argument("--dir")
    r.add_argument("--name", help="template: {date} {time} {freq_khz} {mode} {radio} {server}")
    r.add_argument("--max-seconds", type=float)
    sub.add_parser("record-stop")
    a = ap.parse_args(argv)

    cl = ApiClient(a.host, a.port, a.token)
    try:
        if a.cmd == "info":
            code, resp = cl.info()
        elif a.cmd == "status":
            code, resp = cl.status()
        elif a.cmd == "tune":
            code, resp = cl.tune(**_drop_none({"freq_hz": a.hz, "freq_khz": a.khz, "mode": a.mode}))
        elif a.cmd == "record-start":
            code, resp = cl.record_start(**_drop_none({"path": a.path, "dir": a.dir, "name": a.name,
                                                       "max_seconds": a.max_seconds}))
        else:
            code, resp = cl.record_stop()
    except OSError as e:
        sys.stderr.write("cannot reach Icom Remote at %s: %s\n" % (cl.base, getattr(e, "reason", e)))
        return 2
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
