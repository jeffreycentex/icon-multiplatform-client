# CLAUDE.md

Context for working on this repo. Read this first. README.md is the user-facing
documentation; this file holds the decisions, protocol findings and working
conventions behind it.

## Project

Cross-platform (macOS / Windows / Linux) Python desktop client for Icom's
RS-BA1 LAN remote protocol. Target radio: the owner's **IC-7300MK2**
(built-in LAN, no shack PC), **CI-V address B6h**. The IC-705 (A4h) is the
secondary profile. Modelled on the owner's
[kiwisdr-remote](https://github.com/jeffreycentex/kiwisdr-remote): same
architecture, GUI style and reused modules.

Why not kappanhang (Go) itself: it is Linux-only (PulseAudio, pty, terminal
UI), and adding macOS/Windows audio plus a GUI was more work than porting the
protocol into the existing Python app design.

Out of scope by decision: virtual serial port, TCP serial server, waterfall /
scope, CW/RTTY modes.

## Owner and working style

- Engineer (BS Engineering, MS Computer Science). Prefers direct answers.
- Tests on macOS with Homebrew **Python 3.15**, WSJT-X, BlackHole 2ch (RX) +
  BlackHole 16ch (TX). Not yet tested on Windows or Linux.
- Builds in numbered steps; each is tested on the real radio before the next.
- For ambiguous or safety-relevant changes, ask before building.
- Commits: author `jeffreycentex <jeffreycentex@gmail.com>`; end messages with
  the session's Co-Authored-By / Claude-Session lines. Push to `main`.

## Status

| Step | Content | State |
|---|---|---|
| 1 | Login, 3 UDP streams, RX audio, CI-V read; `python -m icomremote.probe` | verified on IC-7300MK2 |
| 2 | GUI, freq/mode/filter, S-meter, rigctld | verified |
| 3 | TX audio, PTT safety, DATA MOD + DATA OFF MOD switch/restore, voice PTT (armed Space), recording | verified (spots on PSKReporter; DATA MOD restore confirmed) |
| 4 | RF power, TUNE, Output/SWR readout | TUNE + PTT verified; readout redesigned after feedback |
| 5 | Power ON/OFF | verified on IC-7300MK2 (off, on, connect while off) |
| 6 | PyInstaller builds + CI | CI green on Windows + Linux (tests, build, built-app self-check); macOS last timing fix pending; owner to test the .app and a Windows build on hardware |

## Spec decisions (from the owner)

- **Modes**: LSB, USB, AM, FM and their DATA variants (USB-D …). Nothing else.
- **Filters (option A)**: FIL1/2/3 buttons. rigctl passband is ignored on set;
  `m` reports the selected filter's real width (CI-V 1A 03; FM fixed 15/10/7 kHz).
- **rigctl modes**: `PKTUSB/PKTLSB/PKTAM/PKTFM` -> DATA modes. Plain `USB/LSB`
  -> USB-D/LSB-D by default (checkbox to turn off). fldigi offers USB and
  USB-Data; default to USB-D was the owner's call.
- **Transmit** (safety rules, owner-approved):
  - Data modes: PTT button (toggle), rigctl `T 1`, or armed Space.
  - Voice modes: **only** the space bar while "Voice PTT armed" is on. PTT
    button and rigctl are refused in voice modes (`RPRT -9`).
  - Arming: off at every start and every disconnect. While armed, Space is
    PTT anywhere in the window **including text fields** (owner couldn't
    leave a text field on macOS); arming clears focus.
  - Key-down asks the radio for its DATA state and decides from the radio's
    answer, never from the app's cached state.
  - Unkey on: timeout (default 180 s), voice<->data switch (app or radio),
    disconnect/quit/connection loss, TX input change, rigctl client that
    keyed disconnects, Space release, disarm, window losing focus.
  - TUNE refused while keyed; PTT refused while tuning; tune stopped after
    30 s or on disconnect.
- **One transmit input** ("Audio from") for data and voice; owner switches
  device as needed (two separate inputs made the window too tall).
- **DATA MOD (1A 05 00 85) and DATA OFF MOD (1A 05 00 84)**: switched to LAN
  while connected and **must be restored**. Original written to
  `restore.json` (atomic, fsync) *before* touching the radio; restored and
  read back on disconnect; a record left by a crash is honoured at the next
  connection; manual "Restore now". Owner has had trouble with MOD settings
  before; treat this path as critical.
- **Recording**: received audio only; WAV (48 kHz) or M4A/MP4 (PyAV).
- **Meters**: bar = S-meter always. Separate "Output NN %  SWR N.N" readout
  next to PTT timeout with a dot: green < 1.5, yellow < 2.5, red >= 2.5,
  grey off-air. (A bar that switched to Po/SWR on TX confused the owner.)
- **Layout**: two columns. Left: radio, tuning, RX audio, recording. Right:
  transmit, rigctld. Avoid making the window taller.
- **Power ON/OFF** (step 5): OFF with a confirm dialog (unkey + MOD restore
  first). ON works over the LAN on the IC-7300MK2. Where a radio's network
  drops when off, Power ON is simply unreachable (not connected).
- `samplerate` removed from requirements (Windows build pain; linear
  resampling is inaudible at 48 kHz). PyAV stays for M4A.

## Architecture

```
icomremote/
  gui.py        PySide6 window. Events from other threads arrive via a Qt
                signal (_Bridge). App-wide event filter handles Space.
  session.py    RadioSession: one worker thread running asyncio; owns the
                three streams; reconnect loop; control API (thread-safe);
                PTT/TX/DATA MOD/tuner logic; audio sinks.
  rigctld.py    hamlib NET rigctl (model 2) server, threaded TCP; from
                kiwisdr-remote. Tracks which client keyed PTT.
  civ.py        CI-V framing/BCD, decoder -> (kind, value) events, meter
                scales, filter widths, command builders, menu item tables.
  state.py      modes, hamlib mapping, per-model freq limits, profiles.
  txaudio.py    TxFramer (resample/gain/limit/20 ms frames) + PortAudio input.
  sinks.py      SoundCardSink (PI-controlled clock-drift lock), WavSink,
                M4aSink; from kiwisdr-remote.
  restore.py    on-disk record of radio settings to put back.
  timerres.py   timeBeginPeriod(1) on Windows while connected.
  probe.py      CLI connection test with --debug packet hex dump.
  rsba1/        protocol: packets.py, passcode.py, seqbuf.py (reorder +
                retransmit), stream.py (UDP base), streams.py (control /
                serial / audio).
tests/          fake_rsba1.py (fake radio over real UDP) + unit, end-to-end,
                TX, step-4 and GUI tests.
```

Threads: GUI thread; session thread (asyncio; the only one touching sockets);
PortAudio callback threads (RX playback, TX capture); rigctld threads. Cross
into the loop only with `call_soon_threadsafe` / `run_coroutine_threadsafe`.

## RS-BA1 protocol notes

Ported from kappanhang (MIT; notice in THIRD_PARTY_NOTICES.md). Findings from
the real IC-7300MK2 that differ from or add to kappanhang:

- Ports 50001 control, 50002 serial (CI-V), 50003 audio. Handshake per
  stream: 0x03 -> 0x04 (radio sid), 0x06 -> 0x06. Radio sends answers twice.
- Login (0x80) -> 0x60 reply (`ff ff ff fe` at 48 = bad credentials) -> auth
  0x02 and 0x05 -> radio sends **capabilities (0xA8, every login)**, then an
  **unsolicited 0x90 status with in-use byte 96 = 0 (radio free)**, then the
  auth reply. Treating that 0x90 as a refusal was the step-1 bug.
- 0x90 status layout: byte 96 in-use flag, 100..115 client computer name,
  132..135 its IPv4. Our stream request is granted when a 0x90 arrives with
  in-use 1 **and our client name `icom-pc`**. In-use 1 with another name =
  radio held by another client (report name and IP).
- Capabilities: radio name `IC-7300MK2`, CI-V B6h at offset +82 of the radio
  record, audio `ICOM_VAUDIO`.
- Audio: 48 kHz s16le mono, about 10 ms per RX packet; TX sent as 1364 +
  556 byte packets per 20 ms frame; TX buffer 300 ms (larger breaks TX).
- Measured: RTT 6-18 ms over the owner's VPN, 0 packet loss in 20 s.
- Re-login too often can make radios drop audio: 5 fast retries, then 65 s.
- Password is blanked in packet hex dumps (`packets.redact`); keep it that way.

## CI-V items used (IC-7300MK2 reference)

| Command | Use |
|---|---|
| 03 / 05 | read / set frequency |
| 04 / 06 | read / set mode + filter |
| 1A 06 | DATA mode on/off + filter |
| 1A 03 | IF filter width index |
| 15 02 / 15 11 / 15 12 | S-meter / Po / SWR (0000-0255 BCD) |
| 14 0A | RF power 0000-0255 |
| 1C 00 | PTT |
| 1C 01 | tuner: 00 off, 01 on, 02 tune |
| 1A 05 00 85 / 00 84 | DATA MOD / DATA OFF MOD: 00 MIC, 01 USB, 02 ACC, 03 MIC+USB, 04 MIC+ACC, 05 LAN |
| 1A 05 00 83 | LAN MOD level (warn when 0) |
| 1A 05 01 09 | Power OFF setting for remote: 00 shutdown only, 01 standby/shutdown |
| 18 00 / 18 01 | power off / on |

Scales: S-meter 0=S0, 120=S9, 241=S9+60. Po 143=50 %, 213=100 %. SWR 48=1.5,
80=2.0, 120=3.0.

## Power (step 5) design and unknowns

- Power state = CI-V liveness: `on` if the radio answers state reads,
  `standby` if CI-V is silent > 3 s while the UDP session is alive. Bare
  OK/NG don't count (the radio ACKs `18 00`), and CI-V is ignored for 2 s
  after a power-off.
- In standby the audio-timeout reconnect is suspended (`AudioStream.require_audio`);
  instead a link check fails the session if *nothing* arrives for 6 s.
- Power OFF: stop tune, unkey, restore DATA MOD/DATA OFF MOD, then `18 00`.
  Power ON: 25 x FE preamble + `18 01`, wait up to 25 s; on wake, re-read
  state and switch the MODs to LAN again (`_power_is_on`).
- **Verified on the IC-7300MK2** (log of 2026-10-24):
  - After `18 00` the network stays up; the RS-BA1 session survives and a
    new login while off is accepted and streams are granted.
  - In standby the radio **answers every CI-V command with NG (`FA`)**, it
    is not silent. Standby is still detected by "no state replies for 3 s";
    NG is logged at debug only.
  - `18 01` (with 25 x FE preamble) over the LAN is ACKed (`FB`) and the
    radio answers frequency reads about 4 s later.
  - `1A 05 01 09` reads `00` ("Shutdown only") on this radio, yet the
    network stays up. Don't predict LAN behaviour from that setting; it is
    only logged.
- The fake radio matches this (NG in standby, ACK on power-on).
  `network_off_when_off=True` models a radio whose network drops (IC-705
  Wi-Fi), which the 6 s link check turns into "stopped responding".

## Concurrency lessons (both found by tests, both fixed)

- **Stale poll replies**: a read in flight when the app changes a setting
  used to overwrite the new value, briefly letting PTT key in plain USB. Fix:
  per-group read counters and fences (`_fence`, `_reads_sent`, `_replies`):
  replies to reads sent before a change are ignored for that group. Same
  idea for tuner status at tune start (`_tune_fence`).
- **PTT request ordering**: a stale release could clear a newer press, so
  the radio stayed keyed. Fix: sequence-numbered requests under
  `_ptt_lock`; only the newest applies; automatic unkeys go through
  `_unkey()`, which also bumps the sequence.

Any new feature that changes radio state or keys the transmitter needs a
race test (see `test_stale_reply_cannot_enable_ptt`, `test_quick_release_never_sticks`).

## Environment and testing

- `python -m unittest discover -s tests -v`; also run with
  `-W error::ResourceWarning`. Repeat `tests.test_tx` a few times after
  touching PTT code.
- The fake radio follows the real IC-7300MK2 packet order. When the real
  radio shows new behaviour, change the fake to match and add a test.
- Claude's sandbox has no PyPI access (PySide6, PyAV, sounddevice/PortAudio
  unavailable): `test_gui` and `test_record_m4a` skip there and must be run
  by the owner on macOS. Ask for their output.
