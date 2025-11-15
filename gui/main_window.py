"""
Main GUI window for the Seismic Data Downloader.

Three tabs:
- Stations: ROI map + provider/network/channel filters + search
- Events: Catalog/time/magnitude/depth/distance filters + search
- Download: Parameters + arrivals + download + save
"""

import logging
from typing import List, Dict, Optional, Tuple

from PyQt5.QtCore import Qt, QDateTime, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QLabel, QLineEdit, QComboBox, QDateTimeEdit, QDoubleSpinBox,
    QSpinBox, QCheckBox, QFileDialog, QProgressBar, QTextEdit, QDockWidget,
    QTableWidget, QTableWidgetItem, QMessageBox
)

from data.data_manager import DataManager
from services.station_service import StationService
from services.event_service import EventService, MagnitudeDepthFilter
from services.waveform_downloader import WaveformDownloader
from utils.logging_progress import ProgressManager, setup_logger
from gui.map_pane import MapPane


class WorkerThread(QThread):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            result = self.func(*self.args, **self.kwargs)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self, data_manager: DataManager, base_logger: logging.Logger):
        super().__init__()
        self.setWindowTitle("Seismic Data Downloader")
        self.resize(1280, 900)

        self.data_manager = data_manager
        self.progress_manager = ProgressManager()

        # Central attributes
        self.events: List[Dict] = []
        self.stations: List[Dict] = []
        self.theoretical_arrivals: Dict[str, Dict[str, float]] = {}
        self.center: Optional[Tuple[float, float]] = None

        # Log dock and logger
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_dock = QDockWidget("Logs", self)
        self.log_dock.setWidget(self.log_text)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)

        # File log path if project_dir known
        log_file = None
        if self.data_manager.project_dir:
            log_file = str(self.data_manager.project_dir / 'output' / 'logs' / 'session.log')

        self.logger = setup_logger('downloader_gui', log_widget=self.log_text, log_file=log_file, level=base_logger.level)

        # Services
        self.station_service = StationService(self.progress_manager, self.logger)
        self.event_service = EventService(self.progress_manager, self.logger)
        self.waveform_downloader = WaveformDownloader(self.progress_manager, self.logger)

        # Tabs
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.project_tab = self._build_project_tab()
        self.station_tab = self._build_station_tab()
        self.event_tab = self._build_event_tab()
        self.download_tab = self._build_download_tab()

        # Sync event times from station tab by default; allow user override
        self._ev_time_synced = True
        # Set event times initially without emitting change signals
        self.ev_start_dt.blockSignals(True)
        self.ev_end_dt.blockSignals(True)
        self.ev_start_dt.setDateTime(self.sta_start_dt.dateTime())
        self.ev_end_dt.setDateTime(self.sta_end_dt.dateTime())
        self.ev_start_dt.blockSignals(False)
        self.ev_end_dt.blockSignals(False)
        # Connect after initial set
        self.sta_start_dt.dateTimeChanged.connect(self._maybe_sync_event_times)
        self.sta_end_dt.dateTimeChanged.connect(self._maybe_sync_event_times)
        self.ev_start_dt.dateTimeChanged.connect(self._disable_time_sync)
        self.ev_end_dt.dateTimeChanged.connect(self._disable_time_sync)

        self.tabs.addTab(self.project_tab, "Project")
        self.tabs.addTab(self.station_tab, "Stations")
        self.tabs.addTab(self.event_tab, "Events")
        self.tabs.addTab(self.download_tab, "Download")

        # Connect progress signals
        self.progress_manager.progress_updated.connect(self._on_progress_updated)
        self.progress_manager.task_completed.connect(self._on_task_completed)
        self.progress_manager.task_failed.connect(self._on_task_failed)

        self._workers = []  # Keep references to worker threads
        self.logger.info("GUI initialized.")

    # ------------------------
    # Project Tab
    # ------------------------
    def _build_project_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        form = QFormLayout()

        # Project directory picker
        self.project_dir_input = QLineEdit("")
        btn_browse_proj = QPushButton("Browse…")
        def _browse_proj():
            path = QFileDialog.getExistingDirectory(self, "Select Project Directory")
            if path:
                self.project_dir_input.setText(path)
        btn_browse_proj.clicked.connect(_browse_proj)
        row = QHBoxLayout(); row.addWidget(self.project_dir_input); row.addWidget(btn_browse_proj)
        form.addRow(QLabel("Project directory:"), self._wrap(row))

        # Initialize/apply button
        self.btn_init_project = QPushButton("Initialize/Use Project")
        self.chk_set_waveforms_output = QCheckBox("Set Download output to <project>/data")
        self.chk_set_waveforms_output.setChecked(True)
        btns = QHBoxLayout(); btns.addWidget(self.btn_init_project); btns.addWidget(self.chk_set_waveforms_output)
        form.addRow(self._wrap(btns))

        # Derived paths preview (labels)
        self.lbl_paths_preview = QLabel("")
        form.addRow(QLabel("Paths preview:"), self.lbl_paths_preview)

        layout.addLayout(form)

        def _update_preview(path: str):
            if not path:
                self.lbl_paths_preview.setText("")
                return
            from pathlib import Path
            p = Path(path)
            preview = f"\nStations CSV: {p / 'data' / 'stations' / 'stations.csv'}\n" \
                      f"Events CSV:   {p / 'data' / 'events' / 'events.csv'}\n" \
                      f"StationXML:   {p / 'data' / 'stationxml'}\n" \
                      f"Waveforms:    {p / 'data' / 'waveforms'}"
            self.lbl_paths_preview.setText(preview)

        self.project_dir_input.textChanged.connect(lambda _: _update_preview(self.project_dir_input.text().strip()))
        _update_preview("")

        def _init_project():
            path = self.project_dir_input.text().strip()
            if not path:
                QMessageBox.warning(self, "Project", "Please select a project directory.")
                return
            ok = self.data_manager.initialize_project(path)
            if ok:
                # Set logs path for GUI logger
                try:
                    log_file = str((self.data_manager.project_dir / 'output' / 'logs' / 'session.log'))
                    # Recreate logger to include file (optional)
                    # self.logger = setup_logger('downloader_gui', log_widget=self.log_text, log_file=log_file, level=self.logger.level)
                    self.logger.info(f"Using project directory: {self.data_manager.project_dir}")
                except Exception:
                    pass
                if self.chk_set_waveforms_output.isChecked():
                    try:
                        default_wave_dir = str(self.data_manager.project_dir / 'data')
                        self.output_dir.setText(default_wave_dir)
                    except Exception:
                        pass
                QMessageBox.information(self, "Project", "Project directory set.")
                _update_preview(path)
            else:
                QMessageBox.critical(self, "Project", "Failed to initialize project directory.")
        self.btn_init_project.clicked.connect(_init_project)

        return w

    # ------------------------
    # Station Tab
    # ------------------------
    def _build_station_tab(self) -> QWidget:
        w = QWidget()
        outer = QHBoxLayout(w)

        # Left: controls
        left = QWidget(); left_layout = QVBoxLayout(left)
        form = QFormLayout()

        # Providers as checkboxes container
        providers_layout = QHBoxLayout()
        self.provider_checks = []
        for name in ["IRIS", "GEOFON", "ORFEUS", "RESIF", "INGV", "ETHZ", "NCEDC", "SCEDC", "USGS"]:
            cb = QCheckBox(name)
            if name == "IRIS":
                cb.setChecked(True)
            self.provider_checks.append(cb)
            providers_layout.addWidget(cb)
        form.addRow(QLabel("Providers:"), self._wrap(providers_layout))

        self.network_input = QLineEdit("*")
        self.station_input = QLineEdit("*")
        self.channels_input = QLineEdit("BH?")
        self.sta_start_dt = QDateTimeEdit()
        self.sta_start_dt.setCalendarPopup(True)
        self.sta_start_dt.setDateTime(QDateTime.currentDateTime().addYears(-5))
        self.sta_end_dt = QDateTimeEdit()
        self.sta_end_dt.setCalendarPopup(True)
        self.sta_end_dt.setDateTime(QDateTime.currentDateTime())

        form.addRow("Networks:", self.network_input)
        form.addRow("Stations:", self.station_input)
        form.addRow("Channels:", self.channels_input)
        form.addRow("Start Time:", self.sta_start_dt)
        form.addRow("End Time:", self.sta_end_dt)

        controls_row = QHBoxLayout()
        self.btn_search_stations = QPushButton("Search Stations")
        self.btn_save_stations = QPushButton("Save Stations")
        self.btn_save_stations.setEnabled(False)
        controls_row.addWidget(self.btn_search_stations)
        controls_row.addWidget(self.btn_save_stations)

        form.addRow(self._wrap(controls_row))
        left_layout.addLayout(form)

        # Right: map (top) + table (bottom) in a vertical splitter
        right_split = QVBoxLayout()
        self.map_pane = MapPane()
        self.map_pane.roi_changed.connect(self._on_roi_changed)
        self.map_pane.center_computed.connect(self._on_center_computed)
        # Wrap map and table in a splitter-like behavior using layouts (Qt splitter optional)
        right_widget = QWidget(); right_inner = QVBoxLayout(right_widget)
        right_inner.addWidget(self.map_pane, stretch=3)
        self.station_table = QTableWidget(0, 6)
        self.station_table.setHorizontalHeaderLabels(["Network", "Station", "Lat", "Lon", "Provider", "Channels"])
        right_inner.addWidget(self.station_table, stretch=2)

        # Assemble outer layout with stretch factors
        outer.addWidget(left, stretch=1)
        outer.addWidget(right_widget, stretch=3)

        # Connect actions
        self.btn_search_stations.clicked.connect(self._on_search_stations)
        self.btn_save_stations.clicked.connect(self._on_save_stations)

        return w

    def _wrap(self, layout: QHBoxLayout) -> QWidget:
        holder = QWidget()
        holder.setLayout(layout)
        return holder

    def _on_roi_changed(self, roi_obj):
        # Update internal state; optionally compute center
        self.logger.info("ROI updated.")

    def _on_center_computed(self, lat: float, lon: float):
        self.center = (lat, lon)
        self.logger.info(f"Center computed: {lat:.3f}, {lon:.3f}")
        # Reflect center on Events map if available
        try:
            self.events_map.clear_markers()
            self.events_map.set_center_and_rings(self.center, [])
        except Exception:
            pass

    def _on_search_stations(self):
        roi = self.map_pane.get_current_roi()
        if not roi:
            # Try to fetch ROI from JS fallback (window.lastGeoJSON)
            self.map_pane.fetch_roi_async(lambda gj: self._continue_station_search(gj))
            return
        self._continue_station_search(roi)

    def _continue_station_search(self, roi):
        if not roi:
            QMessageBox.warning(self, "ROI Required", "Please draw a rectangle or circle on the map.")
            return
        bbox = MapPane.extract_bbox_from_roi(roi)
        if not bbox:
            QMessageBox.warning(self, "Invalid ROI", "Could not extract bounding box from the ROI.")
            return
        providers = [cb.text() for cb in self.provider_checks if cb.isChecked()]
        if not providers:
            QMessageBox.warning(self, "No Providers", "Please select at least one provider.")
            return
        self.btn_search_stations.setEnabled(False)

        def on_finished(result):
            self.btn_search_stations.setEnabled(True)
            if result is None:
                QMessageBox.critical(self, "Error", "Station search failed.")
                return
            self.stations = result
            self._populate_station_table(result)
            self.map_pane.clear_markers()
            self.map_pane.add_stations(result)
            self.btn_save_stations.setEnabled(True)
            self.logger.info(f"Station search complete: {len(result)} stations.")

        def on_error(msg):
            self.btn_search_stations.setEnabled(True)
            QMessageBox.critical(self, "Error", f"Station search failed: {msg}")

        worker = WorkerThread(
            self.station_service.search_stations,
            providers=providers,
            roi_bbox=bbox,
            networks=self.network_input.text().strip() or "*",
            stations=self.station_input.text().strip() or "*",
            channels=self.channels_input.text().strip() or "BH?",
            start_time=self.sta_start_dt.dateTime().toString(Qt.ISODate),
            end_time=self.sta_end_dt.dateTime().toString(Qt.ISODate),
            include_closed=False
        )
        self._run_worker(worker, on_finished, on_error)

    def _populate_station_table(self, stations: List[Dict]):
        self.station_table.setRowCount(0)
        for s in stations:
            row = self.station_table.rowCount()
            self.station_table.insertRow(row)
            self.station_table.setItem(row, 0, QTableWidgetItem(s.get('network', '')))
            self.station_table.setItem(row, 1, QTableWidgetItem(s.get('station', '')))
            self.station_table.setItem(row, 2, QTableWidgetItem(f"{s.get('latitude', 0):.3f}"))
            self.station_table.setItem(row, 3, QTableWidgetItem(f"{s.get('longitude', 0):.3f}"))
            self.station_table.setItem(row, 4, QTableWidgetItem(s.get('provider', '')))
            # Channels column: prefer channel_types (BH,HH,EH), fallback to full channel list
            chan_types = s.get('channel_types') or []
            chan_list = s.get('channels') or []
            chan_display = ",".join(chan_types) if chan_types else ",".join(chan_list)
            self.station_table.setItem(row, 5, QTableWidgetItem(chan_display))
        self.station_table.resizeColumnsToContents()

    def _on_save_stations(self):
        self.data_manager.set_stations(self.stations)
        self.data_manager.save_checkpoint("stations")
        # Export CSV and StationXML if project_dir is set
        try:
            proj = self.data_manager.project_dir
            if proj:
                stations_csv = proj / 'data' / 'stations' / 'stations.csv'
                self.data_manager.export_stations_csv(str(stations_csv))
                # Save StationXML files
                sx_dir = proj / 'data' / 'stationxml'
                count = self.station_service.save_stationxml(self.stations, str(sx_dir))
                self.logger.info(f"Saved stations CSV and {count} StationXML files.")
        except Exception as e:
            self.logger.warning(f"Could not export stations CSV/StationXML: {e}")
        QMessageBox.information(self, "Saved", f"Saved {len(self.stations)} stations to project (CSV + StationXML).")

    # ------------------------
    # Event Tab
    # ------------------------
    def _build_event_tab(self) -> QWidget:
        w = QWidget()
        outer = QHBoxLayout(w)

        # Left: controls
        left = QWidget(); left_layout = QVBoxLayout(left)
        form = QFormLayout()

        self.catalog_combo = QComboBox()
        self.catalog_combo.addItems(["IRIS", "USGS", "ISC"])

        self.ev_start_dt = QDateTimeEdit()
        self.ev_start_dt.setCalendarPopup(True)
        self.ev_start_dt.setDateTime(QDateTime.currentDateTime().addYears(-1))
        self.ev_end_dt = QDateTimeEdit()
        self.ev_end_dt.setCalendarPopup(True)
        self.ev_end_dt.setDateTime(QDateTime.currentDateTime())

        self.min_mag = QDoubleSpinBox(); self.min_mag.setRange(0.0, 10.0); self.min_mag.setValue(5.0); self.min_mag.setSingleStep(0.1)
        self.max_mag = QDoubleSpinBox(); self.max_mag.setRange(0.0, 10.0); self.max_mag.setValue(9.5); self.max_mag.setSingleStep(0.1)
        self.min_dep = QDoubleSpinBox(); self.min_dep.setRange(0, 700); self.min_dep.setValue(0)
        self.max_dep = QDoubleSpinBox(); self.max_dep.setRange(0, 700); self.max_dep.setValue(700)
        self.min_dist = QDoubleSpinBox(); self.min_dist.setRange(0, 180); self.min_dist.setValue(30)
        self.max_dist = QDoubleSpinBox(); self.max_dist.setRange(0, 180); self.max_dist.setValue(90)

        self.chk_dyn_filter = QCheckBox("Enable magnitude-depth dynamic filter")
        self.chk_dyn_filter.setChecked(True)

        form.addRow("Catalog:", self.catalog_combo)
        form.addRow("Start Time:", self.ev_start_dt)
        form.addRow("End Time:", self.ev_end_dt)
        form.addRow("Magnitude (min/max):", self._row(self.min_mag, self.max_mag))
        form.addRow("Depth km (min/max):", self._row(self.min_dep, self.max_dep))
        form.addRow("Distance° (min/max):", self._row(self.min_dist, self.max_dist))
        form.addRow(self.chk_dyn_filter)

        btn_row = QHBoxLayout()
        self.btn_search_events = QPushButton("Search Events")
        self.btn_save_events = QPushButton("Save Events")
        self.btn_save_events.setEnabled(False)
        btn_row.addWidget(self.btn_search_events)
        btn_row.addWidget(self.btn_save_events)
        form.addRow(self._wrap(btn_row))

        left_layout.addLayout(form)

        # Right: map + table stacked, map given more space
        right_widget = QWidget(); right_inner = QVBoxLayout(right_widget)
        self.events_map = MapPane(add_draw_controls=False)
        right_inner.addWidget(self.events_map, stretch=3)
        self.event_table = QTableWidget(0, 6)
        self.event_table.setHorizontalHeaderLabels(["ID", "Time", "Lat", "Lon", "Depth", "Mag"])
        right_inner.addWidget(self.event_table, stretch=2)

        outer.addWidget(left, stretch=1)
        outer.addWidget(right_widget, stretch=3)

        # Connect
        self.btn_search_events.clicked.connect(self._on_search_events)
        self.btn_save_events.clicked.connect(self._on_save_events)

        return w

    def _row(self, *widgets) -> QWidget:
        box = QHBoxLayout()
        for w in widgets:
            box.addWidget(w)
        return self._wrap(box)

    def _on_search_events(self):
        if not self.center:
            QMessageBox.warning(self, "Center Required", "Please compute center from ROI on the Stations tab.")
            return

        catalog = self.catalog_combo.currentText()
        start = self.ev_start_dt.dateTime().toString(Qt.ISODate)
        end = self.ev_end_dt.dateTime().toString(Qt.ISODate)
        min_mag = float(self.min_mag.value())
        max_mag = float(self.max_mag.value())
        min_dep = float(self.min_dep.value())
        max_dep = float(self.max_dep.value())
        min_dist = float(self.min_dist.value())
        max_dist = float(self.max_dist.value())

        self.btn_search_events.setEnabled(False)

        def on_finished(result):
            self.btn_search_events.setEnabled(True)
            if result is None:
                QMessageBox.critical(self, "Error", "Event search failed.")
                return

            events = result
            # Apply dynamic filter if enabled
            if self.chk_dyn_filter.isChecked():
                passing, filtered_out = MagnitudeDepthFilter.apply_filter(events, enabled=True)
                self.events = passing
                filtered_ids = set(e['event_id'] for e in filtered_out)
            else:
                self.events = events
                filtered_ids = set()

            self._populate_event_table(self.events)
            # Show center, rings, and events on Events map
            try:
                self.events_map.clear_markers()
                # Plot rings for min/max distance
                rings = [min_dist, max_dist]
                self.events_map.set_center_and_rings(self.center, rings)
                self.events_map.add_events(self.events, filtered_ids=filtered_ids)
            except Exception:
                pass

            self.btn_save_events.setEnabled(True)
            self.logger.info(f"Event search complete: {len(self.events)} events.")

        def on_error(msg):
            self.btn_search_events.setEnabled(True)
            QMessageBox.critical(self, "Error", f"Event search failed: {msg}")

        worker = WorkerThread(
            self.event_service.search_events,
            catalog_source=catalog,
            center=self.center,
            start_time=start,
            end_time=end,
            min_magnitude=min_mag,
            max_magnitude=max_mag,
            min_depth=min_dep,
            max_depth=max_dep,
            min_distance=min_dist,
            max_distance=max_dist,
        )
        self._run_worker(worker, on_finished, on_error)

    def _populate_event_table(self, events: List[Dict]):
        self.event_table.setRowCount(0)
        for e in events:
            row = self.event_table.rowCount()
            self.event_table.insertRow(row)
            self.event_table.setItem(row, 0, QTableWidgetItem(e.get('event_id', '')))
            self.event_table.setItem(row, 1, QTableWidgetItem(e.get('time', '')))
            self.event_table.setItem(row, 2, QTableWidgetItem(f"{e.get('latitude', 0):.3f}"))
            self.event_table.setItem(row, 3, QTableWidgetItem(f"{e.get('longitude', 0):.3f}"))
            self.event_table.setItem(row, 4, QTableWidgetItem(f"{e.get('depth', 0):.1f}"))
            self.event_table.setItem(row, 5, QTableWidgetItem(f"{e.get('magnitude', 0):.1f}"))
        self.event_table.resizeColumnsToContents()

    def _on_save_events(self):
        self.data_manager.set_events(self.events)
        self.data_manager.save_checkpoint("events")
        # Export CSV if project_dir is set
        try:
            proj = self.data_manager.project_dir
            if proj:
                events_csv = proj / 'data' / 'events' / 'events.csv'
                self.data_manager.export_events_csv(str(events_csv))
                self.logger.info("Saved events CSV.")
        except Exception as e:
            self.logger.warning(f"Could not export events CSV: {e}")
        QMessageBox.information(self, "Saved", f"Saved {len(self.events)} events to project (CSV).")

    # ------------------------
    # Download Tab
    # ------------------------
    def _build_download_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # Status labels
        status_row = QHBoxLayout()
        self.lbl_sta_count = QLabel("Stations: 0")
        self.lbl_evt_count = QLabel("Events: 0")
        status_row.addWidget(self.lbl_sta_count)
        status_row.addWidget(self.lbl_evt_count)
        status_row.addStretch()
        layout.addLayout(status_row)

        # Parameters form
        form = QFormLayout()
        self.time_before = QDoubleSpinBox(); self.time_before.setRange(0, 600); self.time_before.setValue(10)
        self.time_after = QDoubleSpinBox(); self.time_after.setRange(0, 3600); self.time_after.setValue(120)
        # Channel selection (families and components)
        self.channel_families = {
            'EH': QCheckBox('EH'),
            'HH': QCheckBox('HH'),
            'BH': QCheckBox('BH'),
            'LH': QCheckBox('LH'),
            'SH': QCheckBox('SH'),
            'VH': QCheckBox('VH'),
        }
        for k in self.channel_families:
            self.channel_families[k].setChecked(k in ('BH', 'HH'))
        fam_row = QHBoxLayout()
        for cb in self.channel_families.values():
            fam_row.addWidget(cb)
        self.channel_components = {
            'Z': QCheckBox('Z'),
            'N': QCheckBox('N'),
            'E': QCheckBox('E'),
        }
        for k in self.channel_components:
            self.channel_components[k].setChecked(True)
        comp_row = QHBoxLayout()
        for cb in self.channel_components.values():
            comp_row.addWidget(cb)

        # Phases selection for arrivals
        self.phase_P = QCheckBox('P'); self.phase_P.setChecked(True)
        self.phase_S = QCheckBox('S'); self.phase_S.setChecked(True)
        phases_row = QHBoxLayout(); phases_row.addWidget(self.phase_P); phases_row.addWidget(self.phase_S)

        self.location_code = QLineEdit("*")
        # Provider and auth
        self.provider = QComboBox(); self.provider.addItems(["IRIS", "GEOFON", "ORFEUS", "RESIF", "INGV", "ETH", "NCEDC", "SCEDC", "USGS"]) 
        self.username = QLineEdit(""); self.username.setPlaceholderText("optional username")
        self.password = QLineEdit(""); self.password.setPlaceholderText("optional password"); self.password.setEchoMode(QLineEdit.Password)
        # Download behavior
        self.bulk_download = QCheckBox("Bulk download"); self.bulk_download.setChecked(True)
        self.chunk_size = QSpinBox(); self.chunk_size.setRange(1, 10000); self.chunk_size.setValue(50)
        self.max_retries = QSpinBox(); self.max_retries.setRange(0, 10); self.max_retries.setValue(3)
        self.retry_delay = QDoubleSpinBox(); self.retry_delay.setRange(0.0, 60.0); self.retry_delay.setValue(2.0); self.retry_delay.setDecimals(1)
        # Output
        self.save_format = QComboBox(); self.save_format.addItems(["SAC", "MSEED"])
        # Velocity model selection
        self.vel_model = QComboBox(); self.vel_model.addItems(["IASP91", "AK135"])
        # Optional cleanup
        self.clean_gaps = QCheckBox("Clean gaps after download")
        self.fill_value = QDoubleSpinBox(); self.fill_value.setRange(-1e6, 1e6); self.fill_value.setValue(0.0)
        self.max_gap = QDoubleSpinBox(); self.max_gap.setRange(0.0, 3600.0); self.max_gap.setValue(10.0)
        # Output dir
        self.output_dir = QLineEdit(""); self.btn_browse = QPushButton("Browse…")
        self.btn_browse.clicked.connect(self._on_browse_output)

        form.addRow("Time before P (s):", self.time_before)
        form.addRow("Time after P (s):", self.time_after)
        form.addRow("Channel families:", self._wrap(fam_row))
        form.addRow("Components:", self._wrap(comp_row))
        form.addRow("Location code:", self.location_code)
        form.addRow("Phases:", self._wrap(phases_row))
        form.addRow("Velocity model:", self.vel_model)
        form.addRow("Provider:", self.provider)
        form.addRow("Username:", self.username)
        form.addRow("Password:", self.password)
        form.addRow(self.bulk_download)
        form.addRow("Chunk size:", self.chunk_size)
        form.addRow("Max retries:", self.max_retries)
        form.addRow("Retry delay (s):", self.retry_delay)
        form.addRow("Save format:", self.save_format)
        form.addRow(self.clean_gaps)
        form.addRow("Fill value:", self.fill_value)
        form.addRow("Max gap (s):", self.max_gap)
        form.addRow("Output dir:", self._row(self.output_dir, self.btn_browse))

        layout.addLayout(form)

        # Buttons
        btn_row = QHBoxLayout()
        self.btn_compute_arrivals = QPushButton("Compute Arrivals")
        self.btn_download = QPushButton("Download Waveforms")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_save_streams = QPushButton("Save to Disk")
        self.btn_save_streams.setEnabled(False)
        # Require arrivals first
        self.btn_download.setEnabled(False)
        btn_row.addWidget(self.btn_compute_arrivals)
        btn_row.addWidget(self.btn_download)
        btn_row.addWidget(self.btn_stop)
        btn_row.addWidget(self.btn_save_streams)
        layout.addLayout(btn_row)

        # Progress bar
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)

        # Connect
        self.btn_compute_arrivals.clicked.connect(self._on_compute_arrivals)
        self.btn_download.clicked.connect(self._on_download)
        self.btn_stop.clicked.connect(self._on_stop_download)
        self.btn_save_streams.clicked.connect(self._on_save_streams)

        return w

    def _on_browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if path:
            self.output_dir.setText(path)

    def _on_compute_arrivals(self):
        if not self.events or not self.stations:
            QMessageBox.warning(self, "Missing Data", "Please select stations and events first.")
            return

        # Build selected phases
        phases = []
        if self.phase_P.isChecked(): phases.append('P')
        if self.phase_S.isChecked(): phases.append('S')
        self.logger.info(f"Computing theoretical arrivals ({','.join(phases)})...")

        def on_finished(result):
            self.theoretical_arrivals = result or {}
            QMessageBox.information(self, "Arrivals", f"Computed arrivals for {len(self.theoretical_arrivals)} pairs.")

        def on_error(msg):
            QMessageBox.critical(self, "Error", f"Arrival computation failed: {msg}")

        # Map velocity model to TauP names
        model = self.vel_model.currentText().lower()
        worker = WorkerThread(
            self.waveform_downloader.compute_theoretical_arrivals,
            self.events,
            self.stations,
            phases=phases,
            model=model
        )
        # When done, enable Download button
        def enable_download(*args, **kwargs):
            self.btn_download.setEnabled(True)
        def combined_finished(result):
            enable_download()
            on_finished(result)
        self._run_worker(worker, combined_finished, on_error)

    def _on_download(self):
        if not self.events or not self.stations:
            QMessageBox.warning(self, "Missing Data", "Please select stations and events first.")
            return

        if not self.theoretical_arrivals:
            QMessageBox.warning(self, "Arrivals Required", "Please compute theoretical arrivals before downloading.")
            return

        # Snapshot counts
        self.lbl_sta_count.setText(f"Stations: {len(self.stations)}")
        self.lbl_evt_count.setText(f"Events: {len(self.events)}")

        bulk = self.bulk_download.isChecked()

        self.logger.info("Starting waveform download…")
        self.waveform_downloader.reset_cancel()
        self.btn_stop.setEnabled(True)
        self.btn_download.setEnabled(False)
        self.btn_compute_arrivals.setEnabled(False)

        def on_finished(stream):
            self.btn_stop.setEnabled(False)
            self.btn_compute_arrivals.setEnabled(True)
            if stream is None or len(stream) == 0:
                QMessageBox.information(self, "Download", "Finished (no data or cancelled).")
                return
            self.downloaded_stream = stream
            self.logger.info(f"Downloaded {len(stream)} traces.")
            self.btn_save_streams.setEnabled(True)

        def on_error(msg):
            self.btn_stop.setEnabled(False)
            self.btn_compute_arrivals.setEnabled(True)
            QMessageBox.critical(self, "Error", f"Download failed: {msg}")

        # Build channel list from selections
        fams = [k for k,cb in self.channel_families.items() if cb.isChecked()]
        comps = [k for k,cb in self.channel_components.items() if cb.isChecked()]
        channels_list = []
        for fam in fams:
            for comp in comps:
                channels_list.append(fam + comp)
        channels_arg = ",".join(channels_list) if channels_list else "BHZ,BHN,BHE"

        worker = WorkerThread(
            self.waveform_downloader.download_waveforms,
            events=self.events,
            stations=self.stations,
            theoretical_arrivals=self.theoretical_arrivals,
            time_before=float(self.time_before.value()),
            time_after=float(self.time_after.value()),
            channels=channels_arg,
            location=self.location_code.text().strip(),
            bulk_download=bulk,
            chunk_size=int(self.chunk_size.value()),
            max_retries=int(self.max_retries.value()),
            retry_delay=float(self.retry_delay.value()),
            provider=self.provider.currentText(),
            username=self.username.text().strip() or None,
            password=self.password.text() or None,
            clean_gaps=self.clean_gaps.isChecked(),
            fill_value=float(self.fill_value.value()),
            max_gap=float(self.max_gap.value())
        )
        self._run_worker(worker, on_finished, on_error)

    def _on_stop_download(self):
        self.waveform_downloader.cancel()
        self.btn_stop.setEnabled(False)

    def _on_save_streams(self):
        if not hasattr(self, 'downloaded_stream'):
            QMessageBox.warning(self, "No Data", "Nothing to save.")
            return
        out_dir = self.output_dir.text().strip()
        if not out_dir:
            QMessageBox.warning(self, "Output Required", "Please choose an output directory.")
            return

        ok = self.waveform_downloader.save_waveforms(
            self.downloaded_stream,
            output_dir=out_dir,
            save_format=self.save_format.currentText()
        )
        if ok:
            QMessageBox.information(self, "Saved", "Waveforms saved successfully.")
        else:
            QMessageBox.warning(self, "Save Failed", "Could not save waveforms.")

    # ------------------------
    # Progress handlers
    # ------------------------
    def _on_progress_updated(self, task_id: str, current: int, total: int, percent: int):
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(percent)

    def _on_task_completed(self, task_id: str, success: bool):
        if success:
            self.statusBar().showMessage(f"Task '{task_id}' completed.", 5000)
        else:
            self.statusBar().showMessage(f"Task '{task_id}' failed.", 5000)

    def _on_task_failed(self, task_id: str, error_message: str):
        self.statusBar().showMessage(f"Task '{task_id}' failed: {error_message}", 8000)
        self.logger.error(f"Task '{task_id}' failed: {error_message}")

    def _maybe_sync_event_times(self, *args, **kwargs):
        if getattr(self, '_ev_time_synced', False):
            self.ev_start_dt.setDateTime(self.sta_start_dt.dateTime())
            self.ev_end_dt.setDateTime(self.sta_end_dt.dateTime())

    def _disable_time_sync(self, *args, **kwargs):
        # User modified event time; stop auto-sync
        self._ev_time_synced = False

    def _run_worker(self, worker: WorkerThread, on_finished, on_error):
        # Keep reference to avoid garbage collection
        self._workers.append(worker)
        def cleanup(*args, **kwargs):
            try:
                self._workers.remove(worker)
            except ValueError:
                pass
        worker.finished.connect(cleanup)
        worker.error.connect(cleanup)
        worker.finished.connect(on_finished)
        worker.error.connect(on_error)
        worker.start()
