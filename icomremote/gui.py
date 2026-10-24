"""PySide6 main window (layout and audio/rigctld boxes adapted from kiwisdr-remote)."""

import logging
import shutil
import sys

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QSlider, QSpinBox, QStatusBar, QVBoxLayout, QWidget)

from . import __version__, civ
from .rigctld import RigctldServer
from .session import ConnectionSpec, NotConnected, PttRefused, RadioSession
from .sinks import (HAVE_SOUNDDEVICE, PULSE_PREFIX, create_null_sink, guess_virtual_cable,
                    list_output_devices, sounddevice_error)
from .state import BASE_MODES, FILTERS, PROFILES
from .txaudio import guess_tx_cable, list_input_devices

log = logging.getLogger(__name__)

STEPS_HZ = [10, 100, 500, 1000, 5000, 9000, 10000, 100000]
S_MAX_RAW = 241          # S9+60 dB


class _Bridge(QObject):
    """Carries events from the session and rigctld threads to the GUI thread."""
    event = Signal(str, object)


class MainWindow(QMainWindow):
    def __init__(self, settings=None):
        super().__init__()
        self.setWindowTitle("Icom Remote %s" % __version__)
        self.settings = settings if settings is not None else QSettings("IcomRemote", "Icom Remote")
        self.bridge = _Bridge()
        self.bridge.event.connect(self._on_event, Qt.QueuedConnection)
        self.radio = RadioSession(on_event=self.bridge.event.emit)
        self.rigctld = None
        self._editing_freq = False

        central = QWidget()
        root = QVBoxLayout(central)
        root.addWidget(self._build_radio_box())
        root.addWidget(self._build_tune_box())
        root.addWidget(self._build_audio_box())
        root.addWidget(self._build_tx_box())
        root.addWidget(self._build_rigctl_box())
        root.addStretch(1)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        exit_btn = QPushButton("Exit")
        exit_btn.setToolTip("Disconnect and close the application")
        exit_btn.clicked.connect(self.close)          # closeEvent saves settings and shuts down
        bottom.addWidget(exit_btn)
        root.addLayout(bottom)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        self._load_settings()
        self._set_controls_enabled(False)
        self._timer = QTimer(self, interval=500, timeout=self._tick)
        self._timer.start()

        if not HAVE_SOUNDDEVICE:
            self.audio_enable.setEnabled(False)
            self.audio_enable.setToolTip("PortAudio not available: %s" % sounddevice_error())
        if self.rig_enable.isChecked():
            self._toggle_rigctld(True)

    # ================================================================== layout
    def _build_radio_box(self):
        box = QGroupBox("Radio")
        g = QGridLayout(box)
        self.host = QLineEdit(placeholderText="IP address or host name, e.g. 192.168.1.50")
        self.host.returnPressed.connect(self._connect_clicked)
        self.user = QLineEdit(placeholderText="network user name set in the radio")
        self.password = QLineEdit(echoMode=QLineEdit.Password)
        self.password.returnPressed.connect(self._connect_clicked)
        self.remember_pw = QCheckBox("Remember password")
        self.remember_pw.setToolTip("Stored unencrypted in this computer's settings for the app")
        self.profile = QComboBox()
        for label, addr in PROFILES.items():
            self.profile.addItem(label, addr)
        self.profile.setToolTip("CI-V address. Auto uses the address the radio reports.")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._connect_clicked)
        self.conn_label = QLabel("Disconnected")
        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color: gray")
        self.smeter = QProgressBar(minimum=0, maximum=S_MAX_RAW, textVisible=True)
        self.smeter.setFormat("—")
        self.smeter.setValue(0)

        g.addWidget(QLabel("Address"), 0, 0)
        g.addWidget(self.host, 0, 1, 1, 2)
        g.addWidget(self.connect_btn, 0, 3)
        g.addWidget(QLabel("User"), 1, 0)
        g.addWidget(self.user, 1, 1)
        g.addWidget(QLabel("Radio"), 1, 2)
        g.addWidget(self.profile, 1, 3)
        g.addWidget(QLabel("Password"), 2, 0)
        g.addWidget(self.password, 2, 1)
        g.addWidget(self.remember_pw, 2, 2, 1, 2)
        g.addWidget(self.conn_label, 3, 0, 1, 4)
        g.addWidget(self.info_label, 4, 0, 1, 4)
        g.addWidget(QLabel("S-meter"), 5, 0)
        g.addWidget(self.smeter, 5, 1, 1, 3)
        g.setColumnStretch(1, 1)
        return box

    def _build_tune_box(self):
        box = QGroupBox("Tuning")
        v = QVBoxLayout(box)

        row = QHBoxLayout()
        self.freq = QDoubleSpinBox()
        self.freq.setDecimals(3)
        self.freq.setRange(30.0, 470000.0)
        self.freq.setSuffix(" kHz")
        self.freq.setKeyboardTracking(False)          # commit on Enter / arrows / wheel only
        self.freq.setAccelerated(True)
        f = QFont(self.freq.font())
        f.setPointSize(f.pointSize() + 8)
        f.setBold(True)
        self.freq.setFont(f)
        self.freq.setMinimumWidth(260)
        self.freq.valueChanged.connect(self._freq_edited)
        self.freq.findChild(QLineEdit).textEdited.connect(self._freq_typing)
        self.freq.editingFinished.connect(self._freq_done)
        self.step = QComboBox()
        for s in STEPS_HZ:
            self.step.addItem(("%d Hz" % s) if s < 1000 else ("%g kHz" % (s / 1000)), s)
        self.step.currentIndexChanged.connect(self._step_changed)
        row.addWidget(self.freq, 1)
        row.addWidget(QLabel("Step"))
        row.addWidget(self.step)
        v.addLayout(row)

        row = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for name in BASE_MODES:
            b = QPushButton(name, checkable=True)
            b.setMinimumWidth(56)
            self.mode_group.addButton(b)
            self.mode_buttons[name] = b
            row.addWidget(b)
        self.mode_group.buttonClicked.connect(self._mode_clicked)
        self.data_btn = QPushButton("DATA", checkable=True)
        self.data_btn.setToolTip("DATA mode (USB-D etc.): audio comes from the LAN, not the mic")
        self.data_btn.clicked.connect(self._data_clicked)
        row.addSpacing(12)
        row.addWidget(self.data_btn)
        row.addSpacing(24)
        self.filter_group = QButtonGroup(self)
        self.filter_buttons = {}
        for n in FILTERS:
            b = QPushButton("FIL%d" % n, checkable=True)
            self.filter_group.addButton(b, n)
            self.filter_buttons[n] = b
            row.addWidget(b)
        self.filter_group.idClicked.connect(self._filter_clicked)
        self.width_label = QLabel("")
        row.addWidget(self.width_label)
        row.addStretch(1)
        v.addLayout(row)
        return box

    def _build_audio_box(self):
        box = QGroupBox("Receive audio output (virtual cable / speakers)")
        g = QGridLayout(box)
        self.audio_enable = QCheckBox("Play to")
        self.audio_enable.toggled.connect(self._audio_toggled)
        self.device_combo = QComboBox()
        self.device_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.currentIndexChanged.connect(self._device_changed)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_devices)
        self.volume = QSlider(Qt.Horizontal, minimum=0, maximum=200, value=100)
        self.volume.setToolTip("Output gain (100% = unity)")
        self.volume.sliderReleased.connect(self._volume_changed)
        self.volume_label = QLabel("100%")
        self.volume.valueChanged.connect(lambda v: self.volume_label.setText("%d%%" % v))
        self.audio_stats = QLabel("")
        self.audio_stats.setStyleSheet("color: gray")
        g.addWidget(self.audio_enable, 0, 0)
        g.addWidget(self.device_combo, 0, 1)
        g.addWidget(refresh, 0, 2)
        g.addWidget(QLabel("Gain"), 1, 0)
        g.addWidget(self.volume, 1, 1)
        g.addWidget(self.volume_label, 1, 2)
        g.addWidget(self.audio_stats, 2, 1, 1, 2)
        if sys.platform.startswith("linux") and shutil.which("pactl"):
            mk = QPushButton("Create 'IcomRX' virtual sink")
            mk.setToolTip("pactl load-module module-null-sink sink_name=IcomRX\n"
                          "Other programs record from 'Monitor of IcomRX'.")
            mk.clicked.connect(self._create_pulse_sink)
            g.addWidget(mk, 3, 1, 1, 2)
        g.setColumnStretch(1, 1)
        return box

    def _build_tx_box(self):
        box = QGroupBox("Transmit (data modes only)")
        g = QGridLayout(box)
        self.tx_enable = QCheckBox("Audio from")
        self.tx_enable.setToolTip("The transmit audio input: the output side of the virtual cable\n"
                                  "WSJT-X/fldigi play into (BlackHole 2ch, CABLE Output, a monitor source)")
        self.tx_enable.toggled.connect(self._tx_toggled)
        self.tx_combo = QComboBox()
        self.tx_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.tx_combo.currentIndexChanged.connect(self._tx_device_changed)
        tx_refresh = QPushButton("Refresh")
        tx_refresh.clicked.connect(self._refresh_tx_devices)
        self.tx_gain = QSlider(Qt.Horizontal, minimum=0, maximum=200, value=100)
        self.tx_gain.setToolTip("Transmit audio gain (100% = unity). Set ALC on the radio as usual.")
        self.tx_gain.valueChanged.connect(self._tx_gain_changed)
        self.tx_gain_label = QLabel("100%")
        self.tx_level = QProgressBar(minimum=0, maximum=100, textVisible=True)
        self.tx_level.setFormat("input level")
        self.ptt_btn = QPushButton("PTT", checkable=True)
        self.ptt_btn.setMinimumWidth(90)
        self.ptt_btn.clicked.connect(self._ptt_clicked)
        self.tx_indicator = QLabel("RX")
        self.tx_indicator.setAlignment(Qt.AlignCenter)
        self.tx_indicator.setMinimumWidth(44)
        self._style_tx(False)
        self.ptt_timeout = QSpinBox(minimum=10, maximum=600, value=180, suffix=" s")
        self.ptt_timeout.setToolTip("Transmit is cut off after this long, whatever keyed it")
        self.ptt_timeout.valueChanged.connect(lambda v: setattr(self.radio, "ptt_timeout", float(v)))
        self.manage_data_mod = QCheckBox("Switch DATA MOD to LAN while connected (put back on disconnect)")
        self.manage_data_mod.toggled.connect(lambda on: setattr(self.radio, "manage_data_mod", on))
        self.data_mod_label = QLabel("")
        self.data_mod_label.setWordWrap(True)
        self.restore_btn = QPushButton("Restore DATA MOD now")
        self.restore_btn.setToolTip("Put DATA MOD back to its original setting now.\n"
                                    "Transmit is then refused until you reconnect.")
        self.restore_btn.clicked.connect(self._restore_clicked)
        self.restore_btn.setEnabled(False)

        g.addWidget(self.tx_enable, 0, 0)
        g.addWidget(self.tx_combo, 0, 1, 1, 3)
        g.addWidget(tx_refresh, 0, 4)
        g.addWidget(QLabel("Gain"), 1, 0)
        g.addWidget(self.tx_gain, 1, 1, 1, 2)
        g.addWidget(self.tx_gain_label, 1, 3)
        g.addWidget(QLabel("Level"), 2, 0)
        g.addWidget(self.tx_level, 2, 1, 1, 3)
        row = QHBoxLayout()
        row.addWidget(self.ptt_btn)
        row.addWidget(self.tx_indicator)
        row.addSpacing(16)
        row.addWidget(QLabel("PTT timeout"))
        row.addWidget(self.ptt_timeout)
        row.addStretch(1)
        g.addLayout(row, 3, 0, 1, 5)
        if sys.platform.startswith("linux") and shutil.which("pactl"):
            mk = QPushButton("Create 'IcomTX' virtual sink")
            mk.setToolTip("pactl load-module module-null-sink sink_name=IcomTX\n"
                          "WSJT-X/fldigi play to 'IcomTX'; transmit audio comes from 'Monitor of IcomTX'.")
            mk.clicked.connect(self._create_tx_sink)
            g.addWidget(mk, 2, 4)
        g.addWidget(self.manage_data_mod, 4, 0, 1, 5)
        g.addWidget(self.data_mod_label, 5, 0, 1, 4)
        g.addWidget(self.restore_btn, 5, 4)
        g.setColumnStretch(1, 1)
        return box

    def _build_rigctl_box(self):
        box = QGroupBox("Hamlib rigctld (CAT control for WSJT-X, fldigi, JS8Call …)")
        g = QGridLayout(box)
        self.rig_enable = QCheckBox("Enable")
        self.rig_enable.toggled.connect(self._toggle_rigctld)
        self.rig_addr = QComboBox(editable=True)
        self.rig_addr.addItems(["127.0.0.1", "0.0.0.0", "::1"])
        self.rig_addr.setToolTip("127.0.0.1 = this computer only; 0.0.0.0 = allow LAN clients")
        self.rig_port = QSpinBox(minimum=1, maximum=65535, value=4532)
        self.rig_plain_data = QCheckBox("Plain USB/LSB from rigctl selects USB-D/LSB-D")
        self.rig_plain_data.setToolTip("PKTUSB/PKTLSB always select DATA mode. With this on,\n"
                                       "plain USB/LSB do too (fldigi 'USB', WSJT-X mode 'USB').")
        self.rig_plain_data.toggled.connect(self._rig_plain_data_toggled)
        self.rig_status = QLabel("Off")
        hint = QLabel("In the client choose rig 'Hamlib NET rigctl' (model 2), "
                      "address 127.0.0.1:<port>. PTT arrives in the next step.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        g.addWidget(self.rig_enable, 0, 0)
        g.addWidget(QLabel("Address"), 0, 1)
        g.addWidget(self.rig_addr, 0, 2)
        g.addWidget(QLabel("Port"), 0, 3)
        g.addWidget(self.rig_port, 0, 4)
        g.addWidget(self.rig_plain_data, 1, 0, 1, 5)
        g.addWidget(self.rig_status, 2, 0, 1, 5)
        g.addWidget(hint, 3, 0, 1, 5)
        return box

    # ================================================================== settings
    def _load_settings(self):
        s = self.settings
        self.host.setText(s.value("host", ""))
        self.user.setText(s.value("user", ""))
        remember = s.value("remember_pw", False, type=bool)
        self.remember_pw.setChecked(remember)
        if remember:
            self.password.setText(s.value("password", ""))
        i = self.profile.findText(s.value("profile", ""))
        self.profile.setCurrentIndex(i if i >= 0 else 0)
        idx = self.step.findData(s.value("step", 1000, type=int))
        self.step.setCurrentIndex(idx if idx >= 0 else 3)
        self._step_changed()
        self._refresh_devices(select=s.value("audio_device", ""))
        self.volume.setValue(s.value("volume", 100, type=int))
        self._refresh_tx_devices(select=s.value("tx_device", ""))
        self.tx_gain.setValue(s.value("tx_gain", 100, type=int))
        self.ptt_timeout.setValue(s.value("ptt_timeout", 180, type=int))
        self.radio.ptt_timeout = float(self.ptt_timeout.value())
        self.manage_data_mod.setChecked(s.value("manage_data_mod", True, type=bool))
        self.radio.manage_data_mod = self.manage_data_mod.isChecked()
        if s.value("tx_enable", False, type=bool) and self.tx_combo.currentData():
            self.tx_enable.setChecked(True)
        self._show_pending_restores()
        self.rig_addr.setCurrentText(s.value("rig_addr", "127.0.0.1"))
        self.rig_port.setValue(s.value("rig_port", 4532, type=int))
        self.rig_plain_data.setChecked(s.value("rig_plain_data", True, type=bool))
        self.rig_enable.blockSignals(True)
        self.rig_enable.setChecked(s.value("rig_enable", True, type=bool))
        self.rig_enable.blockSignals(False)
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        if s.value("audio_enable", False, type=bool) and self.device_combo.currentData():
            self.audio_enable.setChecked(True)

    def _save_settings(self):
        s = self.settings
        s.setValue("host", self.host.text().strip())
        s.setValue("user", self.user.text().strip())
        s.setValue("remember_pw", self.remember_pw.isChecked())
        if self.remember_pw.isChecked():
            s.setValue("password", self.password.text())
        else:
            s.remove("password")
        s.setValue("profile", self.profile.currentText())
        s.setValue("step", self.step.currentData())
        s.setValue("audio_device", self.device_combo.currentData() or "")
        s.setValue("audio_enable", self.audio_enable.isChecked())
        s.setValue("volume", self.volume.value())
        s.setValue("tx_device", self.tx_combo.currentData() or "")
        s.setValue("tx_enable", self.tx_enable.isChecked())
        s.setValue("tx_gain", self.tx_gain.value())
        s.setValue("ptt_timeout", self.ptt_timeout.value())
        s.setValue("manage_data_mod", self.manage_data_mod.isChecked())
        s.setValue("rig_enable", self.rig_enable.isChecked())
        s.setValue("rig_addr", self.rig_addr.currentText())
        s.setValue("rig_port", self.rig_port.value())
        s.setValue("rig_plain_data", self.rig_plain_data.isChecked())
        s.setValue("geometry", self.saveGeometry())

    # ================================================================== connection
    def _connect_clicked(self, *_):
        if self.radio.running:
            self.radio.disconnect()
            self.connect_btn.setText("Connect")
            return
        host, user = self.host.text().strip(), self.user.text().strip()
        if not host or not user:
            QMessageBox.warning(self, "Radio", "Enter the radio's address and the network user name.")
            return
        spec = ConnectionSpec(host, user, self.password.text(), civ_address=self.profile.currentData())
        self._save_settings()
        self.radio.connect(spec)
        self.connect_btn.setText("Disconnect")

    def _set_controls_enabled(self, on):
        for w in (self.freq, self.data_btn, self.ptt_btn,
                  *self.mode_buttons.values(), *self.filter_buttons.values()):
            w.setEnabled(on)
        self.manage_data_mod.setEnabled(not on)     # takes effect at connect time

    # ================================================================== tuning
    def _show_rig(self, st):
        if not self._editing_freq and st.freq_hz:
            self.freq.blockSignals(True)
            self.freq.setValue(st.freq_hz / 1000.0)
            self.freq.blockSignals(False)
        for name, b in self.mode_buttons.items():
            b.setChecked(name == st.mode)
        self.data_btn.setChecked(st.data)
        for n, b in self.filter_buttons.items():
            b.setChecked(n == st.filter)
        w = st.width_hz
        self.width_label.setText(("%.1f kHz" % (w / 1000.0)) if w else "")
        if st.mode and st.mode not in BASE_MODES:
            self.statusBar().showMessage("Radio is in %s (not offered here)" % st.mode, 5000)

    def _step_changed(self, *_):
        self.freq.setSingleStep((self.step.currentData() or 1000) / 1000.0)

    def _freq_typing(self, *_):
        self._editing_freq = True

    def _freq_done(self):
        self._editing_freq = False

    @Slot(float)
    def _freq_edited(self, khz):
        self._editing_freq = False
        self._control(self.radio.set_frequency, khz * 1000.0)

    def _mode_clicked(self, button):
        self._control(self.radio.set_mode, button.text(), self.data_btn.isChecked())

    def _data_clicked(self, on):
        mode = self.radio.rig.mode
        if mode in BASE_MODES:
            self._control(self.radio.set_mode, mode, on)
        else:
            self.data_btn.setChecked(not on)

    def _filter_clicked(self, n):
        self._control(self.radio.set_filter, n)

    def _control(self, fn, *args):
        try:
            fn(*args)
        except (NotConnected, ValueError) as e:
            self.statusBar().showMessage(str(e), 5000)
            self._show_rig(self.radio.rig)

    # ================================================================== audio device
    def _refresh_devices(self, select=None):
        current = select if isinstance(select, str) and select else self.device_combo.currentData()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        devices = list_output_devices()
        for key, label in devices:
            self.device_combo.addItem(label, key)
        if not devices:
            self.device_combo.addItem("(no output devices found)", None)
        idx = self.device_combo.findData(current) if current else -1
        if idx < 0:
            guess = guess_virtual_cable(devices)
            idx = self.device_combo.findData(guess) if guess else 0
        self.device_combo.setCurrentIndex(max(idx, 0))
        self.device_combo.blockSignals(False)

    @Slot(bool)
    def _audio_toggled(self, on):
        key = self.device_combo.currentData() if on else None
        if on and not key:
            self.audio_enable.setChecked(False)
            return
        try:
            self.radio.set_audio_device(key, self.volume.value() / 100.0)
        except Exception as e:
            QMessageBox.warning(self, "Audio device", str(e))
            self.audio_enable.blockSignals(True)
            self.audio_enable.setChecked(False)
            self.audio_enable.blockSignals(False)
            self.audio_stats.setText("")

    def _create_pulse_sink(self, *_):
        try:
            name = create_null_sink("IcomRX")
        except Exception as e:
            QMessageBox.warning(self, "Virtual sink", str(e))
            return
        self._refresh_devices(select=PULSE_PREFIX + name)
        self._device_changed()

    def _device_changed(self, *_):
        if self.audio_enable.isChecked():
            self._audio_toggled(True)

    def _volume_changed(self, *_):
        sink = self.radio._sinks.get("card")
        if sink is not None:
            sink.gain = self.volume.value() / 100.0

    # ================================================================== transmit
    def _refresh_tx_devices(self, select=None):
        current = select if isinstance(select, str) and select else self.tx_combo.currentData()
        self.tx_combo.blockSignals(True)
        self.tx_combo.clear()
        devices = list_input_devices()
        for key, label in devices:
            self.tx_combo.addItem(label, key)
        if not devices:
            self.tx_combo.addItem("(no input devices found)", None)
        idx = self.tx_combo.findData(current) if current else -1
        if idx < 0:
            guess = guess_tx_cable(devices)
            idx = self.tx_combo.findData(guess) if guess else 0
        self.tx_combo.setCurrentIndex(max(idx, 0))
        self.tx_combo.blockSignals(False)

    def _create_tx_sink(self, *_):
        try:
            name = create_null_sink("IcomTX")
        except Exception as e:
            QMessageBox.warning(self, "Virtual sink", str(e))
            return
        from .txaudio import PULSE_SOURCE_PREFIX
        self._refresh_tx_devices(select=PULSE_SOURCE_PREFIX + name + ".monitor")
        self._tx_device_changed()

    @Slot(bool)
    def _tx_toggled(self, on):
        key = self.tx_combo.currentData() if on else None
        if on and not key:
            self.tx_enable.setChecked(False)
            return
        if on and self.audio_enable.isChecked():
            rx = (self.device_combo.currentData() or "").split("|")[0].lower()
            tx = key.split("|")[0].lower()
            if rx and (rx == tx or (rx.startswith("pulse-sink:") and tx == "pulse-source:%s.monitor" % rx[11:])):
                QMessageBox.warning(self, "Transmit audio",
                                    "Receive audio is played into the same device you selected for "
                                    "transmit audio, so received audio would be transmitted.\n\n"
                                    "Use a second virtual cable for transmit (see the README).")
                self.tx_enable.setChecked(False)
                return
        self.radio.set_tx_device(key, self.tx_gain.value() / 100.0)

    def _tx_device_changed(self, *_):
        if self.tx_enable.isChecked():
            self._tx_toggled(True)

    def _tx_gain_changed(self, v):
        self.tx_gain_label.setText("%d%%" % v)
        self.radio.set_tx_gain(v / 100.0)

    def _ptt_clicked(self, on):
        try:
            self.radio.set_ptt(on, owner=self)
        except PttRefused as e:
            self.ptt_btn.setChecked(False)
            self.statusBar().showMessage(str(e), 8000)

    def _style_tx(self, tx):
        self.tx_indicator.setText("TX" if tx else "RX")
        self.tx_indicator.setStyleSheet(
            "background: #c0392b; color: white; font-weight: bold; border-radius: 3px" if tx
            else "background: #2e7d32; color: white; font-weight: bold; border-radius: 3px")

    def _restore_clicked(self, *_):
        try:
            self.radio.restore_data_mod_now()
        except NotConnected as e:
            self.statusBar().showMessage(str(e), 5000)

    def _show_data_mod(self, info):
        v, orig, name = info["value"], info["original"], info["name"]
        if v is None:
            self.data_mod_label.setText("DATA MOD: unknown (set it to LAN on the radio)")
        elif orig is not None:
            self.data_mod_label.setText("DATA MOD: %s (was %s; put back on disconnect)"
                                        % (name, civ.DATA_MOD_NAMES.get(orig, orig)))
        else:
            self.data_mod_label.setText("DATA MOD: %s" % name)
        self.restore_btn.setEnabled(orig is not None)

    def _show_pending_restores(self):
        pend = self.radio.pending_restores()
        lines = ["%s: DATA MOD is still LAN; it will be put back to %s at the next connection"
                 % (radio, civ.DATA_MOD_NAMES.get(v.get("data_mod"), v.get("data_mod")))
                 for radio, v in pend.items() if "data_mod" in v]
        self.data_mod_label.setText("\n".join(lines))

    # ================================================================== rigctld
    @Slot(bool)
    def _toggle_rigctld(self, on):
        if self.rigctld is not None:
            self.rigctld.stop()
            self.rigctld = None
        self.rig_addr.setEnabled(not on)
        self.rig_port.setEnabled(not on)
        if not on:
            self.rig_status.setText("Off")
            return
        try:
            self.rigctld = RigctldServer(self.radio, self.rig_addr.currentText().strip(),
                                         self.rig_port.value(), on_activity=self._rig_activity,
                                         plain_ssb_is_data=self.rig_plain_data.isChecked())
            self.rigctld.start()
        except OSError as e:
            self.rigctld = None
            self.rig_status.setText("Could not listen: %s" % e)
            self.rig_enable.blockSignals(True)
            self.rig_enable.setChecked(False)
            self.rig_enable.blockSignals(False)
            self.rig_addr.setEnabled(True)
            self.rig_port.setEnabled(True)
            return
        self._rig_activity("clients", (0, None, False))

    def _rig_plain_data_toggled(self, on):
        if self.rigctld is not None:
            self.rigctld.plain_ssb_is_data = on

    def _rig_activity(self, kind, payload):
        # called from rigctld threads -> hop to GUI thread
        self.bridge.event.emit("rig_" + kind, payload)

    # ================================================================== events (GUI thread)
    @Slot(str, object)
    def _on_event(self, kind, payload):
        if kind == "rig":
            self._show_rig(payload)
        elif kind == "meter":
            name, raw = payload
            if name == "smeter":
                db = civ.s_meter_db(raw)
                self.smeter.setValue(min(raw, S_MAX_RAW))
                self.smeter.setFormat(civ.s_meter_text(db))
        elif kind == "status":
            text, level = payload
            self.conn_label.setText(text)
            self.conn_label.setStyleSheet("color: #c0392b" if level == "error" else "")
        elif kind == "radio":
            lo, hi = self.radio.freq_limits
            self.freq.blockSignals(True)
            self.freq.setRange(lo / 1000.0, hi / 1000.0)
            self.freq.blockSignals(False)
            self._radio_info = "%s · CI-V %02Xh" % (payload["name"], payload["civ_address"])
            self.info_label.setText(self._radio_info)
        elif kind == "connected":
            self._set_controls_enabled(payload)
            if payload:
                self.connect_btn.setText("Disconnect")
            else:
                self.smeter.setValue(0)
                self.smeter.setFormat("—")
                self.ptt_btn.setChecked(False)
                self._style_tx(False)
                self.restore_btn.setEnabled(False)
                self._show_pending_restores()
                if not self.radio.running:
                    self.connect_btn.setText("Connect")
        elif kind == "stats":
            if payload.get("connected"):
                self.info_label.setText("%s · RTT %.0f ms · audio lost %d" % (
                    getattr(self, "_radio_info", ""), payload.get("rtt_ms", 0), payload.get("audio_lost", 0)))
        elif kind == "stopped":
            if not self.radio.running:
                self.connect_btn.setText("Connect")
                self._set_controls_enabled(False)
        elif kind == "sink_failed":
            if payload == "card":
                self.audio_enable.blockSignals(True)
                self.audio_enable.setChecked(False)
                self.audio_enable.blockSignals(False)
        elif kind == "rig_clients":
            n = payload[0]
            if self.rigctld is not None:
                self.rig_status.setText("Listening on %s:%d — %d client%s"
                                        % (self.rigctld.host, self.rigctld.port, n, "" if n == 1 else "s"))
        elif kind == "ptt":
            self.ptt_btn.setChecked(payload)
        elif kind == "tx":
            self._style_tx(payload)
        elif kind == "data_mod":
            self._show_data_mod(payload)
        elif kind == "tx_source_failed":
            self.tx_enable.blockSignals(True)
            self.tx_enable.setChecked(False)
            self.tx_enable.blockSignals(False)
        elif kind == "rig_refused":
            self.statusBar().showMessage("rigctl: %s" % payload, 8000)
        elif kind == "rig_command":
            self.statusBar().showMessage("rigctl: %s" % payload, 3000)

    def _tick(self, *_):
        lvl = self.radio.tx_level()
        self.tx_level.setValue(int(100 * min(1.0, lvl)) if lvl is not None else 0)
        stats = self.radio.audio_stats()
        if stats:
            self.audio_stats.setText("buffer %.0f ms   clock trim %+.0f ppm   underruns %d"
                                     % (stats["buffer_ms"], stats["trim_ppm"], stats["underruns"]))
        elif not self.audio_enable.isChecked():
            self.audio_stats.setText("")

    def closeEvent(self, ev):
        self._save_settings()
        if self.rigctld is not None:
            self.rigctld.stop()
        self.radio.shutdown()
        super().closeEvent(ev)


def run(argv):
    app = QApplication(argv)
    app.setApplicationName("Icom Remote")
    w = MainWindow()
    w.show()
    return app.exec()
