# Icom Remote (multiplatform RS-BA1 client)

A cross-platform (Windows / macOS / Linux) Python client for Icom's RS-BA1 LAN
remote protocol, built for the IC-7300MK2. It should also work with the radios
kappanhang supports (IC-705, IC-9700, IC-7610, IC-785x). It talks to the radio's built-in network server directly;
no RS-BA1 software or shack PC is needed.

Planned features: receive and transmit audio through any sound device (virtual
cable to WSJT-X / fldigi), frequency and mode control, a hamlib rigctld
server, S-meter / Po / SWR, RF power, ATU tune and power on/off. The design
follows [kiwisdr-remote](https://github.com/jeffreycentex/kiwisdr-remote).

## Status: step 1, connection test

This step contains the protocol core and a command-line connection test. It
is the go/no-go check that the IC-7300MK2 speaks the same RS-BA1 dialect as
the radios kappanhang was written against. The probe:

- logs in and opens the control, serial (CI-V) and audio streams
- reads frequency, mode and DATA mode over CI-V
- receives audio, optionally plays it to a sound device and records a WAV file
- prints round-trip time, packet loss and retransmit counts once a second

| Step | Content | State |
|---|---|---|
| 1 | Login, UDP streams, RX audio, CI-V read | **this release** |
| 2 | GUI (PySide6), frequency/mode/filter control, rigctld | next |
| 3 | TX audio, PTT with safety (data modes only, timeout, unkey on disconnect), DATA MOD switch and restore | |
| 4 | S-meter, Po/SWR on TX, RF power, TUNE | |
| 5 | Power ON/OFF | |
| 6 | Fake-radio tests from real captures, PyInstaller builds | |

## Install

Requires Python 3.10 or newer.

```
git clone https://github.com/jeffreycentex/icon-multiplatform-client.git
cd icon-multiplatform-client
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

- Windows: if `pip` fails building `samplerate`, delete that line from
  `requirements.txt`; the app falls back to linear-interpolation resampling.
- Linux: PortAudio is required for sound output: `sudo apt install libportaudio2`
  (Debian/Ubuntu) or `sudo dnf install portaudio` (Fedora).

## Radio settings

In the radio's network/remote settings:

- **Network Control**: ON
- A **network user name and password** (this is what the client logs in with)
- **UDP ports** on their defaults: control 50001, serial 50002, audio 50003
- Note the radio's **IP address** (or give it a fixed one in your router)

On the IC-705 these are under `Menu > Set > WLAN Set > Remote Settings`. I
have not verified the menu path on the IC-7300MK2.

TX-related settings (DATA MOD = LAN) are not needed for this step; step 3
will switch DATA MOD to LAN while connected and put it back afterwards.

## Run the connection test

```
python -m icomremote.probe --host 192.168.1.50 -u USER -p PASSWORD
```

It stays connected for 20 seconds (`--seconds N` to change), then prints a
result:

```
RESULT: connected to IC-7300MK2 (CI-V B6h)
  CI-V:  14074.000 kHz USB-D
  Audio: 1923 packets (19.2 s of audio), lost 0
```

Exit code 0 means login, CI-V and audio all worked.

Options:

| Option | Purpose |
|---|---|
| `--play "BlackHole 2ch"` | play the received audio to an output device (name or part of it) |
| `--list-devices` | list output device names |
| `--wav rx.wav` | record the received audio |
| `--civ B6` | force the CI-V address (default: the address the radio reports) |
| `--debug` | hex-dump every control and serial packet to `icomremote-probe.log` |
| `--bind-same-ports` | use local UDP ports 50001-50003 like kappanhang does |
| `--port`, `--serial-port`, `--audio-port` | non-default radio UDP ports |

The password can also be given in the `ICOM_PASSWORD` environment variable so
it doesn't end up in your shell history.

### If it fails

1. Re-run with `--debug`. The log file `icomremote-probe.log` shows each packet
   exchanged during login. The password is blanked in the log, so it is safe to
   share. The user name is still visible (lightly encoded).
2. If the log shows the login being sent but no answer, try `--bind-same-ports`.
3. "the radio did not answer": wrong address, Network Control off, a firewall
   blocking UDP 50001-50003, or another RS-BA1 client already connected.
4. "login/stream request timeout": reboot the radio. kappanhang recommends
   this too; the radios' network stacks can get stuck after unclean disconnects.

## Tests

```
python -m unittest discover -s tests -v
```

Headless; no radio or audio hardware needed. A fake radio
(`tests/fake_rsba1.py`) implements the radio side of all three streams over
real UDP sockets. The tests cover:

- packet layouts, checked byte for byte against kappanhang, plus parsing a
  real IC-705 capabilities capture
- login, bad password, stream setup and clean disconnect
- CI-V queries and transceive broadcasts
- audio continuity, with dropped packets recovered by retransmit
- the audio timeout and reconnect

## Licensing

GPL-3.0-or-later; see [LICENSE](LICENSE). The RS-BA1 protocol code is a port of
[kappanhang](https://github.com/nonoo/kappanhang) (MIT). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Layout

```
icomremote/
  probe.py          step-1 connection test (python -m icomremote.probe)
  session.py        connection lifecycle: worker thread, reconnects, audio sinks, CI-V state
  civ.py            CI-V framing, BCD, meter scales, state decoder
  sinks.py          sound card (clock-drift locked) and WAV outputs
  resample.py       streaming resampler
  timerres.py       1 ms timer resolution on Windows while connected
  rsba1/
    packets.py      packet layouts
    passcode.py     credential encoding
    seqbuf.py       receive reorder buffer and retransmit logic
    stream.py       UDP stream base: handshake, keepalive, pings, retransmit service
    streams.py      control (login/auth), serial (CI-V) and audio streams
tests/              fake radio + unit and end-to-end tests
```
