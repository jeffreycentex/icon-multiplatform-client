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
| 3 | TX audio (data and voice), PTT with safety rules, DATA MOD / DATA OFF MOD switch and restore, recording | done, verified on an IC-7300MK2 |
| 4 | Output/SWR readout, RF power, TUNE | done, verified on an IC-7300MK2 |
| 5 | Power ON/OFF | done, verified on an IC-7300MK2 |
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

- Optional: `pip install samplerate` for libsamplerate resampling. Not needed:
  the radio sends 48 kHz and most sound devices run at 48 kHz, so the default
  linear interpolator only trims for clock drift and is inaudible. (On
  Windows it may need a compiler, which is why it isn't in the requirements.)
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
- **S-meter**: the bar in the Radio box.
- **Output / SWR** (next to the PTT timeout): measured while the radio
  transmits or tunes, four times a second; the last reading stays shown
  afterwards. Output is the percent of full power. The dot is green below
  SWR 1.5, yellow below 2.5, red at 2.5 or higher, and grey when not
  transmitting.
- **Receive audio output**: pick the virtual cable (or speakers) and tick
  *Play to*.

The password is not saved unless you tick *Remember password*; it is then
stored unencrypted in the app's settings on that computer.

## Recording

**Record received audio** saves what the radio receives (not your transmit
audio) to the chosen folder:

- **WAV**: 16-bit PCM at 48 kHz, as received from the radio.
- **M4A / MP4**: AAC, 64 kbit/s, 48 kHz. Needs PyAV (`pip install av`, included
  in `requirements.txt`); without it these formats are greyed out.

File names carry the time (UTC), frequency and mode, e.g.
`icom_20261024T180000Z_14074.000kHz_USB-D.wav`. Recording can be started before
connecting; it waits for audio. A classic WAV file is limited to 4 GB, about
12 hours at 48 kHz.

## Virtual audio cables

For data modes you need **two** virtual cables, one per direction: if receive
and transmit share one cable, received audio would be sent back to the radio
(the app refuses that combination). Voice needs no cable: *Audio from* is
simply your microphone.

- **Windows:** VB-CABLE gives one cable; add VB-CABLE A+B (two more) or
  Hi-Fi Cable from the same site. All are donationware.
- **macOS:** BlackHole 2ch and BlackHole 16ch install as two separate devices.
- **Linux:** nothing to install. The app's buttons create two PulseAudio /
  PipeWire null sinks (IcomRX, IcomTX); each sink's "Monitor of …" source is
  the other end of that cable.

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

One transmit input, **Audio from**, with its gain and level meter, used for
both data and voice: pick the transmit virtual cable (e.g. BlackHole 16ch) for
WSJT-X / fldigi, or your microphone for voice. At key-down the app asks the
radio for its DATA state and applies the data or voice rules below from the
radio's answer.

### Keying

| Control | Data modes | Voice modes |
|---|---|---|
| **PTT** button (toggle) | yes | refused |
| rigctl `T 1` (WSJT-X, fldigi …) | yes | refused (`RPRT -9`) |
| **Space bar**, held, while **Voice PTT armed** is on | yes | yes |

The space bar does nothing unless **Voice PTT armed** is switched on. Arming
starts off every time the app starts and is switched off on every disconnect.
While armed, Space is PTT anywhere in the app's window, even in a text field
(arming also takes focus out of any field); disarm to type spaces again.
Releasing Space unkeys, and so do disarming and the window losing focus.

Keying is refused unless:

- **data:** an *Audio from* input is selected and DATA MOD is LAN
- **voice:** it comes from the armed space bar, an *Audio from* input is
  selected, and DATA OFF MOD is LAN

Transmit stops (PTT released) when:

- the **PTT timeout** expires (default 180 s, whatever keyed it),
- the radio switches between voice and DATA while keyed, from the app or on the radio's front panel,
- you disconnect or quit, or the connection fails,
- the transmit input is changed or disappears,
- the rigctl client that keyed PTT disconnects (e.g. WSJT-X crashes mid-transmission).

Audio is only sent to the radio while PTT is on. The **TX/RX** indicator shows
what the radio reports, not just what was requested.

### Power ON / OFF

The button next to the radio info reads **Power OFF** when the radio is on and
**Power ON** when it is in standby.

- **Power OFF** asks for confirmation, stops any transmission or tune, puts
  DATA MOD / DATA OFF MOD back, then switches the radio off.
- **Power ON** sends the CI-V power-on command and waits up to 25 s for the
  radio to answer. Then DATA MOD / DATA OFF MOD are switched to LAN again.
- While the radio is in standby the app stays connected to its network
  server; tuning and transmit controls are disabled. Switching the radio off
  or on at the radio is noticed within a few seconds.
- On the IC-7300MK2 the radio's network stays up after Power OFF (verified,
  with its *Power OFF Setting (for Remote Control)* on *Shutdown only*), and
  Power ON over the LAN brings it back in about 4 s. You can also connect to
  a radio that is already off and switch it on from here.
