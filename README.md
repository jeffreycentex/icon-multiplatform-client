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
| 2 | GUI, frequency/mode/filter control, S-meter, rigctld | done, verified on an IC-7300MK2 |
| 3 | TX audio, PTT with safety rules, DATA MOD switch and restore | **this release** |
| 4 | Po/SWR on TX, RF power, TUNE | |
| 5 | Power ON/OFF | |
| 6 | PyInstaller builds | |


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

## Virtual audio cables

You need **two** virtual cables, one per direction. If receive and transmit
share one cable, received audio would be sent back to the radio (the app
refuses that combination).

| Direction | Windows | macOS | Linux |
|---|---|---|---|
| Install | [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) + a second cable (VB-Audio *Hi-Fi Cable* or *Cable A/B*) | [BlackHole](https://existential.audio/blackhole/): `brew install blackhole-2ch blackhole-16ch` | nothing: the app's buttons create PulseAudio/PipeWire sinks |
| **Receive**: Icom Remote *Play to* | `CABLE Input` | `BlackHole 2ch` | `PulseAudio/PipeWire sink: IcomRX` |
| Receive: WSJT-X / fldigi input | `CABLE Output` | `BlackHole 2ch` | `Monitor of IcomRX` |
| **Transmit**: WSJT-X / fldigi output | second cable's input | `BlackHole 16ch` | `IcomTX` |
| Transmit: Icom Remote *Audio from* | second cable's output | `BlackHole 16ch` | `PulseAudio/PipeWire source: IcomTX.monitor` |

macOS: the first time the app reads an input device, macOS asks for
microphone permission for the program running it (Terminal, or the app
bundle). If it was denied, the transmit level stays at zero: enable it in
System Settings → Privacy & Security → Microphone.

## Transmit

Transmit is for data modes only. PTT, from the **PTT** button or from rigctl
(`T 1`), is refused unless:

- the radio is in a DATA mode (USB-D, LSB-D, AM-D, FM-D),
- a transmit audio input is selected (*Audio from*), and
- DATA MOD is LAN.

Transmit stops (PTT released) when:

- the **PTT timeout** expires (default 180 s, whatever keyed it),
- the radio leaves DATA mode, from the app or on the radio's front panel,
- you disconnect or quit, or the connection fails,
- the transmit audio input is changed or disappears,
- the rigctl client that keyed PTT disconnects (e.g. WSJT-X crashes mid-transmission).

Audio is only sent to the radio while PTT is on. The **TX/RX** indicator shows
what the radio reports, not just what was requested.

### DATA MOD

With *Switch DATA MOD to LAN while connected* on (the default), the app reads
the radio's DATA MOD setting when it connects, writes the original value to
disk, then sets DATA MOD to LAN. When you disconnect or quit, it puts the
original back and confirms it by reading it back.

If the app can't put it back (the app is killed, the computer loses power,
the network drops), the original value stays recorded in `restore.json` in
the app's settings folder:

- macOS: `~/Library/Application Support/IcomRemote/`
- Windows: `%APPDATA%\IcomRemote\`
- Linux: `~/.config/icomremote/`

The next connection to that radio restores it on disconnect, and the window
shows the pending restore while you're disconnected. **Restore DATA MOD now**
puts it back immediately (transmit is then refused until you reconnect). At
the radio without the app, change DATA MOD on the front panel (on the IC-7300
and IC-705: Menu → Set →
Connectors → MOD Input → DATA MOD).

On connect the app also warns if *LAN MOD Level* is 0 %, which would make
transmit silent.

DATA MOD switching is implemented for the IC-7300MK2 (CI-V `1A 05 00 85`,
from Icom's CI-V reference). For other models the app reports that it doesn't
know the setting; set DATA MOD to LAN on the radio yourself.

## rigctld (CAT control)

On by default, listening on `127.0.0.1:4532` (the standard hamlib port). Use
`0.0.0.0` to allow clients from other machines on your LAN.

**WSJT-X / JTDX / JS8Call** (Settings → Radio): Rig `Hamlib NET rigctl`,
Network Server `127.0.0.1:4532`, PTT Method `CAT`, Mode `Data/Pkt` or `USB`,
Split `None`. Click *Test CAT*, then *Test PTT*. Settings → Audio: input =
the receive cable, output = the transmit cable (see above).

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

`T 1` is refused with `RPRT -9` when the transmit rules above aren't met
(the reason appears in the status bar). PTT keyed by a client is released if
that client disconnects.

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
- rigctld: frequency, mode mapping, passband reporting, PTT and its release on client disconnect, dump_state
- transmit: framing/resampling, the PTT rules and every unkey trigger, audio sent only while keyed
- DATA MOD: switch, restore, crash recovery from the record on disk, manual restore
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
  txaudio.py        transmit audio capture and 20 ms framing
  restore.py        on-disk record of radio settings to put back
  probe.py          command-line connection test
  rsba1/            RS-BA1 protocol: packets, passcode, reorder buffer, UDP streams
tests/              fake radio + unit, end-to-end and GUI tests
```
