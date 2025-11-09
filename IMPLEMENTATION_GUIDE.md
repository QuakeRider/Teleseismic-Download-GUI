# Implementation Guide for Standalone Seismic Data Downloader

## Current Status

The standalone seismic data downloader has been successfully created with all core backend functionality implemented. The application is structured and ready for GUI integration.

### ✅ Completed Components

1. **Project Structure**
   - Directory structure created with data/, services/, gui/, utils/, output/ folders
   - Python package structure with __init__.py files

2. **Data Management** (`data/data_manager.py`)
   - Lightweight singleton data manager
   - Checkpoint save/load functionality
   - Project directory initialization
   - State management for stations, events, and waveforms

3. **Station Service** (`services/station_service.py`)
   - Multi-provider concurrent FDSN queries
   - Support for 9 providers (IRIS, GEOFON, ORFEUS, etc.)
   - Automatic deduplication
   - Circle/rectangle ROI filtering
   - Retry logic with exponential backoff

4. **Event Service** (`services/event_service.py`)
   - Catalog queries from IRIS, USGS, ISC
   - Epicentral distance calculation
   - Dynamic magnitude-depth filtering
   - Event statistics and sorting

5. **Waveform Downloader** (`services/waveform_downloader.py`)
   - Bulk and individual download modes
   - Theoretical arrival computation using TauP
   - Progress tracking integration
   - Gap detection and cleanup
   - Save to SAC/MSEED formats

6. **Utilities** (`utils/logging_progress.py`)
   - Thread-safe UI logging
   - Progress manager with Qt signals
   - File and console logging

7. **Documentation**
   - README.md with full usage instructions
   - requirements.txt with dependencies
   - main.py entry point

### ⏳ Remaining Work: GUI Implementation

The GUI needs to be implemented to tie all services together. Here's what needs to be done:

## Step 1: Copy Map Pane from RF GUI

The RF GUI already has a working map pane with Folium/Leaflet integration.

**Action Required:**
```bash
# From the original RF GUI project
cp seismic-rf-gui/gui/map_pane.py seismic-data-downloader/gui/map_pane.py
```

**Note:** The map_pane.py may need minor adjustments, but it should work as-is since it's self-contained.

## Step 2: Create Main Window (`gui/main_window.py`)

Create a simplified main window with 3 tabs instead of the 11+ tabs in the full RF GUI.

### Required Tabs:

#### Tab 1: Station Selection
**UI Elements:**
- Map widget (using map_pane.py)
- ROI controls:
  - Rectangle/Circle toggle
  - Coordinate inputs (min/max lat/lon or center/radius)
- Provider selection:
  - Checkboxes for each FDSN provider
- Filter controls:
  - Network codes (QLineEdit with wildcards)
  - Station codes (QLineEdit with wildcards)
  - Channel codes (QLineEdit, default: "BH?")
  - Start/End time (QDateEdit)
- Buttons:
  - "Search Stations" → calls StationService.search_stations()
  - "Save Stations" → stores to DataManager
- Results display:
  - QTableWidget showing found stations
  - Summary label (e.g., "Found 42 stations from 3 providers")

#### Tab 2: Event Selection
**UI Elements:**
- Map widget (showing study area and events)
- Catalog selection:
  - QComboBox for IRIS/USGS/ISC
- Time range:
  - Start/End date (QDateEdit)
- Magnitude range:
  - Min/Max (QDoubleSpinBox, 4.0-9.0)
- Depth range:
  - Min/Max (QDoubleSpinBox, 0-700 km)
- Distance range:
  - Min/Max (QDoubleSpinBox, 30-180 degrees)
- Dynamic filter:
  - QCheckBox "Enable magnitude-depth filter"
- Buttons:
  - "Search Events" → calls EventService.search_events()
  - "Apply Filter" → calls MagnitudeDepthFilter.apply_filter()
  - "Save Events" → stores to DataManager
- Results display:
  - QTableWidget showing events
  - Summary statistics

#### Tab 3: Waveform Download
**UI Elements:**
- Status display:
  - Label showing # of selected stations
  - Label showing # of selected events
  - Estimated download size/time
- Download parameters:
  - Time before P (QDoubleSpinBox, default: 10s)
  - Time after P (QDoubleSpinBox, default: 120s)
  - Channels (QLineEdit, default: "BHZ,BHN,BHE")
  - Location code (QLineEdit, default: "*")
  - Bulk download (QCheckBox, default: True)
  - Chunk size (QSpinBox, default: 50)
  - Max retries (QSpinBox, default: 3)
  - Save format (QComboBox: SAC/MSEED)
  - Output directory (QLineEdit with browse button)
- Buttons:
  - "Compute Arrivals" → calls WaveformDownloader.compute_theoretical_arrivals()
  - "Download Waveforms" → calls WaveformDownloader.download_waveforms()
  - "Save to Disk" → calls WaveformDownloader.save_waveforms()
- Progress display:
  - QProgressBar
  - Status QLabel
  - Log QTextEdit (using UILogHandler)

### Shared Elements:
- **Menu Bar:**
  - File → New Project, Open Project, Save Project, Export Summary, Exit
  - Help → About, Documentation
- **Status Bar:**
  - Current project directory
  - Last saved timestamp
- **Log Panel:**
  - Bottom dock widget with scrolling log view

### Main Window Class Structure:

```python
class MainWindow(QMainWindow):
    def __init__(self, data_manager, logger):
        # Initialize
        # Setup services
        # Create tabs
        # Connect signals
        
    def setup_services(self):
        # Create ProgressManager
        # Create StationService
        # Create EventService
        # Create WaveformDownloader
        
    def setup_ui(self):
        # Create tab widget
        # Create station tab
        # Create event tab
        # Create download tab
        # Create log panel
        # Create menu/status bars
        
    def on_search_stations(self):
        # Get ROI from map
        # Get providers from checkboxes
        # Call station_service.search_stations() in thread
        # Display results in table
        
    def on_search_events(self):
        # Get study area center
        # Get parameters from UI
        # Call event_service.search_events() in thread
        # Display results in table
        
    def on_download_waveforms(self):
        # Get stations and events from DataManager
        # Get parameters from UI
        # Call waveform_downloader.download_waveforms() in thread
        # Update progress bar
```

## Step 3: Thread Management

Since FDSN queries and downloads can be slow, use QThread for non-blocking operations:

```python
from PyQt5.QtCore import QThread, pyqtSignal

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
```

Usage:
```python
def on_search_stations(self):
    # Disable button
    self.search_button.setEnabled(False)
    
    # Create worker
    worker = WorkerThread(
        self.station_service.search_stations,
        providers=selected_providers,
        roi_bbox=roi_bbox,
        ...
    )
    
    # Connect signals
    worker.finished.connect(self.on_stations_found)
    worker.error.connect(self.on_search_error)
    
    # Start
    worker.start()
```

## Step 4: Integration Checklist

- [ ] Copy map_pane.py to gui/ folder
- [ ] Create main_window.py with basic structure
- [ ] Implement station selection tab
- [ ] Implement event selection tab
- [ ] Implement waveform download tab
- [ ] Connect services to UI elements
- [ ] Add thread management for long operations
- [ ] Implement project save/load in menu
- [ ] Test station search with multiple providers
- [ ] Test event search with distance filtering
- [ ] Test waveform download with arrivals
- [ ] Add error handling and user feedback
- [ ] Update main.py to launch MainWindow

## Step 5: Testing

Create a test workflow:

1. **Station Selection Test**
   ```
   - Draw ROI around California
   - Select IRIS and SCEDC providers
   - Network: IU, CI
   - Channels: BH?
   - Click Search Stations
   - Verify results displayed
   - Click Save Stations
   ```

2. **Event Selection Test**
   ```
   - Same ROI
   - Catalog: USGS
   - Date range: Last year
   - Magnitude: 5.0-7.0
   - Depth: 0-100 km
   - Distance: 30-90 degrees
   - Click Search Events
   - Verify results displayed
   - Click Save Events
   ```

3. **Download Test**
   ```
   - Verify station/event counts displayed
   - Time before/after: 10/120 seconds
   - Channels: BHZ
   - Click Compute Arrivals
   - Verify arrivals computed
   - Click Download (start with small subset!)
   - Monitor progress
   - Check output directory for SAC files
   ```

## Code Templates

### Minimal main_window.py Template:

```python
from PyQt5.QtWidgets import (QMainWindow, QTabWidget, QWidget, 
                             QVBoxLayout, QPushButton, QTextEdit)
from PyQt5.QtCore import QThread, pyqtSignal

from data.data_manager import DataManager
from services.station_service import StationService
from services.event_service import EventService
from services.waveform_downloader import WaveformDownloader
from utils.logging_progress import ProgressManager, setup_logger

class MainWindow(QMainWindow):
    def __init__(self, data_manager, logger):
        super().__init__()
        self.data_manager = data_manager
        self.logger = logger
        
        self.progress_manager = ProgressManager()
        self.station_service = StationService(self.progress_manager, self.logger)
        self.event_service = EventService(self.progress_manager, self.logger)
        self.waveform_downloader = WaveformDownloader(self.progress_manager, self.logger)
        
        self.setup_ui()
        
    def setup_ui(self):
        self.setWindowTitle("Seismic Data Downloader")
        self.setGeometry(100, 100, 1200, 800)
        
        # Create central widget with tabs
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        
        # Add tabs (implement these methods)
        self.tabs.addTab(self.create_station_tab(), "Stations")
        self.tabs.addTab(self.create_event_tab(), "Events")
        self.tabs.addTab(self.create_download_tab(), "Download")
    
    def create_station_tab(self):
        widget = QWidget()
        layout = QVBoxLayout()
        
        # Add your UI elements here
        btn = QPushButton("Search Stations (TODO)")
        layout.addWidget(btn)
        
        widget.setLayout(layout)
        return widget
    
    def create_event_tab(self):
        # Similar structure
        pass
    
    def create_download_tab(self):
        # Similar structure
        pass
```

Update main.py:
```python
# In main() function, replace the logger.info messages with:
from gui.main_window import MainWindow

main_window = MainWindow(data_manager, logger)
main_window.show()
sys.exit(app.exec_())
```

## Additional Resources

- **ObsPy Documentation**: https://docs.obspy.org/
- **PyQt5 Tutorial**: https://www.pythonguis.com/pyqt5-tutorial/
- **FDSN Web Services**: https://www.fdsn.org/webservices/
- **Original RF GUI**: Reference `seismic-rf-gui/gui/main_window.py` for patterns

## Tips

1. **Start Simple**: Get basic station search working first before adding all features
2. **Test Incrementally**: Test each service independently before connecting to GUI
3. **Use Existing Patterns**: The original RF GUI has working examples of all these patterns
4. **Handle Errors**: FDSN services can be unreliable - always show errors to user
5. **Progress Feedback**: Long operations should show progress - users appreciate it

## Next Steps

1. Review the original `seismic-rf-gui/gui/main_window.py` to understand the GUI patterns
2. Copy map_pane.py as-is
3. Start with a minimal main_window.py implementing just the station tab
4. Test with real data
5. Expand to event and download tabs
6. Polish and add error handling

The hardest part (the backend services) is done! The GUI is mostly layout and wiring.
