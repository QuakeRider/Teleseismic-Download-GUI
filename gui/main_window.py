"""
Main GUI window for the Seismic Data Downloader.

Four tabs:
- Stations: ROI map + provider/network/channel filters + search
- Events: Catalog/time/magnitude/depth/distance filters + search
- Download: Parameters + arrivals + download + save
- Waveforms: Browse and plot downloaded mseed/sac waveforms
"""

import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from PyQt5.QtCore import Qt, QDateTime, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QLabel, QLineEdit, QComboBox, QDateTimeEdit, QDoubleSpinBox,
    QSpinBox, QCheckBox, QFileDialog, QProgressBar, QTextEdit, QDockWidget,
    QTableWidget, QTableWidgetItem, QMessageBox, QDialog, QDialogButtonBox,
    QRadioButton, QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem,
    QSplitter, QGroupBox, QSlider, QScrollArea
)

# Matplotlib imports for waveform plotting
import matplotlib
matplotlib.use('Qt5Agg')
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
import matplotlib.pyplot as plt

# ObsPy imports for waveform reading
try:
    from obspy import read, Stream, UTCDateTime
    HAS_OBSPY = True
except ImportError:
    HAS_OBSPY = False

from data.data_manager import DataManager
from services.station_service import StationService
from services.event_service import EventService, MagnitudeDepthFilter
from services.waveform_downloader import WaveformDownloader
from utils.logging_progress import ProgressManager, setup_logger
from gui.map_pane import MapPane


