"""Main application window: OD browser, acyclic read/write, telemetry-map editor."""
from __future__ import annotations

import sys
import time
import traceback

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSlider, QSpinBox, QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from . import protocol as proto
from .buffer import TelemetryBuffer
from .client import NetworkClient, TelemetrySample
from .graph_window import GraphWindow
from .log_console import LogConsole
from .od import OdEntry, OdModel, TLM_MAP_INDEX, TLM_MAX_BYTES, TLM_MAX_ENTRIES

# always-present telemetry channels coming from the cyclic status header
STATUS_CHANNELS = ["statusword", "mode_display", "node_state", "error_code", "status_counter", "movement_status"]

# Motor calibration command codes + persistence magics. These mirror the C
# #defines in mc_if_od.h (MC_IF_CAL_* / MC_IF_*_MAGIC); kept here as literals
# because the parser only extracts the OD X-macro, not the command constants.
# If those defines change in the contract, update these to match.
MC_CAL_ALIGN_CAPTURE = 1       # 0x2700:1 = 1  -> run electrical alignment routine
MC_CAL_CURRENT_OFFSET = 2      # (defined in the contract but NOT wired in firmware yet)
MC_CAL_SET_MECH_ZERO = 3       # 0x2700:1 = 3  -> capture current position as mechanical home
MC_SAVE_MAGIC = 0x7376         # 0x2800:1      -> commit PERSIST values to flash
MC_FACTORY_RESET_MAGIC = 0x7274  # 0x2800:3    -> erase saved config, restore defaults on reboot

# cal_status (0x2700:2) decode: raw code -> (label, colour). 0/3 are terminal
# "accepted/done" states, 1/2 are in-progress, 0xFFFF is a rejected/faulted request
# (see mc_scheduler.c: MC_CAL_STATUS_FAULT).
_CAL_STATUS_TEXT = {
    0: ("idle / done", "#060"),
    1: ("aligning…", "#a60"),
    2: ("current-offset…", "#a60"),
    3: ("mech-zero set", "#060"),
    0xFFFF: ("FAULT / rejected", "#a00"),
}

# Calibration completeness bitfield (0x2700:5 cal_done_flags) — mirrors MC_IF_CAL_DONE_* in
# mc_if_od.h. A SET bit means that calibration currently has valid data; a CLEAR bit = outstanding.
MC_CAL_DONE_ELECTRICAL = 0x0001
MC_CAL_DONE_MECH_ZERO = 0x0002
MC_CAL_DONE_CURRENT_OFFSET = 0x0004
# (mask, label) in display order for the outstanding-calibrations checklist.
_CAL_DONE_ITEMS = [
    (MC_CAL_DONE_ELECTRICAL, "Electrical alignment"),
    (MC_CAL_DONE_MECH_ZERO, "Mechanical zero"),
    (MC_CAL_DONE_CURRENT_OFFSET, "Current offset"),
]

# Axis op-mode (0x3020 axis_op_mode) — mirrors MC_IF_AXIS_MODE_* in mc_if_od.h.
MC_AXIS_MODE_OFF = 0
MC_AXIS_MODE_JOYSTICK = 1
MC_AXIS_MODE_PROFILE_VELOCITY = 2
MC_AXIS_MODE_PROFILE_POSITION = 3
MC_AXIS_MODE_HOLD = 4
MC_AXIS_MODE_TORQUE = 5   # CMC REQ-0012 implemented in CHANGELOG [4.3.0]
_AXIS_MODES = [
    (MC_AXIS_MODE_OFF, "Off"),
    (MC_AXIS_MODE_JOYSTICK, "Joystick"),
    (MC_AXIS_MODE_PROFILE_VELOCITY, "Profile Velocity"),
    (MC_AXIS_MODE_PROFILE_POSITION, "Profile Position"),
    (MC_AXIS_MODE_HOLD, "Hold"),
    (MC_AXIS_MODE_TORQUE, "Torque"),
]
_AXIS_STATE_NAMES = {0: "DISABLED", 1: "READY", 2: "RUNNING", 3: "FAULT"}

# Loop-tuning test modes (0x2910:1 test_mode) — mirror MC_IF_TEST_MODE_* in mc_if_od.h (ADR-030).
MC_TEST_MODE_OFF = 0
MC_TEST_MODE_VELOCITY = 1
MC_TEST_MODE_POSITION = 2
MC_TEST_MODE_CURRENT = 3

# Command-page live readback uses the MOTOR actuals (the CMC's 0x3002/3 aren't populated yet) +
# the CiA-402 statusword + the tuning generator's active flag (0x2910:7).
_CMD_STATE_KEYS = [(0x3000, 0), (0x6041, 0), (0x6064, 0), (0x606C, 0), (0x6077, 0), (0x2910, 7)]
# Motor Config tab live readouts, also handled by _cmd_on_state_read (measured current, derived
# brushed gains, configured backend). Routed to that handler but NOT command-state polled.
_MCFG_READOUT_KEYS = [(0x2000, 6), (0x2410, 6), (0x2400, 6), (0x2400, 7), (0x3001, 0)]

# OD tree columns
_COLUMNS = ["Name", "Index:Sub", "Type", "Acc", "Owner", "Category", "Flags", "Value", "Unit", "Watch"]
_VALUE_COL = 7
_WATCH_COL = 9


