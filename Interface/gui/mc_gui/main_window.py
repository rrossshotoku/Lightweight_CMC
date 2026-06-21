"""Main application window: OD browser, acyclic read/write, telemetry-map editor."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import protocol as proto
from .buffer import TelemetryBuffer
from .client import NetworkClient, TelemetrySample
from .graph_window import GraphWindow
from .od import OdEntry, OdModel, TLM_MAP_INDEX, TLM_MAX_BYTES, TLM_MAX_ENTRIES

# always-present telemetry channels coming from the cyclic status header
STATUS_CHANNELS = ["statusword", "mode_display", "node_state", "error_code", "status_counter"]

_WATCH_COL = 7


class MainWindow(QWidget):
    def __init__(self, od: OdModel):
        super().__init__()
        self.od = od
        self.client = NetworkClient(od)
        self.buffer = TelemetryBuffer()
        self.graph_windows: list[GraphWindow] = []
        self.item_by_key: dict[tuple[int, int], QTreeWidgetItem] = {}
        self.latest_sample: TelemetrySample | None = None

        self.setWindowTitle("CMC Object Dictionary Tool")
        self.resize(1280, 820)
        self._build_ui()
        self._wire_client()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_watched)
        self.poll_timer.start(200)  # 5 Hz acyclic polling of watched entries

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._refresh_live)
        self.ui_timer.start(100)  # 10 Hz UI refresh from telemetry

    # === UI ===================================================================
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.addWidget(self._build_connection_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_od_tree())
        splitter.addWidget(self._build_side_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

    def _build_connection_bar(self) -> QWidget:
        box = QGroupBox("Connection")
        lay = QHBoxLayout(box)
        lay.addWidget(QLabel("CMC IP:"))
        self.ip_edit = QLineEdit("192.168.1.10")
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
        self.filter_edit.setPlaceholderText("name or index, e.g. 'vel' or '0x2300'")
        self.filter_edit.textChanged.connect(self._apply_filter)
        filt.addWidget(self.filter_edit)
        lay.addLayout(filt)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(8)
        self.tree.setHeaderLabels(
            ["Name", "Index:Sub", "Type", "Acc", "Flags", "Value", "Unit", "Watch"]
        )
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
                self._flags_str(e), "", e.unit, "",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, e.key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(_WATCH_COL, Qt.CheckState.Unchecked)
            self.tree.addTopLevelItem(item)
            self.item_by_key[e.key] = item

        for c in range(1, 8):
            self.tree.resizeColumnToContents(c)
        lay.addWidget(self.tree)
        return box

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
        tabs.addTab(self._build_rw_panel(), "Read / Write")
        tabs.addTab(self._build_map_panel(), "Telemetry / Graphing")
        tabs.addTab(self._build_log_panel(), "Log")
        return tabs

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

    def _build_log_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        lay.addWidget(self.log)
        return w

    # === client wiring ========================================================
    def _wire_client(self) -> None:
        self.client.connected_changed.connect(self._on_connected)
        self.client.log_message.connect(self._log)
        self.client.od_read_done.connect(self._on_read_done)
        self.client.od_write_done.connect(self._on_write_done)
        self.client.error_received.connect(lambda e: self._log(f"ERROR: {e.describe()}"))
        self.client.map_applied.connect(self._on_map_applied)
        self.client.telemetry_samples.connect(self._on_samples)
        self.client.telemetry_stats.connect(self._on_stats)

    def _log(self, msg: str) -> None:
        self.log.appendPlainText(msg)

    # === connection ===========================================================
    def _toggle_connect(self) -> None:
        if self.client.connected:
            self.client.disconnect()
        else:
            self.buffer.set_rate(self.cyclic_rate.value())
            self.client.connect(self.ip_edit.text().strip(),
                                self.od_port.value(), self.tlm_port.value())

    def _on_connected(self, connected: bool) -> None:
        self.btn_connect.setText("Disconnect" if connected else "Connect")
        self.lbl_status.setText("Connected" if connected else "Disconnected")
        self.lbl_status.setStyleSheet(
            f"font-weight: bold; color: {'green' if connected else 'black'};")
        for w in (self.ip_edit, self.od_port, self.tlm_port):
            w.setEnabled(not connected)

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
            f"{self._flags_str(entry) or 'no flags'}"
            + (f"  scale={entry.scale:g}" if entry.scaled else "")
            + (f"  [{entry.unit}]" if entry.unit else "")
        )
        self.sel_value.setText(self.item_by_key[entry.key].text(5) or "-")
        self.btn_read.setEnabled(entry.readable)
        self.btn_write.setEnabled(entry.writable)
        self.write_edit.setEnabled(entry.writable)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column == _WATCH_COL:
            entry = self._entry_of_item(item)
            if entry and item.checkState(_WATCH_COL) == Qt.CheckState.Checked \
                    and self.client.connected and entry.readable:
                self.client.read_async(entry)

    def _do_read(self) -> None:
        if self._current_entry and self.client.connected:
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
        self.client.write_async(entry, raw)
        self.rw_result.setText(f"Writing {text} to {entry.label} ...")

    def _on_read_done(self, res: dict) -> None:
        entry: OdEntry = res["entry"]
        item = self.item_by_key.get(entry.key)
        if res.get("ok"):
            text = entry.format_value(res["raw"])
            if item:
                self._set_value_cell(item, text)
            if entry is self._current_entry:
                self.sel_value.setText(text)
                self.rw_result.setText(f"Read OK: {text}")
        else:
            err = res.get("error", "error")
            if entry is self._current_entry:
                self.rw_result.setText(f"Read failed: {err}")
            if item:
                self._set_value_cell(item, f"<{err}>", error=True)

    def _on_write_done(self, res: dict) -> None:
        entry: OdEntry = res["entry"]
        if res.get("ok"):
            self.rw_result.setText(f"Write OK: {entry.label}")
            if self.client.connected and entry.readable:
                self.client.read_async(entry)  # read back to confirm
        else:
            self.rw_result.setText(f"Write failed: {res.get('error', 'error')}")

    def _set_value_cell(self, item: QTreeWidgetItem, text: str, error: bool = False) -> None:
        self.tree.blockSignals(True)
        item.setText(5, text)
        item.setForeground(5, QColor("red") if error else QColor("black"))
        self.tree.blockSignals(False)

    def _poll_watched(self) -> None:
        if not self.client.connected:
            return
        for key, item in self.item_by_key.items():
            if item.checkState(_WATCH_COL) == Qt.CheckState.Checked:
                entry = self.od.by_key.get(key)
                if entry and entry.readable:
                    self.client.read_async(entry)

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
                     "tlm_vel_demand_rad_s", "tlm_vel_actual_rad_s", "tlm_iq_meas_a"]
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
        self.client.apply_map_async(entries)

    def _on_map_applied(self, res: dict) -> None:
        self._log(("Map OK: " if res.get("ok") else "Map FAILED: ") + res.get("message", ""))
        if res.get("ok"):
            self.buffer.clear()
            for gw in self.graph_windows:
                gw.set_available_channels(self._available_channels())

    def _available_channels(self) -> list[str]:
        return STATUS_CHANNELS + [e.name for e in self.client.active_map]

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

    def _on_stats(self, stats: dict) -> None:
        self.lbl_rate.setText(
            f"{stats['rate_hz']:.0f} Hz  drops:{stats['dropped']}  "
            f"map v{stats['frame_map_version']}")

    def _refresh_live(self) -> None:
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

    # === graphing =============================================================
    def _new_graph(self, initial: list[str] | None = None) -> GraphWindow:
        gw = GraphWindow(self.buffer, self._available_channels(), initial=initial, parent=self)
        gw.show()
        self.graph_windows.append(gw)
        return gw

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
            hay = f"{item.text(0)} {item.text(1)}".lower()
            item.setHidden(bool(text) and text not in hay)

    # === shutdown =============================================================
    def closeEvent(self, event) -> None:
        for gw in list(self.graph_windows):
            gw.close()
        self.client.disconnect()
        super().closeEvent(event)
