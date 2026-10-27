# Icom Remote control API (v1)

A small HTTP/JSON interface for schedulers and scripts, such as
[FLDigi Net Scheduler](https://github.com/jeffreycentex/fldigi-net-scheduler).
It is the [KiwiSDR Remote API v1](https://github.com/jeffreycentex/kiwisdr-remote/blob/main/docs/API.md)
cut down to what makes sense for your own radio. With it, a program can:

- read the app's status: connection, radio, frequency/mode, S-meter, recording
- set the frequency and mode
- start and stop recordings of the received audio

**Not offered:** `/connect` and `/disconnect` (connect in the window; the
radio's address and login stay there), and session limits and periodic
resets (Kiwi etiquette, not needed for your own radio).

**Control only; transmit is untouched.** The API has no PTT endpoint, and
enabling it changes nothing about transmitting: the PTT button, the armed
space bar and rigctld `T 1` (WSJT-X, fldigi) work exactly as without it. The
one interaction is the other way round: an API `/tune` is refused while the
radio is on the air, so a scheduler can't move the frequency mid-transmission.

The window shows every change the API makes, and the API sees every change
made in the window, on the radio or through rigctld.

rigctld (port 4532) still does CAT and PTT for WSJT-X and fldigi. The API adds
recording and a status a scheduler can poll.

## Enabling it

In the window's *Remote control API* box, tick **Enable on 127.0.0.1**
(default port 7373, the same as KiwiSDR Remote, so a scheduler needs no
change; give one of the two apps another port if you run both). A token is
optional.

The base URL is `http://127.0.0.1:7373/api/v1`.

## Conventions

- Request and response bodies are JSON. **POST requests must send `Content-Type: application/json`**, even with an empty body (`{}`).
- Every response has `"ok": true` or `"ok": false`. Errors also carry `{"error": {"code": "...", "message": "..."}}`. The `code` values are stable; the `message` is for people.
- Frequencies are in Hz (`freq_hz`). `/tune` also accepts `freq_khz`. Times are in seconds.
- New fields may appear in v1 responses; clients should ignore keys they don't know.

| HTTP | `error.code` | When |
|---|---|---|
| 400 | `bad_request`, `bad_json` | missing or invalid parameter, frequency outside the radio's range, unsupported mode, malformed JSON |
| 401 | `unauthorized` | a token is configured, and the request's token is missing or wrong |
| 403 | `forbidden_host` | the `Host` header isn't `localhost`, `127.0.0.1` or `[::1]` |
| 404 / 405 | `not_found`, `method_not_allowed` | unknown path (including `/connect` and `/disconnect`), or wrong HTTP method |
| 409 | `conflict` | a recording is already running |
| 409 | `not_connected` | `/tune` while not connected, or the radio is in standby |
| 409 | `transmitting` | `/tune` while the radio is transmitting or the antenna tuner is tuning |
| 413 / 415 | `too_large`, `unsupported_media_type` | the body is over 64 KB, or isn't JSON |
| 500 | `io_error`, `internal_error` | the file can't be written, or an unexpected error |

## Endpoints

### `GET /api/v1`
API information: `api` (`"icom-remote"`), `api_version`, `app_version`, and the list of endpoints.

### `GET /api/v1/status`
```json
{
  "ok": true,
  "app_version": "0.9",
  "state": "connected",
  "connected": true,
  "server": "192.168.1.50:50001",
  "last_error": null,
  "radio": {"name": "IC-7300MK2", "civ_address": "B6h"},
  "power": "on",
  "tune": {"freq_hz": 7074000, "mode": "USB-D", "data": true, "filter": 1, "passband": 3000},
  "s_meter_dbm": -79.0,
  "s_meter": "S8",
  "transmitting": false,
  "session": null,
  "recording": {
    "active": true, "path": "/Users/me/Music/IcomRemote/Texas-Net_20261027T010000Z.wav",
    "seconds": 1820.4, "gap_seconds": 0.0, "waiting_for_audio": false,
    "max_seconds": 3600, "remaining_s": 1779
  },
  "audio_output": true
}
```

| `state` | Meaning |
|---|---|
| `idle` | not connected |
| `connecting` | first connection attempt in progress |
| `connected` | logged in to the radio |
| `reconnecting` | the link dropped, retrying |
| `failed` | gave up; `last_error` says why (bad user name or password) |

`power` is `on`, `standby` (network up, radio off) or `null` when not connected.
`tune` and the S-meter are `null` unless connected and powered on; the S-meter
is also `null` while transmitting. `s_meter_dbm` assumes S9 = -73 dBm (HF).
`session` is always `null` here (KiwiSDR Remote fills it).

### `POST /api/v1/tune`
Any combination of `freq_hz` or `freq_khz` (number), `mode` and `passband`.
Returns `{"tune": {...}}` as in `/status`.

| `mode` | Radio |
|---|---|
| `PKTUSB`, `PKTLSB`, `PKTAM`, `PKTFM`, `USB-D`, `LSB-D`, `AM-D`, `FM-D` | USB-D, LSB-D, AM-D, FM-D |
| `USB`, `LSB` | USB-D, LSB-D by default; plain USB/LSB if *Plain USB/LSB from rigctl or the API selects USB-D/LSB-D* is unticked (one setting for both) |
| `AM`, `SAM`, `FM` | AM, AM, FM |

- `passband` is accepted and ignored: the radio keeps its selected filter
  (FIL1/2/3). The reply's `passband` is that filter's width.
- Needs a connected, powered-on radio (`409 not_connected` otherwise). The
  request is checked first and nothing is changed when any part is invalid.
- **Refused with `409 transmitting`** while the radio transmits (from any
  source, including its own PTT) or the tuner is tuning. The check and the
  change are made together, so a key-down can't come in between.

### `POST /api/v1/record/start`
| Field | Type | |
|---|---|---|
| `path` | string | full file path. It must end in `.wav`, or have no extension, in which case `.wav` is added |
| `dir` | string | folder; the default is the window's recording folder |
| `name` | string | file name template, used when `path` isn't given. Fields: `{date}` (YYYYMMDD, UTC), `{time}` (HHMMSSZ), `{freq_khz}`, `{mode}` (e.g. `USB-D`), `{radio}` (e.g. `IC-7300MK2`), `{server}` (the radio's address). Default `icom_{date}T{time}_{freq_khz}kHz_{mode}` |
| `max_seconds` | number | stop automatically after this much wall-clock time. Recommended, so a recording ends even if the scheduler never sends stop |

- Existing files are never overwritten: `_1`, `_2`, … is appended instead.
- Characters other than letters, digits and `._+-` in a name are replaced with `_`.
- The response gives the actual path.
- The API records WAV only (16-bit mono, 48 kHz). The window can also record M4A.

A recording can start before the app is connected; it waits for audio. If
audio stops mid-recording because the connection dropped or the radio went to
standby, **silence is inserted for the gap** when audio returns (at most
30 min per gap), so a position in the file still matches the clock.
`gap_seconds` reports how much was inserted. Disconnecting in the window does
not stop a recording.

### `POST /api/v1/record/stop`
Returns `{"was_recording": bool, "recording": {"active": false, "path", "seconds", "gap_seconds", "reason"}}`.
If nothing was recording, `was_recording` is `false` and the call still
returns `ok`, so it is safe to call unconditionally.

## Security

The API is reachable only from this computer:

- It binds to `127.0.0.1`.
- With a token configured, every request needs `Authorization: Bearer <token>` (or `X-Api-Token: <token>`).
- POST requests must be `application/json`. A web page can't send that to another site without a CORS preflight, and the API never approves one, so a page in your browser can't drive the app.
- The `Host` header must be a loopback name. This blocks DNS-rebinding attacks.
- It has no endpoints for PTT, connecting, power ON/OFF or DATA MOD.

## Command-line client

```bash
python run_icomremote.py ctl status
python run_icomremote.py ctl tune --khz 7074 --mode USB-D
python run_icomremote.py ctl record-start --name "{date}_texas_net" --max-seconds 5400
python run_icomremote.py ctl record-stop
```

It takes `--host`, `--port` and `--token`; the token can also come from
`$ICOMREMOTE_API_TOKEN`. It prints the JSON response and exits with `0` (ok),
`1` (the API reported an error) or `2` (the app couldn't be reached).
(Built apps don't include it yet; run it from the source tree, or use curl.)

## Using it from Python

`icomremote/ctl.py` contains `ApiClient`, which depends only on the standard
library. KiwiSDR Remote's `ApiClient` works too, for the endpoints above.

```python
from icomremote.ctl import ApiClient

icom = ApiClient()                                   # 127.0.0.1:7373, token=None
icom.tune(freq_khz=7110, mode="LSB")                 # skip if fldigi tunes via rigctld
code, r = icom.record_start(name="{date}_texas_net", max_seconds=90 * 60)
print("recording to", r["recording"]["path"])
# ... at the end of the net:
icom.record_stop()
```

```bash
curl -s localhost:7373/api/v1/status
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"freq_khz":7110,"mode":"LSB"}' localhost:7373/api/v1/tune
```

**FLDigi Net Scheduler:** point its Remote API at this app and leave the
row's SDR setting off (there is no `/connect`); tuning and recording work as
with KiwiSDR Remote. Connect to the radio in the window before the net starts.