class MainWindow(QMainWindow):
    def __init__(self, od: OdModel):
        super().__init__()
        self.od = od
        self.client = NetworkClient(od)
        self.buffer = TelemetryBuffer()
        self.graph_windows: list[GraphWindow] = []
        self.item_by_key: dict[tuple[int, int], QTreeWidgetItem] = {}
        self.latest_sample: TelemetrySample | None = None
        self._probe_key: tuple[int, int] | None = None
        self._last_sample_time = 0.0

        self.setWindowTitle("CMC Object Dictionary Tool")
        self.resize(1280, 820)
        self._build_ui()
        self._wire_client()
        self._install_excepthook()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_watched)
        self.poll_timer.start(200)  # 5 Hz acyclic polling of watched entries

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._refresh_live)
        self.ui_timer.start(100)  # 10 Hz UI refresh from telemetry

        # No independent readout polling: live values come from the telemetry stream (the map), and
        # static values are read once after an action (derived gains, backend). Avoids OD-queue-full.

    # === UI ===================================================================
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.addWidget(self._build_connection_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_od_tree())
        splitter.addWidget(self._build_side_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)
        self.setCentralWidget(central)

        # dockable terminal-style debug console (float it, hide it, drag it out)
        self.log_console = LogConsole()
        self.log_dock = QDockWidget("Debug Log", self)
        self.log_dock.setObjectName("debug_log_dock")
        self.log_dock.setWidget(self.log_console)
        self.log_dock.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.resizeDocks([self.log_dock], [200], Qt.Orientation.Vertical)

        view_menu = self.menuBar().addMenu("&View")
        toggle = self.log_dock.toggleViewAction()
        toggle.setText("Debug &Log")
        toggle.setShortcut("Ctrl+L")
        view_menu.addAction(toggle)
        act_graph = QAction("&New Graph Window", self)
        act_graph.setShortcut("Ctrl+G")
        act_graph.triggered.connect(lambda: self._new_graph())
        view_menu.addAction(act_graph)

        self._log(f"OD loaded from {self.od.source}: {len(self.od.entries)} entries", "INFO")

    def _build_connection_bar(self) -> QWidget:
        box = QGroupBox("Connection")
        lay = QHBoxLayout(box)
        lay.addWidget(QLabel("CMC IP:"))
        self.ip_edit = QLineEdit("192.1.0.100")
        self.ip_edit.setMaximumWidth(140)
        lay.addWidget(self.ip_edit)

        lay.addWidget(QLabel("OD port:"))
        self.od_port = QSpinBox()
        self.od_port.setRange(1, 65535)
        self.od_port.setValue(proto.DEFAULT_OD_PORT)
        lay.addWidget(self.od_port)

        lay.addWidget(QLabel("Telemetry port:"))
        self.tlm_port = QSpinBox()
        self.tlm_port.setRange(1, 65535)
        self.tlm_port.setValue(proto.DEFAULT_TLM_PORT)
        lay.addWidget(self.tlm_port)

        lay.addWidget(QLabel("Cyclic rate (Hz):"))
        self.cyclic_rate = QSpinBox()
        self.cyclic_rate.setRange(1, 100000)
        self.cyclic_rate.setValue(1000)
        self.cyclic_rate.setToolTip("Cyclic tick rate; sets the telemetry time base from status_counter")
        lay.addWidget(self.cyclic_rate)

        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self._toggle_connect)
        lay.addWidget(self.btn_connect)

        lay.addStretch(1)
        self.lbl_status = QLabel("Disconnected")
        self.lbl_status.setStyleSheet("font-weight: bold;")
        lay.addWidget(self.lbl_status)
        self.lbl_node = QLabel("node: -")
        lay.addWidget(self.lbl_node)
        self.lbl_rate = QLabel("0 Hz  drops:0")
        lay.addWidget(self.lbl_rate)
        return box

    def _build_od_tree(self) -> QWidget:
        box = QGroupBox("Object Dictionary")
        lay = QVBoxLayout(box)

        filt = QHBoxLayout()
        filt.addWidget(QLabel("Filter:"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("name / index / owner, e.g. 'vel', '0x2300', 'cmc'")
        self.filter_edit.textChanged.connect(self._apply_filter)
        filt.addWidget(self.filter_edit)
        lay.addLayout(filt)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(_COLUMNS))
        self.tree.setHeaderLabels(_COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.currentItemChanged.connect(self._on_select)
        self.tree.itemChanged.connect(self._on_item_changed)

        for e in self.od.entries:
            if e.synthetic and e.index == TLM_MAP_INDEX and e.sub != 0:
                continue  # hide the 16 raw map words; the map editor handles them
            item = QTreeWidgetItem([
                e.name, e.id_str, e.type_name, e.access_name,
                e.owner_name, self._od_category(e), self._flags_str(e), "", e.unit, "",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, e.key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(_WATCH_COL, Qt.CheckState.Unchecked)
            self.tree.addTopLevelItem(item)
            self.item_by_key[e.key] = item

        for c in range(1, len(_COLUMNS)):
            self.tree.resizeColumnToContents(c)
        lay.addWidget(self.tree)
        return box

    @staticmethod
    def _od_category(e: OdEntry) -> str:
        """Sortable grouping tag derived from index/name/access (heuristic; click the header to sort)."""
        idx, name, acc = e.index, e.name.lower(), e.access_name
        if 0x1000 <= idx <= 0x1FFF or 0x6000 <= idx <= 0x6FFF:
            return "Standard"
        if name.startswith("tlm_"):
            return "Telemetry"
        if acc == "WO" or any(k in name for k in ("command", "trigger", "save", "factory_reset")):
            return "Command"
        if acc == "RO":
            return "Status"
        return "Config"

    @staticmethod
    def _flags_str(e: OdEntry) -> str:
        parts = []
        if e.is_pdo:
            parts.append("PDO")
        if e.is_persist:
            parts.append("PERSIST")
        return ",".join(parts)

    def _build_side_panel(self) -> QWidget:
        tabs = QTabWidget()
        tabs.addTab(self._build_rw_panel(),       "Read / Write")
        tabs.addTab(self._build_command_panel(),  "Motor Command")
        tabs.addTab(self._build_motorcfg_panel(), "Motor Config")
        tabs.addTab(self._build_setup_panel(),    "CMC Setup")
        tabs.addTab(self._build_map_panel(),      "Telemetry / Graphing")
        return tabs

    # === Motor Command tab ===================================================
    #
    # Operational drive control via the CMC axis_manager (0x30xx) — the
    # spec-mandated command path (INTERFACE_SPEC §5b: the PC tool must not drive
    # the motor's CiA-402 objects directly). Pick a mode, set its target, Enable.
    # Velocity + position work end-to-end; current/torque is pending CMC support
    # (REQUESTS.md REQ-0012) and shown disabled. Live readback uses the MOTOR
    # actuals (0x6064/0x606C/0x6077) + statusword, since the CMC's 0x3002/3 read 0.
    def _build_command_panel(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        intro = QLabel(
            "Drive the motor via the CMC axis_manager (0x30xx). Pick a mode, set its "
            "target, then <b>Enable</b>. <b>Disable</b> / <b>Quick stop</b> cut the drive."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("padding: 4px; color: #234;")
        outer.addWidget(intro)

        # --- Drive: mode + enable/stop + state ---
        drive = QGroupBox("Drive")
        dl = QVBoxLayout(drive)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("Mode:"))
        self.cmd_mode_combo = QComboBox()
        for val, label in _AXIS_MODES:
            self.cmd_mode_combo.addItem(label, val)
        self.cmd_mode_combo.addItem("Torque (pending CMC — REQ-0012)", MC_AXIS_MODE_TORQUE)
        self.cmd_mode_combo.model().item(self.cmd_mode_combo.count() - 1).setEnabled(False)
        self.cmd_mode_combo.currentIndexChanged.connect(self._cmd_update_entry_enables)
        mrow.addWidget(self.cmd_mode_combo)
        b_setmode = QPushButton("Set mode")
        b_setmode.clicked.connect(self._cmd_set_mode)
        mrow.addWidget(b_setmode)
        mrow.addStretch(1)
        dl.addLayout(mrow)

        brow = QHBoxLayout()
        b_en = QPushButton("Enable")
        b_en.clicked.connect(lambda: self._cmd_enable(True))
        brow.addWidget(b_en)
        b_dis = QPushButton("Disable")
        b_dis.clicked.connect(lambda: self._cmd_enable(False))
        brow.addWidget(b_dis)
        b_qs = QPushButton("Quick stop")
        b_qs.setStyleSheet("color: #a00; font-weight: bold;")
        b_qs.clicked.connect(self._cmd_quick_stop)
        brow.addWidget(b_qs)
        b_cf = QPushButton("Clear fault")
        b_cf.clicked.connect(self._cmd_clear_fault)
        brow.addWidget(b_cf)
        brow.addStretch(1)
        dl.addLayout(brow)

        srow = QHBoxLayout()
        srow.addWidget(QLabel("State:"))
        self.cmd_state_lbl = QLabel("-")
        self.cmd_state_lbl.setStyleSheet("font-weight: bold;")
        srow.addWidget(self.cmd_state_lbl)
        srow.addSpacing(16)
        srow.addWidget(QLabel("Statusword:"))
        self.cmd_sw_lbl = QLabel("-")
        self.cmd_sw_lbl.setStyleSheet("font-family: monospace;")
        srow.addWidget(self.cmd_sw_lbl)
        srow.addStretch(1)
        dl.addLayout(srow)
        mqrow = QHBoxLayout()
        b_rdmode = QPushButton("Read current mode")
        b_rdmode.setToolTip("Read axis_op_mode_actual (0x3001) — the mode the motor is actually running.")
        b_rdmode.clicked.connect(self._cmd_read_mode)
        mqrow.addWidget(b_rdmode)
        mqrow.addWidget(QLabel("Active mode:"))
        self.cmd_mode_actual_lbl = QLabel("-")
        self.cmd_mode_actual_lbl.setStyleSheet("font-weight: bold;")
        mqrow.addWidget(self.cmd_mode_actual_lbl)
        mqrow.addStretch(1)
        dl.addLayout(mqrow)
        outer.addWidget(drive)

        # --- Velocity / joystick ---
        self.cmd_velg = velg = QGroupBox("Velocity profile  (Profile Velocity / Joystick)")
        vl = QFormLayout(velg)
        vrow = QHBoxLayout()
        self.cmd_vel_edit = QLineEdit()
        self.cmd_vel_edit.setPlaceholderText("rad/s")
        self.cmd_vel_edit.setMinimumWidth(80)  # don't collapse to nothing
        self.cmd_vel_edit.returnPressed.connect(self._cmd_apply_velocity)
        vrow.addWidget(self.cmd_vel_edit, 1)   # editor takes the slack
        b_av = QPushButton("Apply velocity")
        b_av.clicked.connect(self._cmd_apply_velocity)
        vrow.addWidget(b_av, 0)
        vl.addRow("Target velocity (0x3023):", vrow)
        # --- Joystick row (panel-style RAW path, exercises cmc_state cal) ---
        # Slider acts like a real CAMERAD panel stick: full-positive maps to
        # the "high" raw value, full-negative to "low". The mapped raw is
        # written to OD 0x3026 axis_joystick_raw at the same 25 ms cadence
        # a panel would send MOVEMENT, so the calibration pipeline
        # (centre / full_pos / full_neg / deadband at 0x3027–0x302A) and
        # the joystick watchdog in cmc_state get exercised end-to-end —
        # not just normalisation-bypass via 0x3021 like the old slider did.
        jrow = QHBoxLayout()
        self.cmd_joy_slider = QSlider(Qt.Orientation.Horizontal)
        self.cmd_joy_slider.setRange(-100, 100)
        self.cmd_joy_slider.setTracking(True)
        # No write on every drag tick — the 25 ms timer does the pushing.
        # valueChanged only refreshes the value label so the user sees the
        # raw value the next tick will send.
        self.cmd_joy_slider.valueChanged.connect(self._cmd_joystick_changed)
        # Snap to centre when the operator lets go — matches the spring-
        # centred mechanical stick on a real panel. Next 25 ms tick then
        # sends 0, motor stops.
        self.cmd_joy_slider.sliderReleased.connect(
            lambda: self.cmd_joy_slider.setValue(0))
        jrow.addWidget(self.cmd_joy_slider, 1)

        # Per-direction raw scaling. Defaults match a CAMERAD panel int8
        # range (+127 / -127); operator can change to match whatever raw
        # range the calibration was captured against.
        jrow.addWidget(QLabel("low:"))
        self.cmd_joy_low_edit = QLineEdit("-127")
        self.cmd_joy_low_edit.setMaximumWidth(56)
        self.cmd_joy_low_edit.setToolTip(
            "Raw value sent when slider is at -100% (full left/down).")
        jrow.addWidget(self.cmd_joy_low_edit, 0)
        jrow.addWidget(QLabel("high:"))
        self.cmd_joy_high_edit = QLineEdit("127")
        self.cmd_joy_high_edit.setMaximumWidth(56)
        self.cmd_joy_high_edit.setToolTip(
            "Raw value sent when slider is at +100% (full right/up).")
        jrow.addWidget(self.cmd_joy_high_edit, 0)

        self.cmd_joy_lbl = QLabel("0% → raw=0")
        self.cmd_joy_lbl.setMinimumWidth(110)
        self.cmd_joy_lbl.setStyleSheet("font-family: monospace;")
        jrow.addWidget(self.cmd_joy_lbl, 0)
        vl.addRow("Joystick (0x3026 raw):", jrow)
        # Velocity-demand acceleration ramp (ADR-042) -- smooths the joystick. Motor OD 0x2300:6/7.
        jkrow = QHBoxLayout()
        self.cmd_accel_up = QLineEdit()
        self.cmd_accel_up.setPlaceholderText("up rad/s²")
        self.cmd_accel_up.setMaximumWidth(96)
        self.cmd_accel_up.setToolTip("Acceleration ramp while speeding up [rad/s²]. 0 = off. (0x2300:6)")
        self.cmd_accel_up.returnPressed.connect(self._cmd_apply_accel)
        jkrow.addWidget(self.cmd_accel_up)
        self.cmd_accel_dn = QLineEdit()
        self.cmd_accel_dn.setPlaceholderText("down rad/s²")
        self.cmd_accel_dn.setMaximumWidth(96)
        self.cmd_accel_dn.setToolTip("Acceleration ramp while slowing down [rad/s²]. 0 = off. (0x2300:7)")
        self.cmd_accel_dn.returnPressed.connect(self._cmd_apply_accel)
        jkrow.addWidget(self.cmd_accel_dn)
        b_jk = QPushButton("Apply")
        b_jk.clicked.connect(self._cmd_apply_accel)
        jkrow.addWidget(b_jk)
        self.cmd_accel_bypass = QCheckBox("Bypass")
        self.cmd_accel_bypass.setToolTip("Bypass the ramp (writes 0/0 -> raw joystick) to A/B the smoothing. "
                                         "Uncheck to re-apply the fields.")
        self.cmd_accel_bypass.toggled.connect(self._cmd_accel_bypass_toggled)
        jkrow.addWidget(self.cmd_accel_bypass)
        jkrow.addStretch(1)
        vl.addRow("Accel ramp up / down:", jkrow)

        # 25 ms = 40 Hz, matching real-panel MOVEMENT cadence. Started on
        # first connect; runs continuously while connected (sends 0 when
        # slider is centred, just like a panel sends MOVEMENT(0) at idle).
        # Tick writes 0x3026 axis_joystick_raw at the mapped raw value;
        # cmc_state's auto-mode-switch then puts the motor into JOYSTICK
        # mode on the first non-zero tick (or via the operator's Mode combo).
        self.cmd_joy_timer = QTimer(self)
        self.cmd_joy_timer.setInterval(25)
        self.cmd_joy_timer.timeout.connect(self._cmd_joystick_tick)
        self.cmd_joy_timer.start()

        # --- Tuning (on-motor test-signal generator; drives the 0x2910 block, ADR-030) ---
        self.cmd_tuneg = tuneg = QGroupBox("Tuning  (on-motor test signal)")
        tgl = QVBoxLayout(tuneg)
        trow = QHBoxLayout()
        trow.addWidget(QLabel("Mode:"))
        self.cmd_tune_mode = QComboBox()
        self.cmd_tune_mode.addItem("Off", MC_TEST_MODE_OFF)
        self.cmd_tune_mode.addItem("Velocity tuning", MC_TEST_MODE_VELOCITY)
        self.cmd_tune_mode.addItem("Position tuning", MC_TEST_MODE_POSITION)
        self.cmd_tune_mode.addItem("Current tuning", MC_TEST_MODE_CURRENT)
        self.cmd_tune_mode.currentIndexChanged.connect(self._cmd_tune_mode_changed)
        trow.addWidget(self.cmd_tune_mode)
        trow.addStretch(1)
        tgl.addLayout(trow)

        prow = QHBoxLayout()
        prow.addWidget(QLabel("Amplitude:"))
        self.cmd_tune_amp = QLineEdit("2")
        self.cmd_tune_amp.setMaximumWidth(64)
        prow.addWidget(self.cmd_tune_amp)
        self.cmd_tune_amp_unit = QLabel("rad/s")
        self.cmd_tune_amp_unit.setStyleSheet("color: #666;")
        prow.addWidget(self.cmd_tune_amp_unit)
        prow.addWidget(QLabel("   Rate:"))
        self.cmd_tune_rate = QLineEdit("0")
        self.cmd_tune_rate.setMaximumWidth(56)
        self.cmd_tune_rate.setToolTip("Ramp rate; 0 = instant edge (step). Generated cleanly on the "
                                      "motor at 1 kHz (not the old GUI staircase).")
        prow.addWidget(self.cmd_tune_rate)
        self.cmd_tune_rate_unit = QLabel("rad/s²")
        self.cmd_tune_rate_unit.setStyleSheet("color: #666;")
        prow.addWidget(self.cmd_tune_rate_unit)
        prow.addWidget(QLabel("   Dwell:"))
        self.cmd_tune_dwell = QLineEdit("1.0")
        self.cmd_tune_dwell.setMaximumWidth(56)
        self.cmd_tune_dwell.setToolTip("Hold time at the peak [s].")
        prow.addWidget(self.cmd_tune_dwell)
        prow.addWidget(QLabel("s"))
        prow.addWidget(QLabel("   Pause:"))
        self.cmd_tune_pause = QLineEdit("0.5")
        self.cmd_tune_pause.setMaximumWidth(56)
        self.cmd_tune_pause.setToolTip("Continuous mode only: hold at 0 between pulses for this long "
                                       "before firing the reverse pulse. 0 = no pause. (0x2910:9)")
        prow.addWidget(self.cmd_tune_pause)
        prow.addWidget(QLabel("s"))
        prow.addWidget(QLabel("   Max accel:"))
        self.cmd_tune_max_accel = QLineEdit("100")
        self.cmd_tune_max_accel.setMaximumWidth(64)
        self.cmd_tune_max_accel.setToolTip("Position tuning only: bounds the move's acceleration via a "
                                           "trapezoidal velocity profile (rad/s²). 0 = off (impulsive "
                                           "corners). Ignored in velocity tuning, where rate is already "
                                           "the acceleration. (0x2910:10)")
        prow.addWidget(self.cmd_tune_max_accel)
        prow.addWidget(QLabel("rad/s²"))
        prow.addStretch(1)
        tgl.addLayout(prow)

        brow = QHBoxLayout()
        b_fire = QPushButton("Fire")
        b_fire.setToolTip("Write the signal params to 0x2910 and trigger the on-motor generator. "
                          "Be enabled in the matching operational mode first.")
        b_fire.clicked.connect(self._cmd_tune_fire)
        brow.addWidget(b_fire)
        b_tstop = QPushButton("Stop")
        b_tstop.setToolTip("Disarm (test_mode = 0) — the generator ramps the reference bumplessly to 0.")
        b_tstop.clicked.connect(self._cmd_tune_stop)
        brow.addWidget(b_tstop)
        self.cmd_tune_cont = QCheckBox("Continuous")
        self.cmd_tune_cont.setToolTip("Repeat the pulse alternating direction (ping-pong) until stopped.")
        brow.addWidget(self.cmd_tune_cont)
        self.cmd_tune_active = QLabel("idle")
        self.cmd_tune_active.setStyleSheet("font-family: monospace; color: #444;")
        brow.addWidget(self.cmd_tune_active)
        brow.addStretch(1)
        tgl.addLayout(brow)

        hint = QLabel("Selecting a mode also sets the matching operational mode — then <b>Enable</b>, "
                      "then <b>Fire</b>. Graph <code>tlm_vel_demand_rad_s</code> (0x2310:1) vs "
                      "<code>tlm_vel_actual_rad_s</code> (0x2310:2) for velocity tuning.")
        hint.setStyleSheet("color: #666; font-size: 10px;")
        hint.setWordWrap(True)
        tgl.addWidget(hint)

        # --- Position ---
        self.cmd_posg = posg = QGroupBox("Position profile  (Profile Position)")
        pl = QHBoxLayout(posg)
        pl.addWidget(QLabel("Target (rad):"), 0)
        self.cmd_pos_edit = QLineEdit()
        self.cmd_pos_edit.setMinimumWidth(80)
        pl.addWidget(self.cmd_pos_edit, 1)             # main editor takes the slack
        pl.addWidget(QLabel("Time (s, 0=ASAP):"), 0)
        self.cmd_time_edit = QLineEdit("0")
        self.cmd_time_edit.setMinimumWidth(50)
        self.cmd_time_edit.setMaximumWidth(80)         # this one stays narrow (always short string)
        pl.addWidget(self.cmd_time_edit, 0)
        b_move = QPushButton("Move to position")
        b_move.setToolTip("Writes target_position (0x3024) + target_time (0x3025), then "
                          "pulses start_move (0x3013 → NEW_SETPOINT). Be in Profile Position "
                          "mode and enabled.")
        b_move.clicked.connect(self._cmd_move_position)
        pl.addWidget(b_move, 0)

        # --- Current (REQ-0012, CHANGELOG [4.3.0]) ---
        self.cmd_curg = curg = QGroupBox("Current  (Torque mode)")
        cl = QHBoxLayout(curg)
        cl.addWidget(QLabel("Target current (A):"), 0)
        self.cmd_cur_edit = QLineEdit()
        self.cmd_cur_edit.setMinimumWidth(80)
        self.cmd_cur_edit.setToolTip(
            "Commanded current in amps (sign = direction). Written to CMC OD "
            "0x302B axis_target_current; axis_manager SDO-writes motor 0x6071 "
            "target_torque = round(amps / 1e-3). Effective only when "
            "axis_op_mode = Torque (5) and axis_enable = 1.")
        cl.addWidget(self.cmd_cur_edit, 1)             # editor stretches
        b_ac = QPushButton("Apply current")
        b_ac.clicked.connect(self._cmd_apply_current)
        cl.addWidget(b_ac, 0)

        # --- Feedback (motor actuals) ---
        self.cmd_fbg = fbg = QGroupBox("Feedback  (motor actuals)")
        fl = QFormLayout(fbg)
        self.cmd_fb_pos = QLabel("-")
        self.cmd_fb_vel = QLabel("-")
        self.cmd_fb_torque = QLabel("-")
        self.cmd_fb_reached = QLabel("-")
        for lbl in (self.cmd_fb_pos, self.cmd_fb_vel, self.cmd_fb_torque, self.cmd_fb_reached):
            lbl.setStyleSheet("font-family: monospace;")
        fl.addRow("Position (0x6064):", self.cmd_fb_pos)
        fl.addRow("Velocity (0x606C):", self.cmd_fb_vel)
        fl.addRow("Torque (0x6077):", self.cmd_fb_torque)
        fl.addRow("Target reached:", self.cmd_fb_reached)
        # Entry-point groups in display order: position, velocity, current, tuning, feedback.
        for g in (self.cmd_posg, self.cmd_velg, self.cmd_curg, self.cmd_tuneg, self.cmd_fbg):
            outer.addWidget(g)
        self._cmd_update_entry_enables()

        outer.addStretch(1)
        return w

    # --- command helpers ---
    def _cmd_write(self, key: tuple[int, int], value, what: str = "") -> None:
        if not self.client.connected:
            QMessageBox.warning(self, "Not connected", "Connect to the CMC first.")
            return
        entry = self.od.by_key.get(key)
        if entry is None:
            QMessageBox.warning(self, "Missing OD entry", f"0x{key[0]:04X}:{key[1]} not in OD.")
            return
        try:
            raw = entry.si_to_raw(float(value)) if (entry.is_float or entry.scaled) else int(value)
        except (ValueError, TypeError):
            self._log(f"CMD {what or entry.name}: bad value '{value}'", "WARN")
            return
        self._log(f"CMD {what or entry.name} = {value}", "TX")
        self.client.write_async(entry, raw)

    def _cmd_update_entry_enables(self, *_) -> None:
        """Grey out the entry-point groups that the selected mode doesn't use."""
        mode = int(self.cmd_mode_combo.currentData())
        self.cmd_posg.setEnabled(mode == MC_AXIS_MODE_PROFILE_POSITION)
        self.cmd_velg.setEnabled(mode in (MC_AXIS_MODE_PROFILE_VELOCITY, MC_AXIS_MODE_JOYSTICK))
        self.cmd_curg.setEnabled(mode == MC_AXIS_MODE_TORQUE)
        # tuning + feedback stay available in every mode.

    def _cmd_read_mode(self) -> None:
        """Read the active op mode (axis_op_mode_actual, 0x3001) -> the 'Active mode' label."""
        if not self.client.connected:
            QMessageBox.warning(self, "Not connected", "Connect to the CMC first.")
            return
        entry = self.od.by_key.get((0x3001, 0))
        if entry is not None and entry.readable:
            self.client.read_async(entry)

    def _cmd_set_mode(self) -> None:
        self._cmd_write((0x3020, 0), int(self.cmd_mode_combo.currentData()), "axis_op_mode")

    def _cmd_enable(self, on: bool) -> None:
        self._cmd_write((0x3010, 0), 1 if on else 0, "axis_enable")

    def _cmd_quick_stop(self) -> None:
        self._cmd_write((0x3011, 0), 1, "axis_quick_stop")

    def _cmd_clear_fault(self) -> None:
        self._cmd_write((0x3012, 0), 1, "axis_clear_fault")

    def _cmd_accel_bypass_toggled(self, on: bool) -> None:
        """Bypass = write 0/0 to the accel ramp (raw joystick); un-bypass = re-apply the fields above."""
        if on:
            self._cmd_write((0x2300, 6), 0, "vel_accel_up")
            self._cmd_write((0x2300, 7), 0, "vel_accel_dn")
        else:
            self._cmd_apply_accel()

    def _cmd_apply_accel(self) -> None:
        """Write the velocity-demand acceleration ramp limits (0x2300:6/7, ADR-042)."""
        for edit, key, name in ((self.cmd_accel_up, (0x2300, 6), "vel_accel_up"),
                                (self.cmd_accel_dn, (0x2300, 7), "vel_accel_dn")):
            t = edit.text().strip()
            if t:
                self._cmd_write(key, t, name)

    def _cmd_apply_velocity(self) -> None:
        text = self.cmd_vel_edit.text().strip()
        if text:
            self._cmd_write((0x3023, 0), text, "target_velocity")

    def _cmd_apply_current(self) -> None:
        # REQ-0012: writes CMC 0x302B axis_target_current (amps). CMC's
        # axis_manager translates to motor 0x6071 target_torque on its next
        # setup_sequencer tick. Effective only in Torque mode (op_mode = 5).
        text = self.cmd_cur_edit.text().strip()
        if text:
            self._cmd_write((0x302B, 0), text, "target_current")

    def _cmd_joy_map_raw(self, v_pct: int) -> int | None:
        """Map slider percent (-100..+100) to a raw int using the user's
        high/low inputs. Asymmetric mapping: positive percent scales toward
        high, negative toward low. Returns None if the fields are invalid
        — caller skips this tick rather than spamming the CMC with bad
        values."""
        try:
            high = int(self.cmd_joy_high_edit.text())
            low  = int(self.cmd_joy_low_edit.text())
        except (ValueError, TypeError):
            return None
        if v_pct >= 0:
            return int(round(v_pct / 100.0 * high))
        else:
            return int(round(-v_pct / 100.0 * low))

    def _cmd_joystick_changed(self, v: int) -> None:
        # Only refresh the readout. Network write happens on the 25 ms timer
        # — this avoids two writes per drag tick and keeps the cadence even
        # when the slider is idle (matches a real panel).
        raw = self._cmd_joy_map_raw(v)
        self.cmd_joy_lbl.setText(f"{v:+d}% → raw={raw if raw is not None else '?'}")

    def _cmd_joystick_tick(self) -> None:
        """Fires every 25 ms (40 Hz) while the GUI is alive. When connected,
        sends the slider's current mapped raw to OD 0x3026 — exactly the
        path a CAMERAD panel takes. Logs go to the protocol debug stream,
        not the GUI's debug console (40 Hz × 60 = 2400 lines/min would
        drown everything else)."""
        if not self.client.connected:
            return
        raw = self._cmd_joy_map_raw(self.cmd_joy_slider.value())
        if raw is None:
            return
        entry = self.od.by_key.get((0x3026, 0))
        if entry is None:
            return
        # write_async directly — bypass _cmd_write so we don't log per tick.
        # The CMC's cmc_od logs 0x3026 writes only when they pass
        # is_loggable_write, which excludes this high-rate setpoint path.
        self.client.write_async(entry, raw)

    def _cmd_move_position(self) -> None:
        pos = self.cmd_pos_edit.text().strip()
        if not pos:
            return
        tm = self.cmd_time_edit.text().strip() or "0"
        self._cmd_write((0x3024, 0), pos, "target_position")
        self._cmd_write((0x3025, 0), tm, "target_time")
        self._cmd_write((0x3013, 0), 1, "start_move")

    def _cmd_poll_state(self) -> None:
        if not self.client.connected:
            return
        for key in _CMD_STATE_KEYS:
            entry = self.od.by_key.get(key)
            if entry and entry.readable:
                self.client.read_async(entry)

    @staticmethod
    def _cmd_sw_decode(sw: int) -> str:
        bits = []
        if sw & 0x0008:
            bits.append("FAULT")
        if sw & 0x0004:
            bits.append("ENABLED")
        if sw & 0x0001:
            bits.append("READY")
        if sw & 0x0400:
            bits.append("TARGET_REACHED")
        if sw & 0x0800:
            bits.append("LIMIT")
        return f"0x{sw:04X}  " + (" ".join(bits) if bits else "-")

    def _cmd_on_state_read(self, res: dict) -> None:
        if not res.get("ok"):
            return
        key = res["entry"].key
        if key == (0x3000, 0):
            self.cmd_state_lbl.setText(_AXIS_STATE_NAMES.get(int(res["raw"]), str(res["raw"])))
        elif key == (0x3001, 0):
            self.cmd_mode_actual_lbl.setText(dict(_AXIS_MODES).get(int(res["raw"]), str(res["raw"])))
        elif key == (0x6041, 0):
            sw = int(res["raw"])
            self.cmd_sw_lbl.setText(self._cmd_sw_decode(sw))
            self.cmd_fb_reached.setText("yes" if (sw & 0x0400) else "no")
        elif key == (0x6064, 0):
            self.cmd_fb_pos.setText(f"{res['si']:.5g} rad")
        elif key == (0x606C, 0):
            self.cmd_fb_vel.setText(f"{res['si']:.5g} rad/s")
        elif key == (0x6077, 0):
            self.cmd_fb_torque.setText(f"{res['si']:.5g} A")
        elif key == (0x2910, 7):   # test_active (tuning generator running)
            self.cmd_tune_active.setText("RUNNING" if int(res["raw"]) else "idle")
        elif key == (0x2410, 6):   # measured armature current -> Motor Config readout
            self.mcfg_cur_lbl.setText(f"{res['si']:.3f} A")
        elif key == (0x2400, 6):   # derived brushed kp (RO) -> Motor Config readout
            self.mcfg_kp_lbl.setText(f"{res['si']:.3g}")
        elif key == (0x2400, 7):   # derived brushed ki (RO) -> Motor Config readout
            self.mcfg_ki_lbl.setText(f"{res['si']:.4g}")
        elif key == (0x2000, 6):   # configured drive backend -> Motor Config readout
            be = int(res["raw"])
            self.mcfg_backend_lbl.setText("Brushed DC (H-bridge)" if be else "BLDC / PMSM (FOC)")
            idx = self.mcfg_backend.findData(be)
            if idx >= 0:
                self.mcfg_backend.setCurrentIndex(idx)

    # --- velocity-pulse generator (GUI-side; drives 0x3023 over time) ---
    def _cmd_tune_mode_changed(self) -> None:
        """Selecting a tuning mode writes test_mode (0x2910:1) and sets the matching operational mode
        for the operator (velocity-tuning -> Profile Velocity; position-tuning -> Profile Position).
        Also relabels the amplitude/rate units to the selected domain."""
        mode = int(self.cmd_tune_mode.currentData())
        if mode == MC_TEST_MODE_POSITION:
            self.cmd_tune_amp_unit.setText("rad")
            self.cmd_tune_rate_unit.setText("rad/s")
        elif mode == MC_TEST_MODE_CURRENT:
            self.cmd_tune_amp_unit.setText("A")
            self.cmd_tune_rate_unit.setText("A/s")
        else:
            self.cmd_tune_amp_unit.setText("rad/s")
            self.cmd_tune_rate_unit.setText("rad/s²")
        if not self.client.connected:
            return
        self._cmd_write((0x2910, 1), mode, "test_mode")
        if mode == MC_TEST_MODE_VELOCITY:
            self._cmd_set_op_mode(MC_AXIS_MODE_PROFILE_VELOCITY)
        elif mode == MC_TEST_MODE_POSITION:
            self._cmd_set_op_mode(MC_AXIS_MODE_PROFILE_POSITION)
        elif mode == MC_TEST_MODE_CURRENT:
            self._cmd_set_op_mode(MC_AXIS_MODE_TORQUE)

    def _cmd_set_op_mode(self, mode_val: int) -> None:
        """Point the operational-mode combo at mode_val and write axis_op_mode (0x3020)."""
        for i in range(self.cmd_mode_combo.count()):
            if self.cmd_mode_combo.itemData(i) == mode_val:
                self.cmd_mode_combo.setCurrentIndex(i)
                break
        self._cmd_write((0x3020, 0), mode_val, "axis_op_mode")

    def _cmd_tune_fire(self) -> None:
        if not self.client.connected:
            QMessageBox.warning(self, "Not connected", "Connect to the CMC first.")
            return
        mode = int(self.cmd_tune_mode.currentData())
        if mode == MC_TEST_MODE_OFF:
            QMessageBox.information(
                self, "Select a tuning mode",
                "Pick Velocity, Position, or Current tuning first (it also sets the operational mode); "
                "then Enable the drive and Fire.")
            return
        try:
            amp = float(self.cmd_tune_amp.text())
            rate = max(0.0, float(self.cmd_tune_rate.text() or "0"))
            dwell = max(0.0, float(self.cmd_tune_dwell.text()))
            pause = max(0.0, float(self.cmd_tune_pause.text() or "0"))
            max_accel = max(0.0, float(self.cmd_tune_max_accel.text() or "0"))
        except ValueError:
            QMessageBox.warning(self, "Bad value", "Amplitude / rate / dwell / pause / max accel must be numbers.")
            return
        cont = 1 if self.cmd_tune_cont.isChecked() else 0
        self._log(f"TUNE fire: mode={mode} amp={amp} rate={rate} dwell={dwell} pause={pause} "
                  f"accel={max_accel} cont={cont}", "TX")
        self._cmd_write((0x2910, 1), mode, "test_mode")
        self._cmd_write((0x2910, 2), amp, "test_amplitude")
        self._cmd_write((0x2910, 3), rate, "test_rate")
        self._cmd_write((0x2910, 4), dwell, "test_dwell_s")
        self._cmd_write((0x2910, 9), pause, "test_pause_s")
        self._cmd_write((0x2910, 10), max_accel, "test_max_accel")
        self._cmd_write((0x2910, 5), cont, "test_continuous")
        self._cmd_write((0x2910, 6), 1, "test_trigger")

    def _cmd_tune_stop(self) -> None:
        """Disarm: select Off and write test_mode = 0; the motor ramps the reference bumplessly to 0."""
        self.cmd_tune_mode.blockSignals(True)
        self.cmd_tune_mode.setCurrentIndex(0)
        self.cmd_tune_mode.blockSignals(False)
        self.cmd_tune_amp_unit.setText("rad/s")
        self.cmd_tune_rate_unit.setText("rad/s²")
        if self.client.connected:
            self._cmd_write((0x2910, 1), MC_TEST_MODE_OFF, "test_mode")

    def _build_rw_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.sel_label = QLabel("Select an OD entry")
        self.sel_label.setWordWrap(True)
        self.sel_label.setStyleSheet("font-weight: bold;")
        lay.addWidget(self.sel_label)

        form = QFormLayout()
        self.sel_value = QLabel("-")
        form.addRow("Current value:", self.sel_value)
        self.write_edit = QLineEdit()
        self.write_edit.setPlaceholderText("value to write (SI units; hex ok, e.g. 0x06)")
        self.write_edit.returnPressed.connect(self._do_write)
        form.addRow("Write value:", self.write_edit)
        lay.addLayout(form)

        btns = QHBoxLayout()
        self.btn_read = QPushButton("Read")
        self.btn_read.clicked.connect(self._do_read)
        btns.addWidget(self.btn_read)
        self.btn_write = QPushButton("Write")
        self.btn_write.clicked.connect(self._do_write)
        btns.addWidget(self.btn_write)
        lay.addLayout(btns)

        self.rw_result = QLabel("")
        self.rw_result.setWordWrap(True)
        lay.addWidget(self.rw_result)
        lay.addStretch(1)
        self._current_entry: OdEntry | None = None
        return w

    # === CMC Setup tab =======================================================
    #
    # Edit the CMC-owned PERSIST configuration entries (joystick calibration +
    # motion limits) in one place. Each row shows the current value, an editor,
    # and an "Apply" button that writes via OD-over-UDP. "Read all" refreshes
    # every row. "Save to flash" is disabled until bsp/flash lands on the CMC
    # — until then writes are RAM-only and lost on reboot (the warning at the
    # top of the tab spells that out).
    def _build_setup_panel(self) -> QWidget:
        # Hardcoded list of which OD entries belong here. Grouped by purpose,
        # in the order they should appear. Keys are (index, sub).
        groups = [
            ("Joystick calibration", [
                (0x3022, 0),   # axis_joystick_max_velocity (rad/s)
                (0x3027, 0),   # axis_joystick_raw_center
                (0x3028, 0),   # axis_joystick_raw_full_pos
                (0x3029, 0),   # axis_joystick_raw_full_neg
                (0x302A, 0),   # axis_joystick_raw_deadband
            ]),
            ("Motion limits", [
                (0x3030, 0),   # axis_velocity_limit (rad/s)
                (0x3031, 0),   # axis_position_limit_lo (rad)
                (0x3032, 0),   # axis_position_limit_hi (rad)
                (0x3033, 0),   # axis_accel_limit (rad/s^2)
            ]),
        ]

        w = QWidget()
        outer = QVBoxLayout(w)

        warn = QLabel(
            "<b>Persistence:</b> edits land in CMC RAM immediately on <b>Apply</b>. "
            "Click <b>Save to flash</b> to commit the current values to the CMC's internal "
            "flash — they'll survive a reboot. Saved state is restored automatically at the "
            "next CMC boot."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #115522; padding: 6px; background: #f0f9f0; border: 1px solid #c0e0c0;")
        outer.addWidget(warn)

        # Per-row widget store, so read/write callbacks can update the right row.
        # key -> {"current": QLabel, "editor": QLineEdit, "status": QLabel}
        self._setup_rows: dict[tuple[int, int], dict] = {}

        for group_title, keys in groups:
            box = QGroupBox(group_title)
            grid = QFormLayout(box)
            for key in keys:
                entry = self.od.by_key.get(key)
                if entry is None:
                    grid.addRow(f"0x{key[0]:04X}:{key[1]}", QLabel("<not in OD header>"))
                    continue
                row_widgets = self._make_setup_row(entry)
                grid.addRow(self._setup_row_label(entry), row_widgets["container"])
                self._setup_rows[key] = row_widgets
            outer.addWidget(box)

        # CMC on-board indicator LED (PC0/PC1/PC2 = TIM1_CH1/2/3 PWM).
        # Lives on this page because LED colour rides the same axis_persist
        # blob as the other operator tunables — the "Save to flash" button
        # below commits LED + joystick + limits in one shot. The CMC firmware
        # drives the pattern (boot solid -> network-link 3x flash -> breathing
        # while motor moves / idle solid); the operator just picks the colour.
        outer.addWidget(self._build_led_panel())

        # Bottom buttons row
        btns = QHBoxLayout()
        b_read_all = QPushButton("Read all")
        b_read_all.clicked.connect(self._setup_read_all)
        btns.addWidget(b_read_all)

        # Save to flash: writes MC_IF_SAVE_MAGIC (0x7376) to OD 0x3050
        # cmc_save_config. The CMC's cmc_od dispatches to
        # axis_manager_save_to_flash(), which serialises the PERSIST-flagged
        # config (joystick cal + motion limits) via app/persist + bsp/flash.
        b_save = QPushButton("Save to flash")
        b_save.setToolTip(
            "Commit the values above to the CMC's internal flash. They will\n"
            "survive a CMC reboot. Writes OD 0x3050 = 0x7376 (MC_IF_SAVE_MAGIC).\n"
            "Blocks the CMC for ~30 ms (one flash page erase)."
        )
        b_save.clicked.connect(self._setup_save_to_flash)
        btns.addWidget(b_save)
        btns.addStretch(1)
        outer.addLayout(btns)

        outer.addStretch(1)
        return w

    def _setup_row_label(self, entry: OdEntry) -> str:
        unit = f" [{entry.unit}]" if entry.unit else ""
        return f"{entry.name}{unit}\n  {entry.id_str}  {entry.type_name}"

    def _make_setup_row(self, entry: OdEntry) -> dict:
        """Build a single setup row. Returns dict of the widgets so the read/
        write callbacks can update the current-value / status labels.

        Row layout: [ current value ] [ editor ] [ unit ] [ Apply ] [ status ]
        Units are shown next to the editor so the operator sees rad/s, rad,
        rad/s^2, etc. while typing.
        """
        container = QWidget()
        h = QHBoxLayout(container)
        h.setContentsMargins(0, 0, 0, 0)

        # Row layout uses HBox stretch factors to let the editor and status
        # fields absorb extra horizontal space when the side panel is wide,
        # while the static labels stay sized to their content. This replaces
        # the previous pattern of setMaximumWidth on the editor + trailing
        # addStretch which kept the editor pinned narrow regardless of panel
        # width, making the Motor Config tab look cramped at any window size.
        current = QLabel("-")
        current.setMinimumWidth(80)
        current.setStyleSheet("font-family: monospace; color: #444;")
        h.addWidget(current, 0)

        editor = QLineEdit()
        editor.setPlaceholderText("new value")
        editor.setMinimumWidth(80)            # never collapse to nothing
        # NO setMaximumWidth — let it grow with the panel.
        editor.returnPressed.connect(lambda e=entry: self._setup_apply(e.key))
        h.addWidget(editor, 1)                # stretch factor 1: this is the row's growable cell

        # Unit label sits between the editor and the Apply button so the
        # operator sees the expected units (rad/s, rad, etc.) while typing.
        unit_text = entry.unit if entry.unit else f"({entry.type_name})"
        unit_lbl = QLabel(unit_text)
        unit_lbl.setStyleSheet("color: #666; font-style: italic;")
        unit_lbl.setMinimumWidth(50)
        h.addWidget(unit_lbl, 0)

        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(lambda _checked=False, e=entry: self._setup_apply(e.key))
        h.addWidget(apply_btn, 0)

        status = QLabel("")
        status.setStyleSheet("color: #666; font-size: 10px;")
        status.setMinimumWidth(60)
        h.addWidget(status, 0)                # static-width label, no stretch
        # Removed trailing addStretch — the editor cell now does the stretching.
        return {
            "container": container,
            "current":   current,
            "editor":    editor,
            "status":    status,
        }

    def _setup_read_all(self) -> None:
        if not self.client.connected:
            self._log("CMC Setup: not connected", "WARN")
            return
        for key in self._setup_rows.keys():
            entry = self.od.by_key.get(key)
            if entry and entry.readable:
                self.client.read_async(entry)

    def _cfg_row(self, key: tuple[int, int]) -> dict | None:
        """Look up a config-row widget set across the CMC Setup and Motor Config
        tabs. Both tabs use identical row plumbing (_make_setup_row + the
        _setup_on_*_done callbacks); their keys are disjoint (motor 0x2xxx/0x6xxx
        vs CMC 0x3xxx) so a single lookup is unambiguous."""
        row = getattr(self, "_setup_rows", {}).get(key)
        if row is not None:
            return row
        return getattr(self, "_mcfg_rows", {}).get(key)

    def _setup_apply(self, key: tuple[int, int]) -> None:
        row = self._cfg_row(key)
        if row is None:
            return
        if not self.client.connected:
            row["status"].setText("not connected")
            return
        entry = self.od.by_key.get(key)
        if entry is None:
            return
        text = row["editor"].text().strip()
        if not text:
            row["status"].setText("empty")
            return
        try:
            if entry.is_float or entry.scaled:
                raw = entry.si_to_raw(float(text))
            else:
                raw = int(text, 0)
        except ValueError:
            row["status"].setText("parse error")
            row["status"].setStyleSheet("color: #c33; font-size: 10px;")
            return
        row["status"].setText("writing…")
        row["status"].setStyleSheet("color: #666; font-size: 10px;")
        self.client.write_async(entry, raw)
        self._log(f"SETUP WRITE {entry.label} = {text}", "TX")
        if key in ((0x2000, 3), (0x2000, 4), (0x2400, 8)):  # R / L / bandwidth -> re-read derived gains
            QTimer.singleShot(100, self._mcfg_read_gains)

    # Write MC_IF_SAVE_MAGIC (0x7376) to OD 0x3050 cmc_save_config.
    # The CMC's cmc_od dispatches to axis_manager_save_to_flash() which
    # commits the PERSIST-flagged config to the on-CMC flash via app/persist.
    # Status feedback comes back through _on_write_done and ends up in the
    # Read/Write panel's result label (we don't currently have a dedicated
    # status line on the Setup tab for the save itself).
    _SAVE_MAGIC = 0x7376
    def _setup_save_to_flash(self) -> None:
        if not self.client.connected:
            self._log("CMC Setup: not connected — can't save", "WARN")
            return
        save_entry = self.od.by_key.get((0x3050, 0))
        if save_entry is None:
            self._log("CMC Setup: OD 0x3050 cmc_save_config missing — rebuild GUI "
                      "against the latest mc_if_od.h", "ERROR")
            return
        self._log("SETUP SAVE -> CMC flash (writing OD 0x3050 = SAVE_MAGIC)", "TX")
        self.client.write_async(save_entry, self._SAVE_MAGIC)

    def _setup_on_read_done(self, entry: OdEntry, ok: bool, text: str, err: str) -> None:
        row = self._cfg_row(entry.key)
        if row is None:
            return
        if ok:
            row["current"].setText(text)
            # Don't overwrite a "wrote OK" status with a read-back result;
            # only clear an in-flight "writing…" once the value lands.
            if row["status"].text() in ("", "writing…"):
                row["status"].setText("")
        else:
            row["current"].setText(f"<{err}>")
            row["current"].setStyleSheet("font-family: monospace; color: #c33;")

    def _setup_on_write_done(self, entry: OdEntry, ok: bool, err: str) -> None:
        row = self._cfg_row(entry.key)
        if row is None:
            return
        if ok:
            row["status"].setText("written ✓")
            row["status"].setStyleSheet("color: #060; font-size: 10px;")
            # The existing _on_write_done will trigger a read-back which will
            # then update the "current" cell via _setup_on_read_done. Clear
            # the editor so the user knows the staged value was consumed.
            row["editor"].clear()
        else:
            row["status"].setText(f"fail: {err}")
            row["status"].setStyleSheet("color: #c33; font-size: 10px;")

    # === Motor Config tab ====================================================
    #
    # One place to set the motor MCU's PERSIST configuration (motor model, the
    # three control-loop gain sets, estimator, fault/profile/alignment params)
    # and to fire the OD-triggered calibration + persistence commands. Editable
    # rows reuse the CMC-Setup row plumbing (_make_setup_row + _setup_apply +
    # the _setup_on_*_done callbacks, routed by _cfg_row); the calibration and
    # persistence buttons write the command/magic codes directly. Unlike the
    # CMC, the motor MCU has flash persistence, so "Save to flash" is live here.
    #
    # Grouped (index, sub) keys, in display order. Every config entry is
    # motor-owned and PERSIST; the action entries (0x2700:1, 0x2800:*) are
    # commands handled by the buttons below rather than editable rows.
    _MOTORCFG_GROUPS = [
        ("Motor model (0x2000)", [
            (0x2000, 1), (0x2000, 2), (0x2000, 3), (0x2000, 4), (0x2000, 5),
        ]),
        ("Position loop gains (0x2200)", [
            (0x2200, 1), (0x2200, 2), (0x2200, 3), (0x2200, 4),  # :4 = velocity_ff_gain (ADR-031)
        ]),
        ("Velocity loop gains (0x2300)", [
            (0x2300, 1), (0x2300, 2), (0x2300, 3), (0x2300, 4),
            (0x2300, 5),  # vel_load_factor (REQ-0014) — operator load multiplier on kp/ki
            (0x2300, 6), (0x2300, 7),   # velocity-demand jerk limits (ADR-042)
        ]),
        ("Current loop gains (0x2400)", [
            (0x2400, 1), (0x2400, 2), (0x2400, 3), (0x2400, 4), (0x2400, 5),
            (0x2400, 8),   # brushed current-loop bandwidth wc -> derives kp/ki (ADR-039)
        ]),
        ("State estimator (0x2500)", [
            # 0x2500:1 est_electrical_offset is a calibration RESULT (set by the align routine), not an
            # editable config -- it would be overwritten if set, so it's not an editable row here.
            (0x2500, 2), (0x2500, 3), (0x2500, 4), (0x2500, 5), (0x2500, 6),
        ]),
        ("Faults / limits (0x2600)", [
            (0x2600, 2),
            (0x2600, 4), (0x2600, 5),   # motor safety envelope: vel/accel ceiling (ADR-040; 0 = disabled)
            (0x2600, 6), (0x2600, 7),   # soft position limits, home-rel (ADR-040; lo>=hi = disabled)
        ]),
        ("Motion profile (CiA-402)", [
            (0x6081, 0), (0x6083, 0), (0x6084, 0), (0x6085, 0),
        ]),
        ("Electrical-alignment parameters (0x2700)", [
            (0x2700, 3), (0x2700, 4),
        ]),
    ]

    def _build_motorcfg_panel(self) -> QWidget:
        # key -> row-widget dict, for the read/write plumbing (see _cfg_row).
        self._mcfg_rows: dict[tuple[int, int], dict] = {}
        self._mcfg_keys: list[tuple[int, int]] = []
        # RO status entries (cal_status / store_status) -> their display QLabel.
        self._mcfg_status: dict[tuple[int, int], QLabel] = {}

        # --- scrollable column of config groups ---
        inner = QWidget()
        col = QVBoxLayout(inner)
        intro = QLabel(
            "Motor-controller configuration (all PERSIST) and calibration. Edit a "
            "field and click <b>Apply</b> to write it live; <b>Save to flash</b> "
            "commits the current values so they survive a power cycle. <b>Read all</b> "
            "refreshes every field from the controller."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("padding: 4px; color: #234;")
        col.addWidget(intro)

        # Drive backend selector (0x2000:6) -- per-board; applied at boot, so save + reboot to take effect.
        bb = QGroupBox("Drive backend")
        bbl = QHBoxLayout(bb)
        bbl.addWidget(QLabel("Backend:"))
        self.mcfg_backend = QComboBox()
        self.mcfg_backend.addItem("BLDC / PMSM (FOC, 3-shunt)", 0)
        self.mcfg_backend.addItem("Brushed DC (H-bridge)", 1)
        bbl.addWidget(self.mcfg_backend)
        b_bset = QPushButton("Set + save…")
        b_bset.setToolTip("Write motor_backend_sel (0x2000:6). It selects the drive path and the "
                          "current-sense ADC channel at boot, so Save to flash and power-cycle to apply.")
        b_bset.clicked.connect(self._mcfg_set_backend)
        bbl.addWidget(b_bset)
        bbl.addSpacing(16)
        bbl.addWidget(QLabel("Configured:"))
        self.mcfg_backend_lbl = QLabel("— (read on connect)")
        self.mcfg_backend_lbl.setStyleSheet("font-weight: bold;")
        self.mcfg_backend_lbl.setToolTip("What the motor reports for motor_backend_sel (0x2000:6) — "
                                         "the persisted selection; applies on reboot.")
        bbl.addWidget(self.mcfg_backend_lbl)
        bbl.addStretch(1)
        col.addWidget(bb)

        # Live current-loop readouts. Measured current streams via the telemetry map (no polling); the
        # derived gains are read once on connect + ~100 ms after an R/L/bandwidth edit.
        cg = QGroupBox("Live current loop")
        cgl = QHBoxLayout(cg)
        cgl.addWidget(QLabel("Armature current:"))
        self.mcfg_cur_lbl = QLabel("—")
        self.mcfg_cur_lbl.setStyleSheet("font-family: monospace; font-weight: bold;")
        self.mcfg_cur_lbl.setToolTip("Updates only while tlm_i_arm_a (0x2410:6) is in the telemetry "
                                     "map (it's in the default map; re-add it on the Telemetry tab if "
                                     "you customise the map).")
        cgl.addWidget(self.mcfg_cur_lbl)
        note = QLabel("(add tlm_i_arm_a to telemetry map for live)")
        note.setStyleSheet("color:#888; font-size:10px;")
        cgl.addWidget(note)
        cgl.addSpacing(16)
        cgl.addWidget(QLabel("derived kp:"))
        self.mcfg_kp_lbl = QLabel("—")
        self.mcfg_kp_lbl.setStyleSheet("font-family: monospace;")
        cgl.addWidget(self.mcfg_kp_lbl)
        cgl.addSpacing(8)
        cgl.addWidget(QLabel("ki:"))
        self.mcfg_ki_lbl = QLabel("—")
        self.mcfg_ki_lbl.setStyleSheet("font-family: monospace;")
        cgl.addWidget(self.mcfg_ki_lbl)
        gnote = QLabel("(read after edit)")
        gnote.setStyleSheet("color:#888; font-size:10px;")
        cgl.addWidget(gnote)
        cgl.addStretch(1)
        col.addWidget(cg)

        for group_title, keys in self._MOTORCFG_GROUPS:
            box = QGroupBox(group_title)
            grid = QFormLayout(box)
            if group_title.startswith("Motion profile"):
                note = QLabel(
                    "Note: profile velocity / accel / decel (0x6081 / 0x6083 / 0x6084) are "
                    "<b>mirrored from the CMC</b> (axis_velocity_limit 0x3030, axis_accel_limit "
                    "0x3033) — set and save them on the <b>CMC Setup</b> tab. Values written here "
                    "are overwritten by the CMC on its next sync, so a motor-side save of these "
                    "will not stick. (quick_stop_deceleration 0x6085 is motor-owned and does persist here.)")
                note.setWordWrap(True)
                note.setStyleSheet("color:#b06000; padding:2px;")
                grid.addRow(note)
            for key in keys:
                entry = self.od.by_key.get(key)
                if entry is None:
                    grid.addRow(f"0x{key[0]:04X}:{key[1]}", QLabel("<not in OD header>"))
                    continue
                row_widgets = self._make_setup_row(entry)
                grid.addRow(self._setup_row_label(entry), row_widgets["container"])
                self._mcfg_rows[key] = row_widgets
                self._mcfg_keys.append(key)
            col.addWidget(box)

        col.addWidget(self._build_motorcfg_actions())
        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)

        # --- persistent bottom bar (always visible, outside the scroll) ---
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(scroll, 1)

        bar = QHBoxLayout()
        b_read_all = QPushButton("Read all")
        b_read_all.clicked.connect(self._mcfg_read_all)
        bar.addWidget(b_read_all)
        b_save = QPushButton("Save to flash")
        b_save.setToolTip(
            "Write SAVE_MAGIC (0x7376) to 0x2800:1 — commits every PERSIST value to "
            "the motor's flash. Flash can only be written with the power stage OFF, so the "
            "save commits when the drive is DISABLED — the status shows 'SAVE PENDING' "
            "until then, then 'Saved to flash'.")
        b_save.clicked.connect(self._mcfg_save)
        bar.addWidget(b_save)
        bar.addStretch(1)
        self.lbl_store_status = QLabel("store: -")
        self.lbl_store_status.setStyleSheet("font-family: monospace; color: #444;")
        bar.addWidget(self.lbl_store_status)
        lay.addLayout(bar)

        # Route the RO status entries to their labels (read-back in _on_read_done).
        if self.od.by_key.get((0x2700, 2)) is not None:
            self._mcfg_status[(0x2700, 2)] = self.lbl_cal_status
        if self.od.by_key.get((0x2700, 5)) is not None:
            self._mcfg_status[(0x2700, 5)] = self.lbl_cal_done
        if self.od.by_key.get((0x2800, 2)) is not None:
            self._mcfg_status[(0x2800, 2)] = self.lbl_store_status
        return w

    def _build_led_panel(self) -> QWidget:
        """RGB sliders for the CMC's on-board status LED (OD 0x3060/61/62).

        Each slider sends a write to its OD entry on release (sliderReleased
        signal) — not on every drag tick, so we don't flood the CMC with
        writes while the operator is dragging. Live colour swatch updates
        on drag. "Save to flash" writes MC_IF_SAVE_MAGIC to 0x3050 which is
        the same trigger the existing Motor Config save uses (rides the
        axis_persist blob bumped to v4 for the LED bytes).
        """
        box = QGroupBox("Indicator LED  (CMC on-board RGB)")
        col = QVBoxLayout(box)
        col.addWidget(QLabel(
            "Pick a colour for the CMC's status LED. The CMC firmware drives "
            "the pattern (solid at boot → 3-flash on network link → breathing "
            "while the motor moves → idle solid); you're just choosing the hue."))

        self._led_sliders: dict[str, QSlider] = {}
        self._led_value_labels: dict[str, QLabel] = {}
        for channel, idx in (("R", 0x3060), ("G", 0x3061), ("B", 0x3062)):
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{channel}:"))
            s = QSlider(Qt.Orientation.Horizontal)
            s.setRange(0, 255)
            s.setValue(0)
            s.valueChanged.connect(self._led_on_slider_drag)
            # Apply on release so we don't write 255 OD-writes during a drag.
            s.sliderReleased.connect(
                lambda ch=channel, idx=idx: self._led_apply(ch, idx))
            row.addWidget(s, 1)
            v = QLabel("0")
            v.setMinimumWidth(36)
            v.setStyleSheet("font-family: monospace;")
            row.addWidget(v, 0)
            col.addLayout(row)
            self._led_sliders[channel] = s
            self._led_value_labels[channel] = v

        # Live preview swatch + Save button row.
        bar = QHBoxLayout()
        self._led_swatch = QLabel("                    ")
        self._led_swatch.setFixedHeight(28)
        self._led_swatch.setStyleSheet("background-color: rgb(0,0,0); border: 1px solid #888;")
        bar.addWidget(self._led_swatch, 1)
        b_read = QPushButton("Read LED")
        b_read.setToolTip("Read current 0x3060/61/62 from the CMC into the sliders.")
        b_read.clicked.connect(self._led_read_all)
        bar.addWidget(b_read, 0)
        b_save = QPushButton("Save to flash")
        b_save.setToolTip(
            "Writes MC_IF_SAVE_MAGIC (0x7376) to 0x3050 cmc_save_config — "
            "commits LED colour + every other axis_persist value to flash.")
        b_save.clicked.connect(self._led_save)
        bar.addWidget(b_save, 0)
        col.addLayout(bar)
        return box

    def _led_on_slider_drag(self, _v: int) -> None:
        # Update the value labels + preview swatch on every tick of the drag.
        # No network write here — that happens on sliderReleased.
        r = self._led_sliders["R"].value()
        g = self._led_sliders["G"].value()
        b = self._led_sliders["B"].value()
        self._led_value_labels["R"].setText(str(r))
        self._led_value_labels["G"].setText(str(g))
        self._led_value_labels["B"].setText(str(b))
        self._led_swatch.setStyleSheet(
            f"background-color: rgb({r},{g},{b}); border: 1px solid #888;")

    def _led_apply(self, channel: str, idx: int) -> None:
        v = self._led_sliders[channel].value()
        self._cmd_write((idx, 0), v, f"led_color_{channel.lower()}")

    def _led_save(self) -> None:
        # 0x3050 cmc_save_config = MC_IF_SAVE_MAGIC (0x7376). Same trigger
        # as the existing Motor Config save; covers LED colour too now.
        self._cmd_write((0x3050, 0), 0x7376, "cmc_save_config")

    def _led_read_all(self) -> None:
        # Issue three OD reads; on each completion _on_read_done routes the
        # value back into the slider via _led_on_read (see _on_read_done
        # dispatcher — the (0x3060/61/62) keys are recognised there).
        for idx in (0x3060, 0x3061, 0x3062):
            entry = self.od.by_key.get((idx, 0))
            if entry is not None and self.client.connected:
                self.client.read_async(entry)

    def _led_on_read(self, channel: str, raw: int) -> None:
        # Called from _on_read_done when one of the LED-colour entries
        # comes back. Block signals so setValue doesn't fire the apply path.
        s = self._led_sliders.get(channel)
        if s is None:
            return
        s.blockSignals(True)
        s.setValue(raw & 0xFF)
        s.blockSignals(False)
        self._led_on_slider_drag(raw & 0xFF)

    def _build_motorcfg_actions(self) -> QWidget:
        box = QGroupBox("Calibration")
        lay = QVBoxLayout(box)
        note = QLabel(
            "Calibration runs on the motor controller. Disable the drive (not actively "
            "moving) before running electrical alignment or current-offset calibration — "
            "both need the power stage off and are rejected while the drive is enabled."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #555;")
        lay.addWidget(note)

        actions = QHBoxLayout()
        b_align = QPushButton("Run electrical alignment")
        b_align.setToolTip(
            "Write 1 (ALIGN_CAPTURE) to 0x2700:1. Forces open-loop d-axis current "
            "at cal_align_current_a for cal_align_hold_ms, captures the electrical "
            "offset, then safe-offs and auto-saves.")
        b_align.clicked.connect(
            lambda: self._mcfg_fire_cal(MC_CAL_ALIGN_CAPTURE, "electrical alignment"))
        actions.addWidget(b_align)
        b_mech = QPushButton("Set mechanical zero")
        b_mech.setToolTip(
            "Write 3 (SET_MECH_ZERO) to 0x2700:1. Captures the current position as "
            "mechanical home; position commands become relative to it. Auto-saved.")
        b_mech.clicked.connect(
            lambda: self._mcfg_fire_cal(MC_CAL_SET_MECH_ZERO, "set mechanical zero"))
        actions.addWidget(b_mech)
        b_coff = QPushButton("Current offset")
        b_coff.setToolTip(
            "Write 2 (CURRENT_OFFSET) to 0x2700:1. Measures the phase-current ADC "
            "zero offsets; needs the power stage off (rejected otherwise).")
        b_coff.clicked.connect(
            lambda: self._mcfg_fire_cal(MC_CAL_CURRENT_OFFSET, "current-offset calibration"))
        actions.addWidget(b_coff)
        actions.addStretch(1)
        lay.addLayout(actions)

        # Outstanding-calibrations checklist (0x2700:5 cal_done_flags) — the "what still
        # needs calibrating" view: each item shows done (✓) or outstanding (✗, emphasised),
        # and a summary names what's left.
        done = QHBoxLayout()
        done.addWidget(QLabel("Completeness:"))
        self.lbl_cal_done = QLabel("—")
        self.lbl_cal_done.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_cal_done.setToolTip("From cal_done_flags (0x2700:5). ✗ = outstanding.")
        done.addWidget(self.lbl_cal_done)
        b_refresh = QPushButton("Refresh")
        b_refresh.setToolTip("Read cal_done_flags + cal_status + store_status now.")
        b_refresh.clicked.connect(self._mcfg_read_status)
        done.addWidget(b_refresh)
        done.addStretch(1)
        lay.addLayout(done)

        # Transient last-command feedback (cal_status, 0x2700:2).
        status = QHBoxLayout()
        status.addWidget(QLabel("Last action:"))
        self.lbl_cal_status = QLabel("-")
        self.lbl_cal_status.setStyleSheet("font-family: monospace;")
        status.addWidget(self.lbl_cal_status)
        status.addStretch(1)
        lay.addLayout(status)

        # Destructive action, visually separated.
        fr = QHBoxLayout()
        b_fr = QPushButton("Factory reset…")
        b_fr.setToolTip(
            "Write FACTORY_RESET_MAGIC (0x7274) to 0x2800:3 — erases saved config; "
            "built-in defaults are restored on the next reboot.")
        b_fr.setStyleSheet("color: #a00;")
        b_fr.clicked.connect(self._mcfg_factory_reset)
        fr.addWidget(b_fr)
        fr.addStretch(1)
        lay.addLayout(fr)
        return box

    def _mcfg_set_backend(self) -> None:
        """Write motor_backend_sel (0x2000:6). Applied at boot, so prompt to save + power-cycle."""
        if not self.client.connected:
            QMessageBox.warning(self, "Not connected", "Connect to the CMC first.")
            return
        sel = int(self.mcfg_backend.currentData())
        self._cmd_write((0x2000, 6), sel, "motor_backend_sel")
        QTimer.singleShot(200, self._mcfg_read_backend)   # refresh the "Configured:" readout
        name = "Brushed DC (H-bridge)" if sel else "BLDC / PMSM (FOC)"
        QMessageBox.information(
            self, "Backend selected",
            f"Wrote motor_backend_sel = {sel} ({name}).\n\n"
            "Now click 'Save to flash', then power-cycle the controller — the backend and the "
            "current-sense ADC channel are applied at boot.")

    def _mcfg_read_all(self) -> None:
        if not self.client.connected:
            self._log("Motor Config: not connected", "WARN")
            return
        for key in self._mcfg_keys:
            entry = self.od.by_key.get(key)
            if entry and entry.readable:
                self.client.read_async(entry)
        self._mcfg_read_status()

    def _mcfg_read_backend(self) -> None:
        """Read the configured drive backend (0x2000:6) -> the 'Configured:' readout."""
        be = self.od.get(0x2000, 6)
        if self.client.connected and be is not None and be.readable:
            self.client.read_async(be)

    def _mcfg_read_gains(self) -> None:
        """Read the motor-derived brushed gains (0x2400:6/7) once. They only change when R/L/bandwidth
        are edited, so this is called on connect + ~100 ms after such an edit -- never polled."""
        if not self.client.connected:
            return
        for key in ((0x2400, 6), (0x2400, 7)):
            entry = self.od.by_key.get(key)
            if entry and entry.readable:
                self.client.read_async(entry)

    def _mcfg_read_status(self) -> None:
        if not self.client.connected:
            return
        for key in ((0x2700, 2), (0x2700, 5), (0x2800, 2)):
            entry = self.od.by_key.get(key)
            if entry and entry.readable:
                self.client.read_async(entry)

    def _mcfg_poll_status(self) -> None:
        """Read status now, then a few more times to catch the running->done
        transition after a calibration/save (alignment holds for cal_align_hold_ms)."""
        self._mcfg_read_status()
        for delay in (300, 800, 1500):
            QTimer.singleShot(delay, self._mcfg_read_status)

    def _mcfg_fire_cal(self, code: int, name: str) -> None:
        if not self.client.connected:
            QMessageBox.warning(self, "Not connected", "Connect to the CMC first.")
            return
        entry = self.od.by_key.get((0x2700, 1))  # cal_command
        if entry is None:
            QMessageBox.warning(self, "Missing OD entry", "cal_command (0x2700:1) not in OD.")
            return
        self._log(f"CAL: {name}  (0x2700:1 = {code})", "TX")
        self.lbl_cal_status.setText("requested…")
        self.client.write_async(entry, code)
        self._mcfg_poll_status()

    def _mcfg_save(self) -> None:
        if not self.client.connected:
            QMessageBox.warning(self, "Not connected", "Connect to the CMC first.")
            return
        entry = self.od.by_key.get((0x2800, 1))  # store_save_command
        if entry is None:
            QMessageBox.warning(self, "Missing OD entry", "store_save_command (0x2800:1) not in OD.")
            return
        self._log(f"SAVE: SAVE_MAGIC (0x{MC_SAVE_MAGIC:04X}) -> 0x2800:1", "TX")
        self.client.write_async(entry, MC_SAVE_MAGIC)
        # The motor only commits to flash with the power stage OFF (ADR-010). Watch store_status
        # until the pending bit clears, so the user gets a clear "disable to commit" / "Saved" prompt.
        self._save_watching = True
        self._save_ticks = 0
        self.lbl_store_status.setText("saving… (commit needs the drive disabled)")
        self.lbl_store_status.setStyleSheet("color:#b06000; font-weight:bold;")
        if not hasattr(self, "_save_timer"):
            self._save_timer = QTimer(self)
            self._save_timer.timeout.connect(self._mcfg_save_tick)
        self._save_timer.start(1000)
        self._mcfg_read_status()

    def _mcfg_save_tick(self) -> None:
        """While a save is pending, re-read store_status until it commits (or give up)."""
        self._save_ticks += 1
        if self._save_ticks > 150:          # ~2.5 min: stop waiting
            self._save_watching = False
            self._save_timer.stop()
            return
        self._mcfg_read_status()

    def _mcfg_factory_reset(self) -> None:
        if not self.client.connected:
            QMessageBox.warning(self, "Not connected", "Connect to the CMC first.")
            return
        if QMessageBox.question(
                self, "Factory reset",
                "Erase the motor controller's saved configuration?\n\n"
                "Saved values are wiped and built-in defaults are restored on the "
                "next reboot. This cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        entry = self.od.by_key.get((0x2800, 3))  # store_factory_reset
        if entry is None:
            QMessageBox.warning(self, "Missing OD entry", "store_factory_reset (0x2800:3) not in OD.")
            return
        self._log(f"FACTORY RESET: magic (0x{MC_FACTORY_RESET_MAGIC:04X}) -> 0x2800:3", "TX")
        self.client.write_async(entry, MC_FACTORY_RESET_MAGIC)
        self._mcfg_poll_status()

    def _mcfg_update_status(self, entry: OdEntry, raw: int) -> None:
        label = self._mcfg_status.get(entry.key)
        if label is None:
            return
        if entry.key == (0x2700, 2):  # cal_status (transient last-command feedback)
            text, color = _CAL_STATUS_TEXT.get(raw, (f"0x{raw:04X}", "#444"))
            label.setText(text)
            label.setStyleSheet(f"font-family: monospace; color: {color};")
        elif entry.key == (0x2700, 5):  # cal_done_flags -> outstanding-calibrations checklist
            parts, outstanding = [], []
            for mask, name in _CAL_DONE_ITEMS:
                if raw & mask:
                    parts.append(f'<span style="color:#060;">&#10003; {name}</span>')
                else:
                    parts.append(f'<span style="color:#a00;font-weight:bold;">&#10007; {name}</span>')
                    outstanding.append(name)
            summary = ("<b>all complete</b>" if not outstanding
                       else "<b>outstanding:</b> " + ", ".join(outstanding))
            label.setText(" &nbsp; ".join(parts) + " &nbsp;—&nbsp; " + summary)
        elif entry.key == (0x2800, 2):  # store_status bitfield (MC_IF_STORE_VALID | _PENDING)
            valid   = bool(raw & 0x0001)
            pending = bool(raw & 0x0002)
            if pending:
                label.setText("SAVE PENDING - disable the drive to commit to flash")
                label.setStyleSheet("color:#b06000; font-weight:bold;")
            elif getattr(self, "_save_watching", False):
                # we were watching a requested save and the pending bit just cleared -> committed
                self._save_watching = False
                if hasattr(self, "_save_timer"):
                    self._save_timer.stop()
                label.setText("Saved to flash" if valid else "store: empty (save did not take)")
                label.setStyleSheet(f"color:{'#060' if valid else '#a00'}; font-weight:bold;")
            else:
                label.setText("store: " + ("saved" if valid else "empty"))
                label.setStyleSheet("font-family: monospace; color:#444;")

    def _build_map_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Configure which OD entries stream in the cyclic telemetry frame (0x2A00).\n"
            "Only PDO-mappable entries are listed. Budget: 40 bytes."
        ))

        self.map_combos: list[QComboBox] = []
        self.pdo_entries = self.od.pdo_entries
        grid = QFormLayout()
        for i in range(TLM_MAX_ENTRIES):
            combo = QComboBox()
            combo.addItem("— (empty)", None)
            for e in self.pdo_entries:
                combo.addItem(f"{e.label}  [{e.type_name}]", e.key)
            combo.currentIndexChanged.connect(self._update_budget)
            self.map_combos.append(combo)
            grid.addRow(f"Slot {i + 1}:", combo)
        lay.addLayout(grid)

        self.budget_lbl = QLabel("")
        lay.addWidget(self.budget_lbl)

        mbtns = QHBoxLayout()
        b_default = QPushButton("Load default")
        b_default.clicked.connect(self._load_default_map)
        mbtns.addWidget(b_default)
        b_clear = QPushButton("Clear")
        b_clear.clicked.connect(self._clear_map)
        mbtns.addWidget(b_clear)
        b_apply = QPushButton("Apply map")
        b_apply.clicked.connect(self._apply_map)
        mbtns.addWidget(b_apply)
        lay.addLayout(mbtns)

        sub = QGroupBox("Telemetry stream")
        sform = QFormLayout(sub)
        self.rate_divider = QSpinBox()
        self.rate_divider.setRange(1, 1000)
        self.rate_divider.setValue(1)
        self.rate_divider.setToolTip("Decimate from the cyclic rate (1 = full rate)")
        sform.addRow("Rate divider:", self.rate_divider)
        self.batch = QSpinBox()
        self.batch.setRange(1, 64)
        self.batch.setValue(10)
        self.batch.setToolTip("Samples per telemetry datagram")
        sform.addRow("Batch:", self.batch)
        sbtns = QHBoxLayout()
        b_sub = QPushButton("Subscribe")
        b_sub.clicked.connect(self._subscribe)
        sbtns.addWidget(b_sub)
        b_unsub = QPushButton("Unsubscribe")
        b_unsub.clicked.connect(lambda: self.client.unsubscribe_async())
        sbtns.addWidget(b_unsub)
        sform.addRow(sbtns)
        lay.addWidget(sub)

        b_graph = QPushButton("New graph window")
        b_graph.clicked.connect(lambda: self._new_graph())
        lay.addWidget(b_graph)
        lay.addStretch(1)
        self._update_budget()
        return w

    # === client wiring ========================================================
    def _wire_client(self) -> None:
        self.client.connected_changed.connect(self._on_connected)
        self.client.log_message.connect(self._log)
        self.client.od_read_done.connect(self._on_read_done)
        self.client.od_write_done.connect(self._on_write_done)
        self.client.error_received.connect(lambda e: self._log(e.describe(), "ERROR"))
        self.client.map_applied.connect(self._on_map_applied)
        self.client.telemetry_samples.connect(self._on_samples)
        self.client.telemetry_stats.connect(self._on_stats)

    def _log(self, msg: str, level: str = "INFO") -> None:
        self.log_console.log(msg, level)

    def _install_excepthook(self) -> None:
        """Route uncaught exceptions into the debug console (and the default handler)."""
        self._prev_excepthook = sys.excepthook

        def hook(exc_type, exc, tb):
            text = "".join(traceback.format_exception(exc_type, exc, tb)).rstrip()
            self._log("uncaught exception:\n" + text, "ERROR")
            self._prev_excepthook(exc_type, exc, tb)

        sys.excepthook = hook

    # === connection ===========================================================
    def _toggle_connect(self) -> None:
        if self.client.connected:
            self._log("disconnecting...", "INFO")
            self.client.disconnect()
        else:
            ip = self.ip_edit.text().strip()
            self._log(f"connecting -> OD {ip}:{self.od_port.value()}, "
                      f"telemetry rx :{self.tlm_port.value()} ...", "INFO")
            self.buffer.set_rate(self.cyclic_rate.value())
            self.buffer.set_origin()
            self.client.connect(ip, self.od_port.value(), self.tlm_port.value())

    def _set_status(self, text: str, color: str) -> None:
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet(f"font-weight: bold; color: {color};")

    def _on_connected(self, connected: bool) -> None:
        self.btn_connect.setText("Disconnect" if connected else "Connect")
        # UDP socket-open is NOT proof the CMC is there; stay "unverified" until it replies.
        self._set_status("Socket open - awaiting reply..." if connected else "Disconnected",
                         "darkorange" if connected else "black")
        for w in (self.ip_edit, self.od_port, self.tlm_port):
            w.setEnabled(not connected)
        self._log("socket open" if connected else "link down", "INFO")
        self._last_sample_time = 0.0
        self.latest_sample = None
        self.mcfg_backend_lbl.setText("reading…" if connected else "— (disconnected)")
        if connected:
            # auto-probe so you immediately know whether the CMC is actually answering
            probe = self.od.get(0x1000, 0) or self.od.get(0x6041, 0)
            if probe is not None:
                self._log(f"probing link: read {probe.label} (expect a reply within ~150 ms)",
                          "TX")
                self._probe_key = probe.key
                self.client.read_async(probe)
            # read the configured backend (0x2000:6) + derived gains (0x2400:6/7) once, on connect
            self._mcfg_read_backend()
            self._mcfg_read_gains()
        else:
            self._probe_key = None

    # === OD selection / read / write =========================================
    def _entry_of_item(self, item: QTreeWidgetItem | None) -> OdEntry | None:
        if item is None:
            return None
        key = item.data(0, Qt.ItemDataRole.UserRole)
        return self.od.by_key.get(key) if key else None

    def _on_select(self, current, _previous) -> None:
        entry = self._entry_of_item(current)
        self._current_entry = entry
        if entry is None:
            self.sel_label.setText("Select an OD entry")
            return
        self.sel_label.setText(
            f"{entry.label}\n{entry.type_name}  {entry.access_name}  "
            f"owner:{entry.owner_name}  {self._flags_str(entry) or 'no flags'}"
            + (f"  scale={entry.scale:g}" if entry.scaled else "")
            + (f"  [{entry.unit}]" if entry.unit else "")
        )
        self.sel_value.setText(self.item_by_key[entry.key].text(_VALUE_COL) or "-")
        self.btn_read.setEnabled(entry.readable)
        self.btn_write.setEnabled(entry.writable)
        self.write_edit.setEnabled(entry.writable)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != _WATCH_COL:
            return
        entry = self._entry_of_item(item)
        if entry is None:
            return
        watched = item.checkState(_WATCH_COL) == Qt.CheckState.Checked
        # Watch drives graphing: ticking adds the channel to every open graph,
        # unticking removes it. (Watched, non-streamed entries get data from polling.)
        for gw in self.graph_windows:
            if watched:
                gw.set_available_channels(self._available_channels())
            gw.show_channel(entry.name, watched)
        if watched and self.client.connected and entry.readable:
            self.client.read_async(entry)

    def _watched_entries(self) -> list[OdEntry]:
        out = []
        for key, item in self.item_by_key.items():
            if item.checkState(_WATCH_COL) == Qt.CheckState.Checked:
                e = self.od.by_key.get(key)
                if e:
                    out.append(e)
        return out

    def _watched_names(self) -> list[str]:
        return [e.name for e in self._watched_entries()]

    def _active_map_names(self) -> set[str]:
        return {e.name for e in self.client.active_map}

    def _do_read(self) -> None:
        if self._current_entry and self.client.connected:
            self._log(f"READ  {self._current_entry.label}", "TX")
            self.client.read_async(self._current_entry)
        elif not self.client.connected:
            self.rw_result.setText("Not connected.")

    def _do_write(self) -> None:
        entry = self._current_entry
        if entry is None:
            return
        if not self.client.connected:
            self.rw_result.setText("Not connected.")
            return
        text = self.write_edit.text().strip()
        try:
            if entry.is_float or entry.scaled:
                raw = entry.si_to_raw(float(text))
            else:
                raw = int(text, 0)  # accepts 0x.., 0b.., decimal
        except ValueError:
            self.rw_result.setText(f"Cannot parse '{text}' for {entry.type_name}.")
            return
        self._log(f"WRITE {entry.label} = {text} (raw {raw})", "TX")
        self.client.write_async(entry, raw)
        self.rw_result.setText(f"Writing {text} to {entry.label} ...")

    def _on_read_done(self, res: dict) -> None:
        entry: OdEntry = res["entry"]
        item = self.item_by_key.get(entry.key)
        is_probe = getattr(self, "_probe_key", None) == entry.key
        # Forward to a config tab (CMC Setup or Motor Config) if either owns this
        # row — done before the main handler so the row update isn't dependent on
        # the rest of the logic below.
        if self._cfg_row(entry.key) is not None:
            if res.get("ok"):
                self._setup_on_read_done(entry, True, entry.format_value(res["raw"]), "")
            else:
                self._setup_on_read_done(entry, False, "", res.get("error", "error"))
        # Motor Config tab: route cal_status / store_status read-backs to labels.
        if entry.key in getattr(self, "_mcfg_status", {}):
            if res.get("ok"):
                self._mcfg_update_status(entry, int(res["raw"]))
            else:
                self._mcfg_status[entry.key].setText(f"<{res.get('error', 'error')}>")
        # Motor Command tab: route axis/motor state read-backs to the command labels.
        if (entry.key in _CMD_STATE_KEYS or entry.key in _MCFG_READOUT_KEYS) and res.get("ok"):
            self._cmd_on_state_read(res)
        # Motor Config tab: route LED-colour read-backs (0x3060/61/62) into the
        # sliders so "Read LED" pulls the live values from the CMC.
        if res.get("ok") and entry.key in {(0x3060, 0), (0x3061, 0), (0x3062, 0)}:
            ch = {0x3060: "R", 0x3061: "G", 0x3062: "B"}[entry.key[0]]
            try:
                self._led_on_read(ch, int(res["raw"]))
            except (KeyError, TypeError, ValueError):
                pass
        if res.get("ok"):
            text = entry.format_value(res["raw"])
            if item:
                self._set_value_cell(item, text)
            if entry is self._current_entry:
                self.sel_value.setText(text)
                self.rw_result.setText(f"Read OK: {text}")
            # feed watched-but-not-streamed entries into the graph buffer (poll rate)
            if (item is not None
                    and item.checkState(_WATCH_COL) == Qt.CheckState.Checked
                    and entry.name not in self._active_map_names()):
                self.buffer.add_poll(entry.name, res["si"])
            self._set_status("Connected (CMC replying)", "green")
            if is_probe:
                self._log(f"link OK - CMC replied: {entry.name} = {text}", "RX")
                self._probe_key = None
            else:
                self._log(f"READ  {entry.label} -> {text}", "DEBUG")
        else:
            err = res.get("error", "error")
            if entry is self._current_entry:
                self.rw_result.setText(f"Read failed: {err}")
            if item:
                self._set_value_cell(item, f"<{err}>", error=True)
            if is_probe:
                # Distinguish "no datagram came back at all" (real comms problem)
                # from "the CMC replied but the OD result was non-OK" (the link
                # is fine; the underlying entry is just not readable right now).
                # The client emits the literal string "timeout" only in the
                # former case.
                if err == "timeout":
                    self._set_status("No reply from CMC", "red")
                    self._log(f"NO REPLY from CMC. Check IP/port, that the CMC is "
                              f"running, and the firewall.", "ERROR")
                else:
                    self._set_status(f"Link up; probe got {err}", "darkorange")
                    self._log(f"link OK - CMC replied to probe, but the entry returned "
                              f"'{err}'. The link is up; this entry is just unavailable "
                              f"right now (e.g. motor MCU not ready).", "WARN")
                self._probe_key = None
            else:
                self._log(f"READ  {entry.label} FAILED: {err}", "WARN")

    def _on_write_done(self, res: dict) -> None:
        entry: OdEntry = res["entry"]
        if self._cfg_row(entry.key) is not None:
            self._setup_on_write_done(entry, bool(res.get("ok")),
                                      res.get("error", "error"))
        if res.get("ok"):
            self.rw_result.setText(f"Write OK: {entry.label}")
            self._log(f"WRITE {entry.label} OK", "RX")
            if self.client.connected and entry.readable:
                self.client.read_async(entry)  # read back to confirm
        else:
            err = res.get("error", "error")
            self.rw_result.setText(f"Write failed: {err}")
            self._log(f"WRITE {entry.label} FAILED: {err}", "WARN")

    def _set_value_cell(self, item: QTreeWidgetItem, text: str, error: bool = False) -> None:
        self.tree.blockSignals(True)
        item.setText(_VALUE_COL, text)
        item.setForeground(_VALUE_COL, QColor("red") if error else QColor("black"))
        self.tree.blockSignals(False)

    def _poll_watched(self) -> None:
        if not self.client.connected:
            return
        for key, item in self.item_by_key.items():
            if item.checkState(_WATCH_COL) == Qt.CheckState.Checked:
                entry = self.od.by_key.get(key)
                if entry and entry.readable:
                    self.client.read_async(entry)
        self._cmd_poll_state()   # refresh the Motor Command tab's live state

    # === telemetry map ========================================================
    def _selected_map_entries(self) -> list[OdEntry]:
        out: list[OdEntry] = []
        for combo in self.map_combos:
            key = combo.currentData()
            if key is not None:
                e = self.od.by_key.get(key)
                if e:
                    out.append(e)
        return out

    def _update_budget(self) -> None:
        entries = self._selected_map_entries()
        total = sum(e.size for e in entries)
        ok = total <= TLM_MAX_BYTES and len(entries) <= TLM_MAX_ENTRIES
        self.budget_lbl.setText(
            f"{len(entries)} channels, {total} / {TLM_MAX_BYTES} bytes"
            + ("" if ok else "  -- OVER BUDGET"))
        self.budget_lbl.setStyleSheet("" if ok else "color: red; font-weight: bold;")

    @staticmethod
    def _combo_index_for_key(combo: QComboBox, key) -> int:
        # QComboBox.findData does not reliably match Python tuple userData, so search.
        for i in range(combo.count()):
            if combo.itemData(i) == key:
                return i
        return -1

    def _clear_map(self) -> None:
        for combo in self.map_combos:
            combo.setCurrentIndex(0)

    def _load_default_map(self) -> None:
        preferred = ["position_actual", "velocity_actual", "torque_actual",
                     "tlm_vel_demand_rad_s", "tlm_vel_actual_rad_s", "tlm_iq_meas_a",
                     "tlm_i_arm_a"]   # brushed measured current -> Motor Config live readout
        chosen = [self.od.by_name[n] for n in preferred
                  if n in self.od.by_name and self.od.by_name[n].is_pdo]
        self._clear_map()
        for slot, entry in enumerate(chosen):
            idx = self._combo_index_for_key(self.map_combos[slot], entry.key)
            if idx >= 0:
                self.map_combos[slot].setCurrentIndex(idx)

    def _apply_map(self) -> None:
        if not self.client.connected:
            QMessageBox.warning(self, "Not connected", "Connect to the CMC first.")
            return
        entries = self._selected_map_entries()
        if not entries:
            QMessageBox.warning(self, "Empty map", "Select at least one channel.")
            return
        self._log("applying telemetry map (0x2A00): "
                  + ", ".join(e.name for e in entries), "TX")
        self.client.apply_map_async(entries)

    def _on_map_applied(self, res: dict) -> None:
        ok = res.get("ok")
        self._log(("map applied - " if ok else "map FAILED - ") + res.get("message", ""),
                  "RX" if ok else "ERROR")
        if ok:
            self.buffer.clear()
            for gw in self.graph_windows:
                gw.set_available_channels(self._available_channels())

    def _available_channels(self) -> list[str]:
        names = (set(STATUS_CHANNELS)
                 | {e.name for e in self.client.active_map}
                 | set(self._watched_names()))
        return sorted(names)

    def _subscribe(self) -> None:
        if not self.client.connected:
            QMessageBox.warning(self, "Not connected", "Connect to the CMC first.")
            return
        self.buffer.set_rate(self.cyclic_rate.value())
        self.client.subscribe_async(self.rate_divider.value(), self.batch.value())

    # === telemetry data =======================================================
    def _on_samples(self, samples: list[TelemetrySample]) -> None:
        self.buffer.add_samples(samples)
        if samples:
            self.latest_sample = samples[-1]
            self._last_sample_time = time.monotonic()
            self._set_status("Connected (streaming)", "green")

    def _on_stats(self, stats: dict) -> None:
        self.lbl_rate.setText(
            f"{stats['rate_hz']:.0f} Hz  drops:{stats['dropped']}  "
            f"map v{stats['frame_map_version']}")
        self._log(f"telemetry {stats['rate_hz']:.0f} Hz, drops={stats['dropped']}, "
                  f"frame map v{stats['frame_map_version']}", "DEBUG")

    def _refresh_live(self) -> None:
        # if a stream was running but went silent, stop claiming it's live
        if (self.client.connected and self._last_sample_time
                and time.monotonic() - self._last_sample_time > 2.0):
            self._set_status("Socket open - stream stalled / no telemetry", "darkorange")
            self._last_sample_time = 0.0
        s = self.latest_sample
        if s is None:
            return
        node = proto.NODE_STATE_NAME.get(s.node_state, s.node_state)
        self.lbl_node.setText(
            f"node:{node}  SW:0x{s.statusword:04X}  err:0x{s.error_code:04X}  "
            f"mode:{s.mode_display}")
        # reflect streamed values into the OD tree
        for name, value in s.values.items():
            entry = self.od.by_name.get(name)
            if entry is not None:
                item = self.item_by_key.get(entry.key)
                if item is not None:
                    self._set_value_cell(item, entry.format_value(entry.si_to_raw(value)))
        # Motor Config measured-current readout: from the telemetry stream (not polled).
        i_arm = s.values.get("tlm_i_arm_a")
        if i_arm is not None:
            self.mcfg_cur_lbl.setText(f"{i_arm:.3f} A")

    # === graphing =============================================================
    def _new_graph(self, initial: list[str] | None = None) -> GraphWindow:
        if initial is None:
            initial = self._watched_names()  # default: graph what you've ticked as Watch
        # parent=None makes the graph a TRULY independent top-level window —
        # minimizing it doesn't minimize MainWindow, and minimizing MainWindow
        # doesn't minimize the graph. With parent=self Qt treats it as a
        # "secondary window" of the parent and ties their window-states.
        # We still keep a reference in self.graph_windows to (a) keep it alive
        # against garbage collection and (b) close it explicitly when MainWindow
        # closes — otherwise the Python process keeps running with orphan
        # graph windows after the main UI is gone.
        gw = GraphWindow(self.buffer, self._available_channels(), initial=initial, parent=None)
        gw.show()
        self.graph_windows.append(gw)
        return gw

    def closeEvent(self, event) -> None:
        # Close all detached graph windows so the app actually exits when
        # MainWindow closes (otherwise their event loops keep Python alive).
        for gw in list(self.graph_windows):
            try:
                gw.close()
            except RuntimeError:
                pass  # already deleted
        self.graph_windows.clear()
        super().closeEvent(event)

    # === context menu / filter ===============================================
    def _tree_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        entry = self._entry_of_item(item)
        if entry is None:
            return
        menu = QMenu(self)
        act_read = QAction("Read now", self)
        act_read.triggered.connect(lambda: self.client.read_async(entry) if self.client.connected else None)
        menu.addAction(act_read)

        if entry.is_pdo:
            act_map = QAction("Add to telemetry map", self)
            act_map.triggered.connect(lambda: self._add_to_map(entry))
            menu.addAction(act_map)
            act_graph = QAction("Graph (must be in active map)", self)
            act_graph.triggered.connect(lambda: self._graph_entry(entry))
            menu.addAction(act_graph)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _add_to_map(self, entry: OdEntry) -> None:
        for combo in self.map_combos:
            if combo.currentData() == entry.key:
                return  # already present
        for combo in self.map_combos:
            if combo.currentData() is None:
                idx = self._combo_index_for_key(combo, entry.key)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                return
        QMessageBox.warning(self, "Map full", "All 16 map slots are in use.")

    def _graph_entry(self, entry: OdEntry) -> None:
        if entry.name not in [e.name for e in self.client.active_map]:
            QMessageBox.information(
                self, "Not streamed",
                f"{entry.label} is not in the active telemetry map.\n"
                "Add it to the map and Apply, then graph it.")
            return
        self._new_graph(initial=[entry.name])

    def _apply_filter(self, text: str) -> None:
        text = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            hay = f"{item.text(0)} {item.text(1)} {item.text(4)}".lower()  # name, id, owner
            item.setHidden(bool(text) and text not in hay)

    # === shutdown =============================================================
    def closeEvent(self, event) -> None:
        if getattr(self, "_prev_excepthook", None) is not None:
            sys.excepthook = self._prev_excepthook
        for gw in list(self.graph_windows):
            gw.close()
        self.client.disconnect()
        super().closeEvent(event)
