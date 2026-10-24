"""Remembers radio settings the app changed, so they can be put back.

Written to disk (atomically) *before* a setting is changed on the radio. If
the app is killed, the PC loses power or the network drops, the record
survives and the next connection to that radio restores the original value.

File: <config dir>/restore.json   {"IC-7300MK2@10.0.50.99": {"data_mod": 1}}
"""

import json
import logging
import os
import sys
import threading

log = logging.getLogger(__name__)


def default_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "IcomRemote")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/IcomRemote")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "icomremote")


class RestoreStore:
    def __init__(self, directory=None):
        self.dir = directory or default_dir()
        self.path = os.path.join(self.dir, "restore.json")
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("cannot read %s: %s", self.path, e)
            return {}

    def _save(self, data):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def get(self, radio, setting):
        with self._lock:
            return self._load().get(radio, {}).get(setting)

    def put(self, radio, setting, value):
        with self._lock:
            data = self._load()
            data.setdefault(radio, {})[setting] = value
            self._save(data)

    def clear(self, radio, setting):
        with self._lock:
            data = self._load()
            entry = data.get(radio, {})
            if setting in entry:
                del entry[setting]
                if not entry:
                    data.pop(radio, None)
                self._save(data)

    def pending(self):
        """{radio: {setting: original value}} still waiting to be restored."""
        with self._lock:
            return self._load()