- Python 3.15 on macOS: `loop.getaddrinfo` failed in the worker thread
  (`'lazy_import' object is not callable`). Workaround in place: resolve with
  `socket.getaddrinfo` in the session thread; `ThreadPoolExecutor` touched at
  import. Don't reintroduce executor use in the session loop.
- Debug a real radio with `python -m icomremote --debug --log-file icom.log`
  or `python -m icomremote.probe --host … --debug`.

## Packaging and CI

- `build.py` -> PyInstaller (`icomremote.spec`) -> runs the built app with
  `--check` -> zip (macOS via `ditto`, keeps signature/symlinks) / zip
  (Windows) / tar.gz (Linux) in `dist/`.
- `--check` (`__main__.check`) reports Qt, PortAudio, PyAV, resampler,
  protocol modules; exit 0 = required parts OK. In a windowed build stdout
  is None, so the result goes to the log file too.
- Built app always logs to `<settings dir>/icomremote.log` (overwritten each
  start). Ask the owner for it when the .app misbehaves.
- macOS Info.plist needs `NSLocalNetworkUsageDescription` (macOS 15 local
  network privacy) and `NSMicrophoneUsageDescription`; without them access is
  silently denied. App is unsigned: right-click Open / remove quarantine.
- `.github/workflows/build.yml`: matrix macos-latest (arm64), windows-latest,
  ubuntu-22.04; Python 3.12; runs the **full** test suite (including GUI and
  M4A tests, offscreen) then `build.py --no-install`; artifacts kept 30 days;
  `v*` tags attach archives to a release. This is the only Windows/Linux
  testing so far, and it is how Claude can see Windows results: read runs
  with `gh api repos/jeffreycentex/icon-multiplatform-client/actions/runs`.