class ModeSelectionDialog(QDialog):
    """Startup dialog to choose between array-based and event-based modes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Download Mode")

        layout = QVBoxLayout(self)
        label = QLabel("Select the mode for this session:")
        layout.addWidget(label)

        self.array_radio = QRadioButton("Array-based mode (ROI / array analysis)")
        self.event_radio = QRadioButton("Event-based mode (single-event analysis)")
        self.array_radio.setChecked(True)

        layout.addWidget(self.array_radio)
        layout.addWidget(self.event_radio)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def selected_mode(self) -> str:
        """Return the selected mode string ('array' or 'event')."""
        return 'event' if self.event_radio.isChecked() else 'array'


class ProjectSelectionDialog(QDialog):
    """Startup dialog to create a new project or load an existing one."""

    def __init__(self, mode: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Project Setup")
        self.mode = mode
        self._project_path = None
        self._is_new_project = True

        layout = QVBoxLayout(self)

        # Instructions
        instruction_text = f"Set up your project for {mode}-based analysis:"
        label = QLabel(instruction_text)
        layout.addWidget(label)

        # Radio buttons for new vs existing project
        self.new_project_radio = QRadioButton("Create New Project")
        self.load_project_radio = QRadioButton("Load Existing Project")
        self.new_project_radio.setChecked(True)

        layout.addWidget(self.new_project_radio)
        layout.addWidget(self.load_project_radio)

        # Project directory selection
        dir_layout = QHBoxLayout()
        self.project_dir_input = QLineEdit("")
        self.project_dir_input.setPlaceholderText("Select project directory...")
        self.btn_browse = QPushButton("Browse...")
        self.btn_browse.clicked.connect(self._on_browse)
        dir_layout.addWidget(QLabel("Directory:"))
        dir_layout.addWidget(self.project_dir_input)
        dir_layout.addWidget(self.btn_browse)
        layout.addLayout(dir_layout)

        # Project name input (only for new projects)
        name_layout = QHBoxLayout()
        self.project_name_input = QLineEdit("")
        self.project_name_input.setPlaceholderText("Optional: project folder name")
        name_layout.addWidget(QLabel("Project Name:"))
        name_layout.addWidget(self.project_name_input)
        self.name_widget = QWidget()
        self.name_widget.setLayout(name_layout)
        layout.addWidget(self.name_widget)

        # Help text
        self.help_label = QLabel()
        self.help_label.setWordWrap(True)
        self._update_help_text()
        layout.addWidget(self.help_label)

        # Buttons
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        # Connect radio buttons to update UI
        self.new_project_radio.toggled.connect(self._on_mode_changed)
        self.load_project_radio.toggled.connect(self._on_mode_changed)

        self._on_mode_changed()

    def _on_mode_changed(self):
        """Update UI based on whether creating new or loading existing project."""
        is_new = self.new_project_radio.isChecked()
        self.name_widget.setVisible(is_new)
        self._update_help_text()

    def _update_help_text(self):
        """Update help text based on selection."""
        if self.new_project_radio.isChecked():
            help_text = (
                "Create a new project: Select a parent directory and optionally provide a project name. "
                "A new folder will be created with the simplified structure:\n"
                "  • events.csv, events.json (event data)\n"
                "  • stations.csv, stations.json (station data)\n"
                "  • waveforms/ (downloaded waveform files)\n"
                "  • stationxml/ (station response files)"
            )
        else:
            help_text = (
                "Load an existing project: Select a project directory that contains previously saved data. "
                "The application will scan for events.csv, stations.csv, and other files to restore your session."
            )
        self.help_label.setText(help_text)

    def _on_browse(self):
        """Open directory picker."""
        if self.new_project_radio.isChecked():
            # For new project, select parent directory
            path = QFileDialog.getExistingDirectory(self, "Select Parent Directory for New Project")
        else:
            # For existing project, select the project directory itself
            path = QFileDialog.getExistingDirectory(self, "Select Existing Project Directory")

        if path:
            self.project_dir_input.setText(path)

    def _on_accept(self):
        """Validate and accept the dialog."""
        base_path = self.project_dir_input.text().strip()

        if not base_path:
            QMessageBox.warning(self, "No Directory", "Please select a directory.")
            return

        from pathlib import Path

        if self.new_project_radio.isChecked():
            # Creating new project
            project_name = self.project_name_input.text().strip()
            if project_name:
                # Create subdirectory with project name
                self._project_path = Path(base_path) / project_name
            else:
                # Use the selected directory as-is
                self._project_path = Path(base_path)

            # Check if directory already exists and has project files
            if self._project_path.exists():
                # Check if it looks like an existing project
                has_events = (self._project_path / "events.csv").exists() or (self._project_path / "data" / "events" / "events.csv").exists()
                has_stations = (self._project_path / "stations.csv").exists() or (self._project_path / "data" / "stations" / "stations.csv").exists()

                if has_events or has_stations:
                    reply = QMessageBox.question(
                        self,
                        "Directory Exists",
                        f"The directory '{self._project_path}' already contains project files. Do you want to use it as an existing project?",
                        QMessageBox.Yes | QMessageBox.No
                    )
                    if reply == QMessageBox.Yes:
                        self._is_new_project = False
                    else:
                        return

            self._is_new_project = True
        else:
            # Loading existing project
            self._project_path = Path(base_path)

            if not self._project_path.exists():
                QMessageBox.warning(self, "Invalid Directory", "The selected directory does not exist.")
                return

            # Check if it looks like a project directory
            has_events = (self._project_path / "events.csv").exists() or (self._project_path / "data" / "events" / "events.csv").exists()
            has_stations = (self._project_path / "stations.csv").exists() or (self._project_path / "data" / "stations" / "stations.csv").exists()

            if not has_events and not has_stations:
                reply = QMessageBox.question(
                    self,
                    "No Project Files Found",
                    f"The directory '{self._project_path}' doesn't appear to contain project files. Create a new project here?",
                    QMessageBox.Yes | QMessageBox.No
                )
                if reply == QMessageBox.Yes:
                    self._is_new_project = True
                else:
                    return

            self._is_new_project = False

        self.accept()

    def get_project_path(self) -> Optional[Path]:
        """Return the selected project path."""
        return self._project_path

    def is_new_project(self) -> bool:
        """Return whether this is a new project."""
        return self._is_new_project


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
    def __init__(self, data_manager: DataManager, base_logger: logging.Logger, mode: str = 'array'):
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
        self.current_event: Optional[Dict] = None
        # Event-mode specific state
        self.ev_mode_roi: Optional[Dict] = None
        self.ev_mode_events: List[Dict] = []
        self.ev_mode_selected_index: Optional[int] = None
        self.ev_mode_confirmed_index: Optional[int] = None
        self.mode = mode if mode in ("array", "event") else "array"

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

        # Initialize mode-specific tabs
        if self.mode == 'event':
            self._init_event_mode_tabs()
        else:
            self._init_array_mode_tabs()

        # Connect progress signals
        self.progress_manager.progress_updated.connect(self._on_progress_updated)
        self.progress_manager.task_completed.connect(self._on_task_completed)
        self.progress_manager.task_failed.connect(self._on_task_failed)

        self._workers = []  # Keep references to worker threads

        # Load project data if available and set default output directory
        self._load_project_data()

        self.logger.info(f"GUI initialized in {self.mode} mode.")

    def _load_project_data(self):
        """Load project data from DataManager and populate UI."""
        # Set default output directory to project root (for waveforms)
        if self.data_manager.project_dir:
            self.output_dir.setText(str(self.data_manager.project_dir))
            self.logger.info(f"Project directory: {self.data_manager.project_dir}")

        # Load events from DataManager if available
        events = self.data_manager.get_events()
        if events:
            self.events = events
            self.logger.info(f"Loaded {len(events)} events from project")

            # Populate event tables based on mode
            if self.mode == 'event':
                # Event mode: populate the event mode event table
                self._populate_ev_mode_event_table(events)
                # If there's one event, select it automatically
                if len(events) == 1:
                    self.current_event = events[0]
                    self.ev_mode_confirmed_index = 0
                    # Check the first row
                    if self.ev_mode_event_table.rowCount() > 0:
                        item = self.ev_mode_event_table.item(0, 0)
                        if item:
                            item.setCheckState(Qt.Checked)
            else:
                # Array mode: populate the array mode event table
                self._populate_event_table(events)

        # Load stations from DataManager if available
        stations = self.data_manager.get_stations()
        if stations:
            self.stations = stations
            self.logger.info(f"Loaded {len(stations)} stations from project")

            # Populate station tables based on mode
            if self.mode == 'event':
                self._populate_ev_mode_station_table(stations)
            else:
                self._populate_station_table(stations)

        # Load arrivals if available
        arrivals = self.data_manager.get_arrivals()
        if arrivals:
            self.theoretical_arrivals = arrivals
            self.logger.info(f"Loaded arrival data for {len(arrivals)} event-station pairs")

        # Enable save buttons if we have data
        if events:
            if self.mode == 'event':
                self.btn_ev_mode_save_events.setEnabled(True)
            else:
                self.btn_save_events.setEnabled(True)

        if stations:
            if self.mode == 'event':
                self.btn_ev_mode_save_stations.setEnabled(True)
            else:
                self.btn_save_stations.setEnabled(True)

    def _init_array_mode_tabs(self):
        """Initialize tabs for array-based (ROI-centered) workflow."""
        self.station_tab = self._build_station_tab()
        self.event_tab = self._build_event_tab()
        self.download_tab = self._build_download_tab()
        self.waveform_tab = self._build_waveform_tab()

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

        self.tabs.addTab(self.station_tab, "Stations")
        self.tabs.addTab(self.event_tab, "Events")
        self.tabs.addTab(self.download_tab, "Download")
        self.tabs.addTab(self.waveform_tab, "Waveforms")

    def _init_event_mode_tabs(self):
        """Initialize tabs for single-event based workflow."""
        self.event_mode_event_tab = self._build_event_mode_event_tab()
        self.event_mode_station_tab = self._build_event_mode_station_tab()
        self.download_tab = self._build_download_tab()
        self.waveform_tab = self._build_waveform_tab()

        self.tabs.addTab(self.event_mode_event_tab, "Event")
        self.tabs.addTab(self.event_mode_station_tab, "Stations")
        self.tabs.addTab(self.download_tab, "Download")
        self.tabs.addTab(self.waveform_tab, "Waveforms")

    # ------------------------
    # Event-based Mode Tabs
    # ------------------------
    def _build_event_mode_event_tab(self) -> QWidget:
        """Event tab for single-event based workflow.

        User draws a region on the map, sets time and magnitude ranges,
        then selects and confirms a specific event for analysis.
        """
        w = QWidget()
        outer = QHBoxLayout(w)

        # Left controls
        left = QWidget()
        left_layout = QVBoxLayout(left)
        form = QFormLayout()

        self.ev_mode_catalog_combo = QComboBox()
        self.ev_mode_catalog_combo.addItems(["IRIS", "USGS", "ISC"])

        # Time range
        self.ev_mode_start_dt = QDateTimeEdit()
        self.ev_mode_start_dt.setCalendarPopup(True)
        self.ev_mode_start_dt.setDateTime(QDateTime.currentDateTime().addDays(-30))
        self.ev_mode_end_dt = QDateTimeEdit()
        self.ev_mode_end_dt.setCalendarPopup(True)
        self.ev_mode_end_dt.setDateTime(QDateTime.currentDateTime())

        # Magnitude range
        self.ev_mode_min_mag = QDoubleSpinBox(); self.ev_mode_min_mag.setRange(0.0, 10.0); self.ev_mode_min_mag.setSingleStep(0.1); self.ev_mode_min_mag.setValue(5.0)
        self.ev_mode_max_mag = QDoubleSpinBox(); self.ev_mode_max_mag.setRange(0.0, 10.0); self.ev_mode_max_mag.setSingleStep(0.1); self.ev_mode_max_mag.setValue(9.5)

        form.addRow("Catalog:", self.ev_mode_catalog_combo)
        form.addRow("Start Time:", self.ev_mode_start_dt)
        form.addRow("End Time:", self.ev_mode_end_dt)
        form.addRow("Magnitude (min/max):", self._row(self.ev_mode_min_mag, self.ev_mode_max_mag))

        # Moment Tensor catalog selection
        mt_catalog_label = QLabel("MT Catalogs:")
        mt_catalog_label.setToolTip("Select catalogs to search for moment tensor data when confirming event")
        mt_catalog_row = QHBoxLayout()
        self.ev_mode_mt_iris_check = QCheckBox("IRIS")
        self.ev_mode_mt_iris_check.setToolTip("Basic focal mechanisms")
        self.ev_mode_mt_usgs_check = QCheckBox("USGS")
        self.ev_mode_mt_usgs_check.setChecked(True)
        self.ev_mode_mt_usgs_check.setToolTip("USGS moment tensor solutions")
        self.ev_mode_mt_isc_check = QCheckBox("ISC")
        self.ev_mode_mt_isc_check.setChecked(True)
        self.ev_mode_mt_isc_check.setToolTip("ISC/GCMT moment tensor solutions")
        mt_catalog_row.addWidget(self.ev_mode_mt_iris_check)
        mt_catalog_row.addWidget(self.ev_mode_mt_usgs_check)
        mt_catalog_row.addWidget(self.ev_mode_mt_isc_check)
        mt_catalog_row.addStretch()
        form.addRow(mt_catalog_label, self._wrap(mt_catalog_row))

        # Buttons for search, confirm, save
        btn_row = QHBoxLayout()
        self.btn_ev_mode_search_events = QPushButton("Search Events")
        self.btn_ev_mode_confirm_event = QPushButton("Confirm Event")
        self.btn_ev_mode_save_events = QPushButton("Save Event")
        self.btn_ev_mode_confirm_event.setEnabled(False)
        self.btn_ev_mode_save_events.setEnabled(False)
        btn_row.addWidget(self.btn_ev_mode_search_events)
        btn_row.addWidget(self.btn_ev_mode_confirm_event)
        btn_row.addWidget(self.btn_ev_mode_save_events)
        form.addRow(self._wrap(btn_row))

        left_layout.addLayout(form)

        # Right: map + table
        right_widget = QWidget(); right_inner = QVBoxLayout(right_widget)
        # Enable drawing controls so user can draw a box for event region
        self.ev_mode_events_map = MapPane(add_draw_controls=True)
        self.ev_mode_events_map.roi_changed.connect(self._on_ev_mode_roi_changed)
        right_inner.addWidget(self.ev_mode_events_map, stretch=3)

        # First column is a checkbox ("Use")
        self.ev_mode_event_table = QTableWidget(0, 8)
        self.ev_mode_event_table.setHorizontalHeaderLabels([
            "Use", "ID", "Time", "Lat", "Lon", "Depth", "Mag", "Catalog"
        ])
        right_inner.addWidget(self.ev_mode_event_table, stretch=2)

        # Selected / confirmed event summary
        self.ev_mode_selected_event_label = QLabel("No event selected.")
        right_inner.addWidget(self.ev_mode_selected_event_label)

        outer.addWidget(left, stretch=1)
        outer.addWidget(right_widget, stretch=3)

        # Connections
        self.btn_ev_mode_search_events.clicked.connect(self._on_ev_mode_search_events)
        self.btn_ev_mode_confirm_event.clicked.connect(self._on_ev_mode_confirm_event)
        self.btn_ev_mode_save_events.clicked.connect(self._on_ev_mode_save_events)
        self.ev_mode_event_table.cellClicked.connect(self._on_ev_mode_event_cell_clicked)
        self.ev_mode_event_table.itemChanged.connect(self._on_ev_mode_event_item_changed)

        return w

    def _build_event_mode_station_tab(self) -> QWidget:
        """Station tab for single-event workflow.

        Searches for stations by epicentral distance from the selected event.
        """
        w = QWidget()
        outer = QHBoxLayout(w)

        # Left controls
        left = QWidget(); left_layout = QVBoxLayout(left)
        form = QFormLayout()

        # Providers (scrollable list with checkboxes)
        self.ev_mode_provider_list = QListWidget()
        self.ev_mode_provider_list.setMaximumHeight(120)
        for name in ["IRIS", "GEOFON", "ORFEUS", "RESIF", "INGV", "ETHZ", "NCEDC", "SCEDC", "USGS", "BGR", "AUSPASS", "ICGC", "UIB-NORSAR", "IPGP", "LMU", "KOERI", "KNMI", "NOA", "GEONET", "ISC"]:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            if name == "IRIS":
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)
            self.ev_mode_provider_list.addItem(item)
        form.addRow(QLabel("Providers:"), self.ev_mode_provider_list)

        # Network/station filters
        self.ev_mode_network_input = QLineEdit("*")
        self.ev_mode_station_input = QLineEdit("*")
        form.addRow("Networks:", self.ev_mode_network_input)
        form.addRow("Stations:", self.ev_mode_station_input)

        # Sensor families (BH, HH, EH, LH, SH, VH, UH) -> channel patterns like BH?,HH?
        fam_row = QHBoxLayout()
        self.ev_mode_channel_families = {}
        for fam in ["BH", "HH", "EH", "LH", "SH", "VH", "UH"]:
            cb = QCheckBox(fam)
            if fam in ("BH", "HH"):
                cb.setChecked(True)
            self.ev_mode_channel_families[fam] = cb
            fam_row.addWidget(cb)
        form.addRow("Sensor families:", self._wrap(fam_row))
        self.ev_mode_channels_input = QLineEdit("")
        form.addRow("Channel pattern(s):", self.ev_mode_channels_input)

        # Distance range relative to event
        self.ev_mode_min_dist = QDoubleSpinBox(); self.ev_mode_min_dist.setRange(0.0, 180.0); self.ev_mode_min_dist.setValue(30.0)
        self.ev_mode_max_dist = QDoubleSpinBox(); self.ev_mode_max_dist.setRange(0.0, 180.0); self.ev_mode_max_dist.setValue(90.0)
        form.addRow("Distance° (min/max):", self._row(self.ev_mode_min_dist, self.ev_mode_max_dist))

        # Time window around event (for station availability)
        self.ev_mode_sta_start_dt = QDateTimeEdit(); self.ev_mode_sta_start_dt.setCalendarPopup(True)
        self.ev_mode_sta_end_dt = QDateTimeEdit(); self.ev_mode_sta_end_dt.setCalendarPopup(True)
        now = QDateTime.currentDateTime()
        # Defaults; will be updated to surround confirmed event time when available
        self.ev_mode_sta_start_dt.setDateTime(now.addDays(-1))
        self.ev_mode_sta_end_dt.setDateTime(now.addDays(1))
        form.addRow("Start Time:", self.ev_mode_sta_start_dt)
        form.addRow("End Time:", self.ev_mode_sta_end_dt)

        controls_row = QHBoxLayout()
        self.btn_ev_mode_search_stations = QPushButton("Search Stations")
        self.btn_ev_mode_save_stations = QPushButton("Save Stations")
        self.btn_ev_mode_save_stations.setEnabled(False)
        controls_row.addWidget(self.btn_ev_mode_search_stations)
        controls_row.addWidget(self.btn_ev_mode_save_stations)
        form.addRow(self._wrap(controls_row))

        left_layout.addLayout(form)

        # Right: map + table
        right_widget = QWidget(); right_inner = QVBoxLayout(right_widget)
        # Map without drawing controls (event geometry comes from Event tab)
        self.ev_mode_station_map = MapPane(add_draw_controls=False)
        right_inner.addWidget(self.ev_mode_station_map, stretch=3)
        self.ev_mode_station_table = QTableWidget(0, 9)
        self.ev_mode_station_table.setHorizontalHeaderLabels([
            "Network", "Station", "Lat", "Lon", "Provider", "Channels", "Dist°", "Az", "Baz"
        ])
        right_inner.addWidget(self.ev_mode_station_table, stretch=2)

        outer.addWidget(left, stretch=1)
        outer.addWidget(right_widget, stretch=3)

        # Initialize channel patterns from families
        self._update_ev_mode_channels_from_families()
        for cb in self.ev_mode_channel_families.values():
            cb.stateChanged.connect(self._update_ev_mode_channels_from_families)

        # Connections
        self.btn_ev_mode_search_stations.clicked.connect(self._on_ev_mode_search_stations)
        self.btn_ev_mode_save_stations.clicked.connect(self._on_ev_mode_save_stations)

        return w

    def _update_ev_mode_channels_from_families(self):
        """Update channel pattern line edit from selected sensor families."""
        fams = [k for k, cb in self.ev_mode_channel_families.items() if cb.isChecked()]
        patterns = [f"{fam}?" for fam in fams]
        self.ev_mode_channels_input.setText(",".join(patterns) if patterns else "")

    def _on_ev_mode_roi_changed(self, roi_obj):
        """Store ROI for event-mode search when user draws/edits a shape."""
        self.ev_mode_roi = roi_obj
        self.logger.info("Event-mode ROI updated.")

    def _on_ev_mode_search_events(self):
        """Search for events using the event-mode controls and ROI box."""
        catalog = self.ev_mode_catalog_combo.currentText()

        # Require ROI defining the geographic search region
        roi = self.ev_mode_roi or self.ev_mode_events_map.get_current_roi()
        if not roi:
            QMessageBox.warning(self, "ROI Required", "Please draw a rectangle or circle on the map to define the event region.")
            return

        bbox = MapPane.extract_bbox_from_roi(roi)
        if not bbox:
            QMessageBox.warning(self, "Invalid ROI", "Could not extract bounding box from the ROI.")
            return
        min_lon, min_lat, max_lon, max_lat = bbox

        # Compute center from ROI for use in distance-based search
        center = MapPane.compute_center_from_roi(roi)
        if not center:
            QMessageBox.warning(self, "Invalid ROI", "Could not compute center from the ROI.")
            return
        center_lat, center_lon = center

        # Time and magnitude ranges
        start = self.ev_mode_start_dt.dateTime().toString(Qt.ISODate)
        end = self.ev_mode_end_dt.dateTime().toString(Qt.ISODate)
        min_mag = float(self.ev_mode_min_mag.value())
        max_mag = float(self.ev_mode_max_mag.value())

        self.btn_ev_mode_search_events.setEnabled(False)
        self.ev_mode_selected_index = None
        self.ev_mode_confirmed_index = None
        self.current_event = None
        self.btn_ev_mode_confirm_event.setEnabled(False)
        self.btn_ev_mode_save_events.setEnabled(False)

        def on_finished(result):
            self.btn_ev_mode_search_events.setEnabled(True)
            if result is None:
                QMessageBox.critical(self, "Error", "Event search failed.")
                return

            # Filter by ROI bbox and magnitude range
            events = [
                e for e in result
                if min_lat <= e.get('latitude', 0.0) <= max_lat
                and min_lon <= e.get('longitude', 0.0) <= max_lon
                and min_mag <= e.get('magnitude', 0.0) <= max_mag
            ]

            self.ev_mode_events = events
            self.events = events[:]  # Make available to download tab if needed
            self._populate_ev_mode_event_table(events)
            self.ev_mode_selected_event_label.setText("No event selected.")

            # Draw ROI and events with no highlights yet
            self._refresh_ev_mode_event_map_highlights()

            self.btn_ev_mode_confirm_event.setEnabled(len(events) > 0)
            self.logger.info(f"Event-mode search complete: {len(events)} candidate events.")

        def on_error(msg):
            self.btn_ev_mode_search_events.setEnabled(True)
            QMessageBox.critical(self, "Error", f"Event search failed: {msg}")

        worker = WorkerThread(
            self.event_service.search_events,
            catalog_source=catalog,
            center=center,
            start_time=start,
            end_time=end,
            min_magnitude=min_mag,
            max_magnitude=max_mag,
            min_depth=0.0,
            max_depth=700.0,
            min_distance=0.0,
            max_distance=180.0,
        )
        self._run_worker(worker, on_finished, on_error)

    def _populate_ev_mode_event_table(self, events: List[Dict]):
        self.ev_mode_event_table.blockSignals(True)
        self.ev_mode_event_table.setRowCount(0)
        for e in events:
            row = self.ev_mode_event_table.rowCount()
            self.ev_mode_event_table.insertRow(row)
            # Checkbox column
            use_item = QTableWidgetItem()
            use_item.setFlags(use_item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            use_item.setCheckState(Qt.Unchecked)
            self.ev_mode_event_table.setItem(row, 0, use_item)
            # Data columns
            self.ev_mode_event_table.setItem(row, 1, QTableWidgetItem(e.get('event_id', '')))
            self.ev_mode_event_table.setItem(row, 2, QTableWidgetItem(e.get('time', '')))
            self.ev_mode_event_table.setItem(row, 3, QTableWidgetItem(f"{e.get('latitude', 0):.3f}"))
            self.ev_mode_event_table.setItem(row, 4, QTableWidgetItem(f"{e.get('longitude', 0):.3f}"))
            self.ev_mode_event_table.setItem(row, 5, QTableWidgetItem(f"{e.get('depth', 0):.1f}"))
            self.ev_mode_event_table.setItem(row, 6, QTableWidgetItem(f"{e.get('magnitude', 0):.1f}"))
            self.ev_mode_event_table.setItem(row, 7, QTableWidgetItem(e.get('catalog_source', '')))
        self.ev_mode_event_table.blockSignals(False)
        self.ev_mode_event_table.resizeColumnsToContents()

    def _on_ev_mode_event_cell_clicked(self, row: int, column: int):
        """Handle clicks on event table rows (selection vs checkbox)."""
        if row < 0 or row >= len(self.ev_mode_events):
            return
        # Column 0 is the checkbox; any column click selects the row as the current candidate
        self.ev_mode_selected_index = row
        # Make candidate visible on map (yellow ring)
        self._refresh_ev_mode_event_map_highlights()

    def _on_ev_mode_event_item_changed(self, item: QTableWidgetItem):
        """Enforce single checked event and track confirmed event index."""
        if item.column() != 0:
            return
        row = item.row()
        if item.checkState() == Qt.Checked:
            # Uncheck all other rows
            self.ev_mode_event_table.blockSignals(True)
            rows = self.ev_mode_event_table.rowCount()
            for r in range(rows):
                if r == row:
                    continue
                other = self.ev_mode_event_table.item(r, 0)
                if other is not None and other.checkState() == Qt.Checked:
                    other.setCheckState(Qt.Unchecked)
            self.ev_mode_event_table.blockSignals(False)
            # Update confirmed index and current_event
            self.ev_mode_confirmed_index = row
            if 0 <= row < len(self.ev_mode_events):
                ev = self.ev_mode_events[row]
                self.current_event = ev
                self.events = [ev]
                # Enable confirm/save buttons
                self.btn_ev_mode_confirm_event.setEnabled(True)
                self.btn_ev_mode_save_events.setEnabled(True)
            self._refresh_ev_mode_event_map_highlights()
        else:
            # Checkbox unchecked for this row
            if self.ev_mode_confirmed_index == row:
                self.ev_mode_confirmed_index = None
                self.current_event = None
                self.events = []
                self.btn_ev_mode_save_events.setEnabled(False)
            self._refresh_ev_mode_event_map_highlights()

    def _refresh_ev_mode_event_map_highlights(self):
        """Redraw ROI and events, highlighting selected and confirmed with rings.

        - All events: red markers with black outline (via MapPane.add_events).
        - Selected (but not confirmed): yellow ring.
        - Confirmed (checkbox): blue ring.
        """
        try:
            # Clear all layers and redraw ROI if present
            self.ev_mode_events_map.clear_markers()
            roi = self.ev_mode_roi
            if roi:
                bbox = MapPane.extract_bbox_from_roi(roi)
                if bbox:
                    min_lon, min_lat, max_lon, max_lat = bbox
                    # Redraw ROI rectangle
                    self.ev_mode_events_map.draw_rectangle(min_lat, min_lon, max_lat, max_lon)
            # Plot events
            if self.ev_mode_events:
                self.ev_mode_events_map.add_events(self.ev_mode_events)
            # Add highlight rings
            def _add_ring_for_index(idx: Optional[int], color: str):
                if idx is None or idx < 0 or idx >= len(self.ev_mode_events):
                    return
                ev = self.ev_mode_events[idx]
                lat = ev.get('latitude', 0.0)
                lon = ev.get('longitude', 0.0)
                radius_m = 300000.0  # ~3 degrees, just for visual highlighting
                js = f"addRing({lat}, {lon}, {radius_m}, '{color}', '5,5', null);"
                self.ev_mode_events_map.web_view.page().runJavaScript(js)
            # Selected (yellow) and confirmed (blue)
            _add_ring_for_index(self.ev_mode_selected_index, '#ffff00')
            _add_ring_for_index(self.ev_mode_confirmed_index, '#0000ff')
        except Exception:
            pass

    def _on_ev_mode_confirm_event(self):
        """Confirm currently checked event and retrieve detailed moment tensor information."""
        if self.ev_mode_confirmed_index is None or not self.current_event:
            QMessageBox.warning(self, "No Event Selected", "Please check one event in the table before confirming.")
            return

        ev = self.current_event
        event_id = ev.get('event_id', '')
        event_time = ev.get('time', '')
        catalog_source = ev.get('catalog_source', 'USGS')

        # Update summary label
        summary = (
            f"Confirmed event: {event_id} | "
            f"M{ev.get('magnitude', 0):.1f} | "
            f"{event_time} | "
            f"({ev.get('latitude', 0):.3f}, {ev.get('longitude', 0):.3f})"
        )
        self.ev_mode_selected_event_label.setText(summary + " [Retrieving moment tensor...]")

        # Update station time window based on event time
        self._update_ev_mode_station_time_from_event()

        # Re-highlight map with confirmed event (blue ring)
        self._refresh_ev_mode_event_map_highlights()

        # Get selected MT catalogs
        mt_catalogs = []
        if self.ev_mode_mt_iris_check.isChecked():
            mt_catalogs.append("IRIS")
        if self.ev_mode_mt_usgs_check.isChecked():
            mt_catalogs.append("USGS")
        if self.ev_mode_mt_isc_check.isChecked():
            mt_catalogs.append("ISC")

        if not mt_catalogs:
            QMessageBox.warning(self, "No MT Catalogs", "Please select at least one catalog for moment tensor search.")
            return

        # Retrieve detailed event information including moment tensor
        self.btn_ev_mode_confirm_event.setEnabled(False)
        self.logger.info(f"Retrieving detailed information for event {event_id} from catalogs: {', '.join(mt_catalogs)}...")

        def on_finished(detailed_event):
            self.btn_ev_mode_confirm_event.setEnabled(True)
            if detailed_event is None:
                self.logger.warning(f"Could not retrieve detailed event information for {event_id}")
                self.ev_mode_selected_event_label.setText(summary + " [Moment tensor: not available]")
                return

            # Update current_event with detailed information
            self.current_event.update(detailed_event)

            # Also update in events list
            if self.ev_mode_confirmed_index is not None and self.ev_mode_confirmed_index < len(self.events):
                self.events[self.ev_mode_confirmed_index].update(detailed_event)

            # Update summary with MT status
            has_mt = detailed_event.get('has_moment_tensor', False)
            mt_status = "with moment tensor" if has_mt else "no moment tensor"
            self.ev_mode_selected_event_label.setText(summary + f" [{mt_status}]")

            if has_mt:
                mt_info = detailed_event.get('moment_tensor', {})
                agency = mt_info.get('source_agency', 'unknown')
                self.logger.info(f"Moment tensor found for {event_id} (source: {agency})")
            else:
                self.logger.info(f"No moment tensor available for {event_id}")

        def on_error(msg):
            self.btn_ev_mode_confirm_event.setEnabled(True)
            self.logger.error(f"Failed to retrieve event details: {msg}")
            self.ev_mode_selected_event_label.setText(summary + " [Error retrieving details]")

        worker = WorkerThread(
            self.event_service.get_event_details,
            catalog_source=catalog_source,
            event_id=event_id,
            event_time=event_time,
            time_window_seconds=60.0,
            mt_catalogs=mt_catalogs
        )
        self._run_worker(worker, on_finished, on_error)

    def _update_ev_mode_station_time_from_event(self):
        """Set station time window around the confirmed event time (±1 day by default)."""
        if not self.current_event:
            return
        try:
            ev_time_str = self.current_event.get('time', '')
            if not ev_time_str:
                return
            ev_dt = QDateTime.fromString(ev_time_str, Qt.ISODate)
            if not ev_dt.isValid():
                return
            start_dt = ev_dt.addDays(-1)
            end_dt = ev_dt.addDays(1)
            self.ev_mode_sta_start_dt.setDateTime(start_dt)
            self.ev_mode_sta_end_dt.setDateTime(end_dt)
        except Exception:
            pass

    def _on_ev_mode_save_events(self):
        """Save selected event(s) to project (CSV + JSON)."""
        if not self.events:
            QMessageBox.warning(self, "No Events", "No events to save. Perform a search and select an event.")
            return

        self.data_manager.set_events(self.events)

        # Export CSV / JSON (and arrivals JSON if present) if project_dir is set
        try:
            proj = self.data_manager.project_dir
            if proj:
                # Use simplified structure: files in project root
                events_csv = proj / 'events.csv'
                events_json = proj / 'events.json'
                arrivals_json = proj / 'arrivals.json'
                self.data_manager.export_events_csv(str(events_csv))
                self.data_manager.export_events_json(str(events_json))
                if self.data_manager.get_arrivals():
                    self.data_manager.export_arrivals_json(str(arrivals_json))
                self.logger.info("Saved events CSV/JSON (and arrivals JSON if available).")
        except Exception as e:
            self.logger.warning(f"Could not export events CSV/JSON: {e}")

        QMessageBox.information(self, "Saved", f"Saved {len(self.events)} event(s) to project.")

    def _on_ev_mode_search_stations(self):
        """Search for stations by epicentral distance from the selected event."""
        if not self.current_event:
            QMessageBox.warning(self, "Event Required", "Please select an event on the Event tab first (double-click a row).")
            return

        providers = [self.ev_mode_provider_list.item(i).text()
                     for i in range(self.ev_mode_provider_list.count())
                     if self.ev_mode_provider_list.item(i).checkState() == Qt.Checked]
        if not providers:
            QMessageBox.warning(self, "No Providers", "Please select at least one provider.")
            return

        event_lat = float(self.current_event.get('latitude', 0.0))
        event_lon = float(self.current_event.get('longitude', 0.0))
        min_dist = float(self.ev_mode_min_dist.value())
        max_dist = float(self.ev_mode_max_dist.value())

        start_time = self.ev_mode_sta_start_dt.dateTime().toString(Qt.ISODate)
        end_time = self.ev_mode_sta_end_dt.dateTime().toString(Qt.ISODate)
        channels = self.ev_mode_channels_input.text().strip() or "BH?"

        self.btn_ev_mode_search_stations.setEnabled(False)

        def on_finished(result):
            self.btn_ev_mode_search_stations.setEnabled(True)
            if result is None:
                QMessageBox.critical(self, "Error", "Station search failed.")
                return

            self.stations = result
            self._populate_ev_mode_station_table(result)

            # Plot event, rings, and stations on the map
            try:
                self.ev_mode_station_map.clear_markers()
                self.ev_mode_station_map.set_center_and_rings((event_lat, event_lon), [min_dist, max_dist])
                self.ev_mode_station_map.add_events([self.current_event])
                self.ev_mode_station_map.add_stations(result)
            except Exception:
                pass

            self.btn_ev_mode_save_stations.setEnabled(True)
            self.logger.info(f"Event-mode station search complete: {len(result)} stations.")

        def on_error(msg):
            self.btn_ev_mode_search_stations.setEnabled(True)
            QMessageBox.critical(self, "Error", f"Station search failed: {msg}")

        worker = WorkerThread(
            self.station_service.search_stations_by_event_distance,
            providers=providers,
            event_lat=event_lat,
            event_lon=event_lon,
            min_distance_deg=min_dist,
            max_distance_deg=max_dist,
            networks=self.ev_mode_network_input.text().strip() or "*",
            stations=self.ev_mode_station_input.text().strip() or "*",
            channels=channels,
            start_time=start_time,
            end_time=end_time,
            include_closed=False,
        )
        self._run_worker(worker, on_finished, on_error)

    def _populate_ev_mode_station_table(self, stations: List[Dict]):
        self.ev_mode_station_table.setRowCount(0)
        for s in stations:
            row = self.ev_mode_station_table.rowCount()
            self.ev_mode_station_table.insertRow(row)
            self.ev_mode_station_table.setItem(row, 0, QTableWidgetItem(s.get('network', '')))
            self.ev_mode_station_table.setItem(row, 1, QTableWidgetItem(s.get('station', '')))
            self.ev_mode_station_table.setItem(row, 2, QTableWidgetItem(f"{s.get('latitude', 0):.3f}"))
            self.ev_mode_station_table.setItem(row, 3, QTableWidgetItem(f"{s.get('longitude', 0):.3f}"))
            self.ev_mode_station_table.setItem(row, 4, QTableWidgetItem(s.get('provider', '')))
            chan_types = s.get('channel_types') or []
            chan_list = s.get('channels') or []
            chan_display = ",".join(chan_types) if chan_types else ",".join(chan_list)
            self.ev_mode_station_table.setItem(row, 5, QTableWidgetItem(chan_display))
            self.ev_mode_station_table.setItem(row, 6, QTableWidgetItem(f"{s.get('distance_deg', 0):.2f}"))
            self.ev_mode_station_table.setItem(row, 7, QTableWidgetItem(f"{s.get('azimuth', 0):.1f}"))
            self.ev_mode_station_table.setItem(row, 8, QTableWidgetItem(f"{s.get('back_azimuth', 0):.1f}"))
        self.ev_mode_station_table.resizeColumnsToContents()

    def _on_ev_mode_save_stations(self):
        """Save event-mode stations to project (CSV + StationXML)."""
        if not self.stations:
            QMessageBox.warning(self, "No Stations", "No stations to save. Perform a station search first.")
            return

        self.data_manager.set_stations(self.stations)

        # Export CSV / JSON and StationXML if project_dir is set
        try:
            proj = self.data_manager.project_dir
            if not proj:
                QMessageBox.warning(self, "No Project", "No project directory set. Cannot save stations.")
                return

            # Use simplified structure: files in project root
            stations_csv = proj / 'stations.csv'
            stations_json = proj / 'stations.json'
            self.data_manager.export_stations_csv(str(stations_csv))
            self.data_manager.export_stations_json(str(stations_json))
            sx_dir = proj / 'stationxml'

            # Save StationXML in a background worker, constrained to event-mode time window and channels
            start_time = self.ev_mode_sta_start_dt.dateTime().toString(Qt.ISODate)
            end_time = self.ev_mode_sta_end_dt.dateTime().toString(Qt.ISODate)
            channels = self.ev_mode_channels_input.text().strip() or "BH?"

            self.btn_ev_mode_save_stations.setEnabled(False)

            def on_finished(count):
                self.btn_ev_mode_save_stations.setEnabled(True)
                n = int(count) if isinstance(count, int) else 0
                self.logger.info(f"Saved stations CSV and {n} StationXML files.")
                QMessageBox.information(self, "Saved", f"Saved {len(self.stations)} stations to project (CSV + {n} StationXML files).")

            def on_error(msg):
                self.btn_ev_mode_save_stations.setEnabled(True)
                self.logger.warning(f"Could not export StationXML: {msg}")
                QMessageBox.warning(self, "StationXML", f"StationXML export failed: {msg}")

            worker = WorkerThread(
                self.station_service.save_stationxml,
                self.stations,
                str(sx_dir),
                'response',
                120,
                start_time=start_time,
                end_time=end_time,
                channels=channels,
            )
            self._run_worker(worker, on_finished, on_error)
        except Exception as e:
            self.logger.warning(f"Could not export stations CSV/StationXML: {e}")
            QMessageBox.warning(self, "Save", f"Stations saved, but StationXML export failed: {e}")

    # ------------------------
    # Station Tab
    # ------------------------
    def _build_station_tab(self) -> QWidget:
        w = QWidget()
        outer = QHBoxLayout(w)

        # Left: controls
        left = QWidget(); left_layout = QVBoxLayout(left)
        form = QFormLayout()

        # Providers (scrollable list with checkboxes)
        self.provider_list = QListWidget()
        self.provider_list.setMaximumHeight(120)
        for name in ["IRIS", "GEOFON", "ORFEUS", "RESIF", "INGV", "ETHZ", "NCEDC", "SCEDC", "USGS", "BGR", "AUSPASS", "ICGC", "UIB-NORSAR", "IPGP", "LMU", "KOERI", "KNMI", "NOA", "GEONET", "ISC"]:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            if name == "IRIS":
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)
            self.provider_list.addItem(item)
        form.addRow(QLabel("Providers:"), self.provider_list)

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
        providers = [self.provider_list.item(i).text()
                     for i in range(self.provider_list.count())
                     if self.provider_list.item(i).checkState() == Qt.Checked]
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
        # Export CSV and StationXML if project_dir is set
        try:
            proj = self.data_manager.project_dir
            if not proj:
                QMessageBox.warning(self, "No Project", "No project directory set. Cannot save stations.")
                return

            # Use simplified structure: files in project root
            stations_csv = proj / 'stations.csv'
            stations_json = proj / 'stations.json'
            self.data_manager.export_stations_csv(str(stations_csv))
            self.data_manager.export_stations_json(str(stations_json))
            sx_dir = proj / 'stationxml'
            start_time = self.sta_start_dt.dateTime().toString(Qt.ISODate)
            end_time = self.sta_end_dt.dateTime().toString(Qt.ISODate)
            channels = self.channels_input.text().strip() or "BH?"

            self.btn_save_stations.setEnabled(False)

            def on_finished(count):
                self.btn_save_stations.setEnabled(True)
                n = int(count) if isinstance(count, int) else 0
                self.logger.info(f"Saved stations CSV and {n} StationXML files.")
                QMessageBox.information(self, "Saved", f"Saved {len(self.stations)} stations to project (CSV + {n} StationXML files).")

            def on_error(msg):
                self.btn_save_stations.setEnabled(True)
                self.logger.warning(f"Could not export StationXML: {msg}")
                QMessageBox.warning(self, "StationXML", f"StationXML export failed: {msg}")

            worker = WorkerThread(
                self.station_service.save_stationxml,
                self.stations,
                str(sx_dir),
                'response',
                120,
                start_time=start_time,
                end_time=end_time,
                channels=channels,
            )
            self._run_worker(worker, on_finished, on_error)
        except Exception as e:
            self.logger.warning(f"Could not export stations CSV/StationXML: {e}")
            QMessageBox.warning(self, "Save", f"Stations saved, but StationXML export failed: {e}")

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
        # Export CSV / JSON (and arrivals JSON if present) if project_dir is set
        try:
            proj = self.data_manager.project_dir
            if proj:
                # Use simplified structure: files in project root
                events_csv = proj / 'events.csv'
                events_json = proj / 'events.json'
                arrivals_json = proj / 'arrivals.json'
                self.data_manager.export_events_csv(str(events_csv))
                self.data_manager.export_events_json(str(events_json))
                # Export arrivals if we have any stored
                if self.data_manager.get_arrivals():
                    self.data_manager.export_arrivals_json(str(arrivals_json))
                self.logger.info("Saved events CSV/JSON (and arrivals JSON if available).")
        except Exception as e:
            self.logger.warning(f"Could not export events CSV/JSON: {e}")
        QMessageBox.information(self, "Saved", f"Saved {len(self.events)} events to project (CSV + JSON).")

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
        self.provider = QComboBox(); self.provider.addItems(["IRIS", "GEOFON", "ORFEUS", "RESIF", "INGV", "ETH", "NCEDC", "SCEDC", "USGS", "BGR", "AUSPASS", "ICGC", "UIB-NORSAR", "IPGP", "LMU", "KOERI", "KNMI", "NOA", "GEONET", "ISC"])
        self.provider.setToolTip("Fallback provider used only when a station doesn't have a provider assigned. Multi-provider downloads use each station's own provider.") 
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
        form.addRow("Provider (fallback):", self.provider)
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
            # Basic arrival times (seconds) for download windows
            self.theoretical_arrivals = result or {}

            # Also compute richer arrival details for downstream analysis and
            # store them via the DataManager so they can be exported.
            try:
                details = self.waveform_downloader.compute_arrival_details(
                    self.events,
                    self.stations,
                    phases=phases,
                    model=model,
                )
                self.data_manager.set_arrivals(details)
                self.logger.info(f"Computed detailed arrivals for {len(details)} event-station pairs.")
            except Exception as e:
                # Do not fail the GUI workflow if extra metadata cannot be computed.
                self.logger.warning(f"Could not compute detailed arrivals: {e}")

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
    # Waveform Viewer Tab
    # ------------------------
    def _build_waveform_tab(self) -> QWidget:
        """Build the waveform viewer tab for plotting downloaded seismic data."""
        w = QWidget()
        main_layout = QHBoxLayout(w)

        # Left panel: controls and station tree
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)

        # Directory selection group
        dir_group = QGroupBox("Waveform Directory")
        dir_layout = QVBoxLayout(dir_group)

        dir_row = QHBoxLayout()
        self.wf_dir_input = QLineEdit()
        self.wf_dir_input.setPlaceholderText("Select waveforms directory...")
        self.btn_wf_browse = QPushButton("Browse...")
        self.btn_wf_browse.clicked.connect(self._on_wf_browse_dir)
        dir_row.addWidget(self.wf_dir_input)
        dir_row.addWidget(self.btn_wf_browse)
        dir_layout.addLayout(dir_row)

        self.btn_wf_scan = QPushButton("Scan Directory")
        self.btn_wf_scan.clicked.connect(self._on_wf_scan_dir)
        dir_layout.addWidget(self.btn_wf_scan)

        left_layout.addWidget(dir_group)

        # Filter group
        filter_group = QGroupBox("Filters")
        filter_layout = QFormLayout(filter_group)

        # Channel type filter
        self.wf_channel_filter = QComboBox()
        self.wf_channel_filter.addItems(["All", "BH", "HH", "EH", "LH", "SH", "VH", "UH"])
        self.wf_channel_filter.currentTextChanged.connect(self._on_wf_filter_changed)
        filter_layout.addRow("Channel Type:", self.wf_channel_filter)

        # Component filter
        self.wf_component_filter = QComboBox()
        self.wf_component_filter.addItems(["All", "Z only", "N only", "E only", "3-component"])
        self.wf_component_filter.currentTextChanged.connect(self._on_wf_filter_changed)
        filter_layout.addRow("Components:", self.wf_component_filter)

        # Station filter
        self.wf_station_filter = QLineEdit()
        self.wf_station_filter.setPlaceholderText("Filter by station (e.g., AAK, *)")
        self.wf_station_filter.textChanged.connect(self._on_wf_filter_changed)
        filter_layout.addRow("Station:", self.wf_station_filter)

        left_layout.addWidget(filter_group)

        # Station/Waveform tree
        tree_group = QGroupBox("Available Waveforms")
        tree_layout = QVBoxLayout(tree_group)

        self.wf_tree = QTreeWidget()
        self.wf_tree.setHeaderLabels(["Station/Channel", "Components", "Samples", "Duration"])
        self.wf_tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.wf_tree.itemSelectionChanged.connect(self._on_wf_selection_changed)
        tree_layout.addWidget(self.wf_tree)

        # Selection buttons
        sel_row = QHBoxLayout()
        self.btn_wf_select_all = QPushButton("Select All")
        self.btn_wf_select_all.clicked.connect(self._on_wf_select_all)
        self.btn_wf_clear_sel = QPushButton("Clear Selection")
        self.btn_wf_clear_sel.clicked.connect(self._on_wf_clear_selection)
        sel_row.addWidget(self.btn_wf_select_all)
        sel_row.addWidget(self.btn_wf_clear_sel)
        tree_layout.addLayout(sel_row)

        left_layout.addWidget(tree_group, stretch=1)

        # Plot options group
        plot_group = QGroupBox("Plot Options")
        plot_layout = QFormLayout(plot_group)

        self.wf_normalize = QCheckBox("Normalize traces")
        self.wf_normalize.setChecked(True)
        plot_layout.addRow(self.wf_normalize)

        self.wf_filter_apply = QCheckBox("Apply bandpass filter")
        self.wf_filter_apply.setChecked(False)
        plot_layout.addRow(self.wf_filter_apply)

        freq_row = QHBoxLayout()
        self.wf_freq_min = QDoubleSpinBox()
        self.wf_freq_min.setRange(0.001, 50.0)
        self.wf_freq_min.setValue(0.01)
        self.wf_freq_min.setDecimals(3)
        self.wf_freq_max = QDoubleSpinBox()
        self.wf_freq_max.setRange(0.01, 50.0)
        self.wf_freq_max.setValue(2.0)
        self.wf_freq_max.setDecimals(2)
        freq_row.addWidget(QLabel("Low:"))
        freq_row.addWidget(self.wf_freq_min)
        freq_row.addWidget(QLabel("High:"))
        freq_row.addWidget(self.wf_freq_max)
        plot_layout.addRow("Freq (Hz):", self._wrap(freq_row))

        self.wf_plot_style = QComboBox()
        self.wf_plot_style.addItems(["Stacked", "Overlay", "Individual"])
        plot_layout.addRow("Plot Style:", self.wf_plot_style)

        self.wf_sort_by = QComboBox()
        self.wf_sort_by.addItems(["Station Name", "Distance", "Azimuth", "Back Azimuth"])
        plot_layout.addRow("Sort By:", self.wf_sort_by)

        left_layout.addWidget(plot_group)

        # Plot button
        self.btn_wf_plot = QPushButton("Plot Selected Waveforms")
        self.btn_wf_plot.clicked.connect(self._on_wf_plot)
        self.btn_wf_plot.setEnabled(False)
        left_layout.addWidget(self.btn_wf_plot)

        # Right panel: matplotlib canvas
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

        # Create matplotlib figure and canvas
        self.wf_figure = Figure(figsize=(10, 8), dpi=100)
        self.wf_canvas = FigureCanvas(self.wf_figure)
        self.wf_toolbar = NavigationToolbar(self.wf_canvas, right_panel)

        right_layout.addWidget(self.wf_toolbar)
        right_layout.addWidget(self.wf_canvas, stretch=1)

        # Status label
        self.wf_status_label = QLabel("No waveforms loaded. Select a directory and click 'Scan Directory'.")
        right_layout.addWidget(self.wf_status_label)

        # Use splitter for resizable panels
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        main_layout.addWidget(splitter)

        # Internal state
        self._wf_files: Dict[str, Dict] = {}  # path -> {stream, metadata}
        self._wf_grouped: Dict[str, Dict] = {}  # event_id -> station -> channel_type -> files

        return w

    def _on_wf_browse_dir(self):
        """Browse for waveform directory."""
        # Default to project waveforms dir if available
        default_dir = ""
        if self.data_manager.project_dir:
            wf_dir = self.data_manager.project_dir / 'waveforms'
            if wf_dir.exists():
                default_dir = str(wf_dir)

        path = QFileDialog.getExistingDirectory(self, "Select Waveforms Directory", default_dir)
        if path:
            self.wf_dir_input.setText(path)

    def _on_wf_scan_dir(self):
        """Scan the waveform directory for mseed and sac files."""
        wf_dir = self.wf_dir_input.text().strip()
        if not wf_dir:
            # Try default project waveforms directory
            if self.data_manager.project_dir:
                wf_dir = str(self.data_manager.project_dir / 'waveforms')
                self.wf_dir_input.setText(wf_dir)
            else:
                QMessageBox.warning(self, "No Directory", "Please select a waveforms directory.")
                return

        wf_path = Path(wf_dir)
        if not wf_path.exists():
            QMessageBox.warning(self, "Invalid Directory", f"Directory does not exist: {wf_dir}")
            return

        self.logger.info(f"Scanning waveform directory: {wf_dir}")
        self._wf_files.clear()
        self._wf_grouped.clear()
        self.wf_tree.clear()

        # Find all mseed and sac files
        mseed_files = list(wf_path.rglob("*.mseed"))
        sac_files = list(wf_path.rglob("*.sac"))
        all_files = mseed_files + sac_files

        if not all_files:
            QMessageBox.information(self, "No Files", "No mseed or sac files found in the directory.")
            self.wf_status_label.setText("No waveform files found.")
            return

        self.logger.info(f"Found {len(all_files)} waveform files")

        # Process files in background
        self.btn_wf_scan.setEnabled(False)
        self.wf_status_label.setText(f"Scanning {len(all_files)} files...")

        def scan_files():
            files_info = {}
            grouped = {}

            for fpath in all_files:
                try:
                    # Read waveform header only for speed
                    st = read(str(fpath), headonly=True)
                    if len(st) == 0:
                        continue

                    tr = st[0]
                    net = tr.stats.network
                    sta = tr.stats.station
                    loc = tr.stats.location
                    cha = tr.stats.channel
                    npts = tr.stats.npts
                    sr = tr.stats.sampling_rate
                    duration = npts / sr if sr > 0 else 0

                    # Extract channel type (first 2 chars) and component (last char)
                    channel_type = cha[:2] if len(cha) >= 2 else cha
                    component = cha[-1] if len(cha) >= 1 else ""

                    # Get event_id from parent directory name
                    event_id = fpath.parent.name

                    files_info[str(fpath)] = {
                        'path': str(fpath),
                        'network': net,
                        'station': sta,
                        'location': loc,
                        'channel': cha,
                        'channel_type': channel_type,
                        'component': component,
                        'npts': npts,
                        'sampling_rate': sr,
                        'duration': duration,
                        'event_id': event_id,
                    }

                    # Group by event -> station -> channel_type
                    if event_id not in grouped:
                        grouped[event_id] = {}
                    if sta not in grouped[event_id]:
                        grouped[event_id][sta] = {}
                    if channel_type not in grouped[event_id][sta]:
                        grouped[event_id][sta][channel_type] = []
                    grouped[event_id][sta][channel_type].append(str(fpath))

                except Exception as e:
                    self.logger.warning(f"Could not read {fpath}: {e}")

            return files_info, grouped

        def on_finished(result):
            self.btn_wf_scan.setEnabled(True)
            if result is None:
                QMessageBox.warning(self, "Error", "Failed to scan waveform files.")
                return

            files_info, grouped = result
            self._wf_files = files_info
            self._wf_grouped = grouped

            self._populate_wf_tree()
            self.wf_status_label.setText(f"Loaded {len(files_info)} waveform files from {len(grouped)} event(s).")
            self.logger.info(f"Scanned {len(files_info)} waveform files from {len(grouped)} events")

        def on_error(msg):
            self.btn_wf_scan.setEnabled(True)
            QMessageBox.critical(self, "Error", f"Scan failed: {msg}")

        worker = WorkerThread(scan_files)
        self._run_worker(worker, on_finished, on_error)

    def _populate_wf_tree(self):
        """Populate the waveform tree widget based on filters."""
        self.wf_tree.clear()

        channel_filter = self.wf_channel_filter.currentText()
        component_filter = self.wf_component_filter.currentText()
        station_filter = self.wf_station_filter.text().strip().upper()

        for event_id, stations in sorted(self._wf_grouped.items()):
            event_item = QTreeWidgetItem([event_id, "", "", ""])
            event_item.setData(0, Qt.UserRole, {'type': 'event', 'event_id': event_id})
            has_children = False

            for sta_name, channel_types in sorted(stations.items()):
                # Apply station filter
                if station_filter and station_filter != "*":
                    if station_filter not in sta_name:
                        continue

                sta_item = QTreeWidgetItem([sta_name, "", "", ""])
                sta_item.setData(0, Qt.UserRole, {'type': 'station', 'station': sta_name, 'event_id': event_id})
                has_sta_children = False

                for chan_type, file_paths in sorted(channel_types.items()):
                    # Apply channel type filter
                    if channel_filter != "All" and chan_type != channel_filter:
                        continue

                    # Get component info
                    components = set()
                    total_samples = 0
                    total_duration = 0
                    for fp in file_paths:
                        info = self._wf_files.get(fp, {})
                        comp = info.get('component', '')
                        if comp:
                            components.add(comp)
                        total_samples += info.get('npts', 0)
                        total_duration = max(total_duration, info.get('duration', 0))

                    # Apply component filter
                    if component_filter == "Z only" and 'Z' not in components:
                        continue
                    elif component_filter == "N only" and 'N' not in components:
                        continue
                    elif component_filter == "E only" and 'E' not in components:
                        continue
                    elif component_filter == "3-component":
                        if not ({'Z', 'N', 'E'}.issubset(components) or
                                {'Z', '1', '2'}.issubset(components)):
                            continue

                    comp_str = ",".join(sorted(components))
                    duration_str = f"{total_duration:.1f}s" if total_duration > 0 else ""

                    chan_item = QTreeWidgetItem([f"{chan_type}", comp_str, str(len(file_paths)), duration_str])
                    chan_item.setData(0, Qt.UserRole, {
                        'type': 'channel',
                        'channel_type': chan_type,
                        'station': sta_name,
                        'event_id': event_id,
                        'files': file_paths
                    })

                    sta_item.addChild(chan_item)
                    has_sta_children = True

                if has_sta_children:
                    event_item.addChild(sta_item)
                    has_children = True

            if has_children:
                self.wf_tree.addTopLevelItem(event_item)
                event_item.setExpanded(True)

        self.wf_tree.resizeColumnToContents(0)
        self.wf_tree.resizeColumnToContents(1)
        self.wf_tree.resizeColumnToContents(2)

    def _on_wf_filter_changed(self, *args):
        """Re-filter the tree when filter settings change."""
        self._populate_wf_tree()

    def _on_wf_selection_changed(self):
        """Handle selection changes in the waveform tree."""
        selected = self.wf_tree.selectedItems()
        self.btn_wf_plot.setEnabled(len(selected) > 0)

    def _on_wf_select_all(self):
        """Select all visible items in the tree."""
        self.wf_tree.selectAll()

    def _on_wf_clear_selection(self):
        """Clear all selections."""
        self.wf_tree.clearSelection()

    def _get_selected_waveform_files(self) -> List[str]:
        """Get list of selected waveform file paths."""
        selected_files = []
        selected_items = self.wf_tree.selectedItems()

        for item in selected_items:
            data = item.data(0, Qt.UserRole)
            if not data:
                continue

            item_type = data.get('type')

            if item_type == 'channel':
                # Directly selected channel type - add all its files
                selected_files.extend(data.get('files', []))

            elif item_type == 'station':
                # Station selected - add all channel types under it
                for i in range(item.childCount()):
                    child = item.child(i)
                    child_data = child.data(0, Qt.UserRole)
                    if child_data:
                        selected_files.extend(child_data.get('files', []))

            elif item_type == 'event':
                # Event selected - add all stations and channels under it
                for i in range(item.childCount()):
                    sta_item = item.child(i)
                    for j in range(sta_item.childCount()):
                        chan_item = sta_item.child(j)
                        chan_data = chan_item.data(0, Qt.UserRole)
                        if chan_data:
                            selected_files.extend(chan_data.get('files', []))

        # Remove duplicates while preserving order
        seen = set()
        unique_files = []
        for f in selected_files:
            if f not in seen:
                seen.add(f)
                unique_files.append(f)

        return unique_files

    def _on_wf_plot(self):
        """Plot the selected waveforms."""
        if not HAS_OBSPY:
            QMessageBox.warning(self, "ObsPy Required", "ObsPy is required for waveform plotting.")
            return

        selected_files = self._get_selected_waveform_files()
        if not selected_files:
            QMessageBox.warning(self, "No Selection", "Please select waveforms to plot.")
            return

        self.logger.info(f"Plotting {len(selected_files)} waveform files...")
        self.wf_status_label.setText(f"Loading {len(selected_files)} waveforms...")
        self.btn_wf_plot.setEnabled(False)

        def load_and_plot():
            # Load all selected waveforms
            st = Stream()
            for fpath in selected_files:
                try:
                    st += read(fpath)
                except Exception as e:
                    self.logger.warning(f"Could not read {fpath}: {e}")
            return st

        def on_finished(stream):
            self.btn_wf_plot.setEnabled(True)

            if stream is None or len(stream) == 0:
                QMessageBox.warning(self, "No Data", "Could not load any waveform data.")
                self.wf_status_label.setText("No waveforms to display.")
                return

            self._plot_waveforms(stream)
            self.wf_status_label.setText(f"Plotted {len(stream)} traces.")

        def on_error(msg):
            self.btn_wf_plot.setEnabled(True)
            QMessageBox.critical(self, "Error", f"Failed to load waveforms: {msg}")
            self.wf_status_label.setText("Error loading waveforms.")

        worker = WorkerThread(load_and_plot)
        self._run_worker(worker, on_finished, on_error)

    def _plot_waveforms(self, stream: 'Stream'):
        """Plot the loaded waveforms on the matplotlib canvas."""
        # Apply processing if requested
        st = stream.copy()

        # Apply bandpass filter if enabled
        if self.wf_filter_apply.isChecked():
            try:
                freq_min = self.wf_freq_min.value()
                freq_max = self.wf_freq_max.value()
                st.filter('bandpass', freqmin=freq_min, freqmax=freq_max, corners=4, zerophase=True)
                self.logger.info(f"Applied bandpass filter: {freq_min}-{freq_max} Hz")
            except Exception as e:
                self.logger.warning(f"Could not apply filter: {e}")

        # Sort traces
        sort_by = self.wf_sort_by.currentText()
        if sort_by == "Station Name":
            st.sort(['station'])
        elif sort_by == "Distance":
            # Sort by distance if available in arrivals data
            arrivals = self.data_manager.get_arrivals() or {}
            def get_distance(tr):
                key = f"{tr.stats.network}.{tr.stats.station}"
                for arr_key, arr_data in arrivals.items():
                    if key in arr_key:
                        return arr_data.get('distance_deg', 999)
                return 999
            st.traces.sort(key=get_distance)

        # Normalize if requested
        if self.wf_normalize.isChecked():
            for tr in st:
                tr.normalize()

        # Clear figure
        self.wf_figure.clear()

        plot_style = self.wf_plot_style.currentText()

        if plot_style == "Stacked":
            self._plot_stacked(st)
        elif plot_style == "Overlay":
            self._plot_overlay(st)
        else:  # Individual
            self._plot_individual(st)

        self.wf_canvas.draw()

    def _plot_stacked(self, stream: 'Stream'):
        """Plot waveforms in stacked/record section style."""
        n_traces = len(stream)
        if n_traces == 0:
            return

        ax = self.wf_figure.add_subplot(111)

        # Group by station for better organization
        traces_by_station = {}
        for tr in stream:
            sta_key = f"{tr.stats.network}.{tr.stats.station}"
            if sta_key not in traces_by_station:
                traces_by_station[sta_key] = []
            traces_by_station[sta_key].append(tr)

        y_offset = 0
        y_labels = []
        y_positions = []

        for sta_key in sorted(traces_by_station.keys()):
            traces = traces_by_station[sta_key]
            # Sort by channel within station
            traces.sort(key=lambda t: t.stats.channel)

            for tr in traces:
                times = tr.times()
                data = tr.data

                # Normalize for display
                if self.wf_normalize.isChecked():
                    data = data / (abs(data).max() + 1e-10)

                ax.plot(times, data + y_offset, 'k-', linewidth=0.5)

                label = f"{tr.stats.network}.{tr.stats.station}.{tr.stats.channel}"
                y_labels.append(label)
                y_positions.append(y_offset)

                y_offset += 1.5  # Spacing between traces

        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Station.Channel")
        ax.set_yticks(y_positions)
        ax.set_yticklabels(y_labels, fontsize=8)

        # Add title
        if len(stream) > 0:
            start_time = min(tr.stats.starttime for tr in stream)
            ax.set_title(f"Waveforms starting at {start_time}")

        ax.grid(True, alpha=0.3)
        self.wf_figure.tight_layout()

    def _plot_overlay(self, stream: 'Stream'):
        """Plot all waveforms overlaid on the same axes."""
        ax = self.wf_figure.add_subplot(111)

        colors = plt.cm.tab10.colors
        for i, tr in enumerate(stream):
            times = tr.times()
            data = tr.data

            if self.wf_normalize.isChecked():
                data = data / (abs(data).max() + 1e-10)

            color = colors[i % len(colors)]
            label = f"{tr.stats.network}.{tr.stats.station}.{tr.stats.channel}"
            ax.plot(times, data, color=color, linewidth=0.7, label=label, alpha=0.8)

        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Amplitude")

        if len(stream) > 0:
            start_time = min(tr.stats.starttime for tr in stream)
            ax.set_title(f"Waveforms starting at {start_time}")

        ax.legend(loc='upper right', fontsize=7, ncol=2)
        ax.grid(True, alpha=0.3)
        self.wf_figure.tight_layout()

    def _plot_individual(self, stream: 'Stream'):
        """Plot each trace in its own subplot."""
        n_traces = len(stream)
        if n_traces == 0:
            return

        # Calculate grid layout
        n_cols = min(2, n_traces)
        n_rows = (n_traces + n_cols - 1) // n_cols

        for i, tr in enumerate(stream):
            ax = self.wf_figure.add_subplot(n_rows, n_cols, i + 1)

            times = tr.times()
            data = tr.data

            if self.wf_normalize.isChecked():
                data = data / (abs(data).max() + 1e-10)

            ax.plot(times, data, 'k-', linewidth=0.5)

            label = f"{tr.stats.network}.{tr.stats.station}.{tr.stats.channel}"
            ax.set_title(label, fontsize=9)
            ax.set_xlabel("Time (s)", fontsize=8)

            if i % n_cols == 0:
                ax.set_ylabel("Amplitude", fontsize=8)

            ax.tick_params(labelsize=7)
            ax.grid(True, alpha=0.3)

        self.wf_figure.tight_layout()

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
