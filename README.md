# Icom Remote (multiplatform RS-BA1 client)

A cross-platform (Windows / macOS / Linux) desktop client for Icom's RS-BA1 LAN
remote protocol, built for the IC-7300MK2. It should also work with the
radios kappanhang supports (IC-705, IC-9700, IC-7610, IC-785x). It talks to
the radio's built-in network server directly; no RS-BA1 software or shack PC
is needed. The design follows
[kiwisdr-remote](https://github.com/jeffreycentex/kiwisdr-remote).

```
                ┌──────────────────── Icom Remote ────────────────────┐
IC-7300MK2 ◄udp─┤ session thread (asyncio): control / serial / audio  │
  50001-50003   │   CI-V set/read ◄── GUI (PySide6) ◄── rigctld :4532 ◄┼── WSJT-X / fldigi / rigctl
                │   RX audio 48 kHz ──► SoundCardSink (drift-locked) ──┼─► BlackHole / VB-Cable / Pulse sink
                └─────────────────────────────────────────────────────┘
```

## Status

| Step | Content | State |
|---|---|---|
| 1 | Login, UDP streams, RX audio, CI-V read | done, verified on an IC-7300MK2 |
| 2 | GUI, frequency/mode/filter control, S-meter, rigctld | **this release** |
| 3 | TX audio, PTT with safety (data modes only, timeout, unkey on disconnect), DATA MOD switch and restore | next |
| 4 | Po/SWR on TX, RF power, TUNE | |
| 5 | Power ON/OFF | |
| 6 | PyInstaller builds | |

Receive only for now: PTT requests are refused until step 3.

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
- Linux: PortAudio is required: `sudo apt install libportaudio2` (Debian/Ubuntu)
  or `sudo dnf install portaudio` (Fedora).

## Radio settings

In the radio's network/remote settings:

- **Network Control**: ON
- A **network user name and password** (this is what the app logs in with)
- **UDP ports** on their defaults: control 50001, serial 50002, audio 50003
- Note the radio's **IP address** (or give it a fixed one in your router)

On the IC-705 these are under `Menu > Set > WLAN Set > Remote Settings`. I
have not verified the menu path on the IC-7300MK2.

## Run

```
python -m icomremote            # add --debug --log-file icom.log for a packet log
```

Enter the radio's address, user name and password, and click **Connect**.
**Radio** selects the CI-V address: *Auto* uses the address the radio reports
(B6h on the IC-7300MK2); the IC-705 profile forces A4h.

- **Frequency**: type a value in kHz and press Enter, or use the arrows /
  mouse wheel with the chosen step. Changes made on the radio show up within a
  moment.
- **Mode**: LSB, USB, AM, FM, plus **DATA** for the data variants (USB-D …).
- **Filter**: FIL1/FIL2/FIL3, with the selected filter's passband shown next
  to them.
- **S-meter**: updated twice a second.
- **Receive audio output**: pick the virtual cable (or speakers) and tick
  *Play to*.

The password is not saved unless you tick *Remember password*; it is then
stored unencrypted in the app's settings on that computer.

## Virtual audio cable setup

The app plays into the *input* side of the virtual cable; the decoding
program records from the *output* side.

| OS | Install | In Icom Remote, play to | In WSJT-X / fldigi, input from |
|---|---|---|---|
| Windows | [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) | `CABLE Input (VB-Audio Virtual Cable)` | `CABLE Output (VB-Audio Virtual Cable)` |
| macOS | [BlackHole](https://existential.audio/blackhole/) (`brew install blackhole-2ch`) | `BlackHole 2ch` | `BlackHole 2ch` |
| Linux | none: click **Create 'IcomRX' virtual sink** | `PulseAudio/PipeWire sink: IcomRX` | `Monitor of IcomRX` |

## rigctld (CAT control)

On by default, listening on `127.0.0.1:4532` (the standard hamlib port). Use
`0.0.0.0` to allow clients from other machines on your LAN.

**WSJT-X / JTDX / JS8Call** (Settings → Radio): Rig `Hamlib NET rigctl`,
Network Server `127.0.0.1:4532`, PTT Method `VOX` for now, Mode `Data/Pkt`
or `USB`, Split `None`. Click *Test CAT*.

**fldigi** (Configure → Rig Control → Hamlib): rig `Hamlib NET rigctl`, device
`127.0.0.1:4532`.

**Command line:**

```
rigctl -m 2 -r 127.0.0.1:4532 F 14074000   # set frequency
rigctl -m 2 -r 127.0.0.1:4532 M PKTUSB 0   # USB-D
rigctl -m 2 -r 127.0.0.1:4532 f m l STRENGTH
```

Modes:

| rigctl | Radio |
|---|---|
| `PKTUSB`, `PKTLSB`, `PKTAM`, `PKTFM` | USB-D, LSB-D, AM-D, FM-D |
| `USB`, `LSB` | USB-D, LSB-D by default; plain USB/LSB if *Plain USB/LSB … selects USB-D/LSB-D* is unticked |
| `AM`, `FM` | AM, FM |
| others (CW, RTTY …) | refused (`RPRT -11`) |

The passband a client sends is ignored: the radio keeps its selected filter,
and `m` reports that filter's actual width. While the radio is disconnected,
reads return the last known values and changes fail with `RPRT -6`.

## Connection test (command line)

`python -m icomremote.probe` connects without the GUI and prints what it
finds; useful when something doesn't work:

```
python -m icomremote.probe --host 192.168.1.50 -u USER -p PASSWORD --debug
```

| Option | Purpose |
|---|---|
| `--play "BlackHole 2ch"` | play the received audio to an output device |
| `--list-devices` | list output device names |
| `--wav rx.wav` | record the received audio |
| `--civ B6` | force the CI-V address |
| `--debug` | hex-dump every control and serial packet to `icomremote-probe.log` (password blanked) |
| `--bind-same-ports` | use local UDP ports 50001-50003 like kappanhang does |
| `--seconds N` | how long to stay connected (default 20) |

The password can also come from the `ICOM_PASSWORD` environment variable.

## Tests

```
python -m unittest discover -s tests -v
```

Headless; no radio or audio hardware needed. A fake radio
(`tests/fake_rsba1.py`) implements the radio side of all three streams over
real UDP sockets and follows the packet order captured from a real
IC-7300MK2. Covered:

- packet layouts checked byte for byte against kappanhang, plus a real IC-705
  capabilities capture
- login, bad password, radio in use by another client, clean disconnect
- CI-V reads, transceive broadcasts, frequency/mode/DATA/filter control
- rigctld: frequency, mode mapping, passband reporting, PTT refusal, dump_state
- audio continuity, with dropped packets recovered by retransmit; audio timeout
- the GUI, run offscreen against the fake radio (needs PySide6; skipped otherwise)

## Licensing

GPL-3.0-or-later; see [LICENSE](LICENSE). The RS-BA1 protocol code is a port of
[kappanhang](https://github.com/nonoo/kappanhang) (MIT). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Layout

```
run_icomremote.py   launcher / PyInstaller entry (icomremote.spec)
icomremote/
  gui.py            PySide6 main window
  session.py        connection lifecycle, control API (frequency/mode/filter), audio sinks
  rigctld.py        hamlib NET rigctl server
  state.py          modes, rigctl mode mapping, radio profiles and limits
  civ.py            CI-V framing, BCD, meter scales, filter widths, state decoder
  sinks.py          sound card (clock-drift locked) and WAV outputs
  resample.py       streaming resampler
  timerres.py       1 ms timer resolution on Windows while connected
  probe.py          command-line connection test
  rsba1/            RS-BA1 protocol: packets, passcode, reorder buffer, UDP streams
tests/              fake radio + unit, end-to-end and GUI tests
```