- Version: `icomremote/__init__.py` `__version__` (0.6.0 = step 6).
- Raw job logs are **not** reachable from Claude's sandbox (blob storage
  blocked); `tests/ci_run.py` and `build.py` report results as annotations:
  `gh api repos/jeffreycentex/icon-multiplatform-client/check-runs/<job id>/annotations`.

## Lessons from Windows / CI (2026-10-24)

- Windows + UDP: create sockets via `netcompat.udp_socket` (sets
  `SIO_UDP_CONNRESET` off) and run asyncio with `netcompat.run` (selector
  loop on every OS). The default Proactor loop lost CI-V answers and left
  transports half-closed.
- `time.monotonic()` has 15.6 ms resolution on Windows: use
  `time.perf_counter()` for short intervals (RTT).
- Menu reads (`_read_menu`) accept only the matching answer; stray NGs
  (standby probes) must not satisfy them.
- rigctl `m` right after `M` reports the filter's nominal width until the
  radio's 1A 03 answer arrives (never 0).
- Tests on CI: never assume `sleep()` is accurate; fake sources follow
  wall-clock time; always `wait_for` a second dependent condition; tear Qt
  down in `tearDownModule` (otherwise crash at interpreter exit on all OSes).

## Audio cables

Data needs two virtual cables (RX and TX) or received audio would be
transmitted; the GUI refuses the same device both ways. macOS: BlackHole 2ch
+ 16ch. Windows: VB-CABLE + VB-CABLE A+B or Hi-Fi Cable. Linux: app-created
null sinks IcomRX / IcomTX (use "Monitor of …"). Voice needs no cable.