- If a radio's network goes down with it (e.g. an IC-705 on Wi-Fi), the app
  reports that the radio stopped responding; switch it on at the radio.
- rigctl `get_powerstat` reports on (1) / standby (2); `set_powerstat` is
  accepted and ignored: programs don't switch the radio on or off.

### RF power and TUNE

- **Power** sets the radio's RF POWER (0-100 %) when you release the slider;
  changes made on the radio show up within a couple of seconds.
- **TUNE** runs the internal antenna tuner's tune cycle, like the radio's
  TUNER button. It transmits a carrier, so it is refused while PTT is on, and
  PTT is refused while it runs. The cycle is stopped after 30 s or if you
  disconnect; the result ("tuner on" or "tuner off") appears in the status.

### DATA MOD and DATA OFF MOD

The radio takes transmit audio from **DATA MOD** in DATA modes and from
**DATA OFF MOD** in voice modes. Each has its own checkbox (both on by
default). For each one switched on, the app reads the radio's setting when it
connects, writes the original value to disk, then sets it to LAN. When you
disconnect or quit, it puts the original back and confirms it by reading it
back. While DATA OFF MOD is LAN, a microphone plugged into the radio is not
used.

If the app can't put it back (the app is killed, the computer loses power,
the network drops), the original value stays recorded in `restore.json` in
the app's settings folder:

- macOS: `~/Library/Application Support/IcomRemote/`
- Windows: `%APPDATA%\IcomRemote\`
- Linux: `~/.config/icomremote/`

The next connection to that radio restores it on disconnect, and the window
shows the pending restore while you're disconnected. **Restore now** puts
both back immediately (transmit is then refused until you reconnect). At
the radio without the app, change DATA MOD on the front panel (on the IC-7300
and IC-705: Menu → Set →
Connectors → MOD Input → DATA MOD).

On connect the app also warns if *LAN MOD Level* is 0 %, which would make
transmit silent.

Switching is implemented for the IC-7300MK2 (CI-V `1A 05 00 85` DATA MOD,
`1A 05 00 84` DATA OFF MOD, from Icom's CI-V reference). For other models the app reports that it doesn't
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
- transmit: framing/resampling, one input for data and voice, the PTT rules and every unkey trigger,
  audio sent only while keyed, rapid press/release never leaving the radio keyed
- the armed space bar (GUI test): disarmed, armed in a text field, disarm while keyed
- DATA MOD: switch, restore, crash recovery from the record on disk, manual restore
- audio continuity, with dropped packets recovered by retransmit; audio timeout
- recording: WAV (and M4A when PyAV is installed), including starting before connecting
- RF power read/set, Po/SWR polled only while transmitting, TUNE cycle (success, failure,
  30 s timeout, abort on disconnect, refusals both ways)
- power: off/on from the app, off/on at the radio, connecting to a radio in standby,
  power-on ignored, 'Shutdown only' (network goes down), unkey and DATA MOD restore before off
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
