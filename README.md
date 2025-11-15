# Seismic Data Downloader

A standalone event and station selector with waveform downloading capabilities for seismic data analysis.

## Overview

This application provides a simplified interface for:
- **Station Selection**: Query multiple FDSN providers concurrently for seismic stations within a region of interest
- **Event Selection**: Search earthquake catalogs with distance-based filtering and dynamic magnitude-depth cutoffs
- **Waveform Download**: Bulk download seismic waveforms with progress tracking and retry logic


## Features

### Station Selection
- Multi-provider concurrent queries (IRIS, GEOFON, ORFEUS, RESIF, INGV, ETH, NCEDC, SCEDC, USGS)
- Interactive map for region-of-interest (ROI) selection
- Rectangle and circle ROI drawing
- Station filtering by network, station codes, channels, and time ranges
- Automatic deduplication across providers

### Event Selection
- Catalog queries from IRIS, USGS, and ISC
- Filtering by magnitude, depth, time, and distance from study area
- Dynamic magnitude-depth cutoff filter for optimal event selection
- Epicentral distance calculation from study area center
- Event statistics and visualization

### Waveform Download
- Bulk download support for efficient data retrieval
- Theoretical arrival time calculations (P and S phases) using TauP
- Configurable time windows around phase arrivals
- Progress tracking and retry logic
- Gap detection and cleanup
- Save to SAC or MSEED formats
- Organized output by event

## Installation

1. Clone or extract this directory
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

Run the application:
```bash
python main.py
```

Or with a specific project directory:
```bash
python main.py --project /path/to/project
```

### Workflow

1. **Set Up Project**
   - Create a new project or load an existing one
   - Specify output directory for downloaded data

2. **Select Study Area & Stations**
   - Define your region of interest on the interactive map
   - Choose FDSN providers to query
   - Set network/station codes and channel filters
   - Click "Search Stations" to retrieve available stations

3. **Select Events**
   - Set time range for earthquake search
   - Configure magnitude and depth ranges
   - Set distance range from study area center
   - Optionally enable dynamic magnitude-depth filtering
   - Click "Search Events" to retrieve events

4. **Download Waveforms**
   - Configure download parameters (time windows, channels, etc.)
   - Click "Compute Arrivals" to calculate theoretical P/S arrival times
   - Click "Download Waveforms" to retrieve data
   - Monitor progress and review downloaded traces

5. **Export & Save**
   - Save project checkpoint for later resumption
   - Export summary statistics as JSON
   - Review downloaded waveforms in output directory

## Project Structure

```
seismic-data-downloader/
├── data/
│   └── data_manager.py      # State management
├── services/
│   ├── station_service.py   # Multi-provider station queries
│   ├── event_service.py     # Event catalog queries
│   └── waveform_downloader.py  # Waveform download logic
├── gui/
│   └── main_window.py       # Main application window
├── utils/
│   └── logging_progress.py  # Logging and progress tracking
├── main.py                  # Application entry point
├── requirements.txt         # Python dependencies
└── README.md               # This file
```


## Configuration

### Station Search Parameters
- **Providers**: Select one or more FDSN providers
- **ROI**: Define geographic bounds (rectangle or circle)
- **Networks**: Network codes (supports wildcards like `*`, `IU`)
- **Stations**: Station codes (supports wildcards)
- **Channels**: Channel codes (e.g., `BH?`, `BHZ,BHN,BHE`)
- **Time Range**: Station operation dates

### Event Search Parameters
- **Catalog Source**: IRIS, USGS, or ISC
- **Time Range**: Event occurrence dates
- **Magnitude Range**: Minimum and maximum magnitudes
- **Depth Range**: Event depth in kilometers
- **Distance Range**: Epicentral distance from study area (degrees)
- **Dynamic Filter**: Optional magnitude-depth cutoff

### Download Parameters
- **Phases**: Select arrival phases for travel-time calculation (e.g., P, S)
- **Velocity Model**: IASP91 or AK135 for arrivals
- **Time Before/After**: Time window around P arrival (seconds)
- **Channels**: Select families (EH/HH/BH/LH/SH/VH) and components (Z/N/E)
- **Provider/Auth**: Provider dropdown and optional username/password
- **Bulk Download**: Enable for efficiency (recommended)
- **Chunk Size**: Events per bulk request
- **Max Retries / Retry Delay**: Resilience settings for network errors
- **Clean Gaps (optional)**: Merge traces, fill value, max gap seconds
- **Save Format**: SAC or MSEED

## Output

By default, a project initialized with the downloader uses this structure:
```
project/
├── data/
│   ├── waveforms/                 # Waveform files organized by event ID
│   │   └── <event_id_sanitized>/
│   │       ├── NET.STA.LOC.CHA.sac (or .mseed)
│   │       └── ...
│   ├── events/
│   │   └── events.csv             # Tabular event list for downstream tools
│   ├── stations/
│   │   └── stations.csv           # Tabular station list for downstream tools
│   └── stationxml/
│       └── NET.STA.xml            # Station response XML files (one per station)
├── output/
│   └── logs/
└── checkpoints/
```
If you choose a custom output directory in the Download tab, waveforms will be saved under `<output_dir>/waveforms/` following the same event-based layout.

Notes:
- Invalid characters in event IDs are sanitized for filesystem safety (e.g., `?` → `_`).
- If an event ID is unavailable, a time-based folder is used as a fallback.
- StationXML files include instrument responses used later by seismic-rf-gui for deconvolution.

## Dependencies

- **ObsPy**: Seismic data processing framework
- **PyQt5**: GUI framework
- **NumPy/SciPy**: Numerical operations
- **Folium**: Interactive mapping
- **tqdm**: Progress bars

See `requirements.txt` for full list with version constraints.

## Troubleshooting

### Common Issues

1. **ROI not detected**
   - Draw the rectangle/circle and wait a moment before clicking Search
   - If needed, click “Clear ROI” and redraw

2. **No stations found**
   - Expand your geographic ROI
   - Check network/station codes
   - Try additional FDSN providers
   - Verify station time range overlaps your query window

3. **Download failures**
   - Check internet connection and provider availability
   - Reduce chunk size, increase retries or retry delay
   - Use the Stop button to cancel and rerun with adjusted settings

4. **Missing arrivals**
   - Ensure stations and events are selected
   - Verify phases and velocity model
   - Check distance range and event depths

## Future Enhancements

Potential improvements for this standalone tool:
- Export station/event lists to CSV
- Import existing station/event lists
- Parallel downloads using multiprocessing
- Additional FDSN providers
- Waveform preview plots
- Data availability checks before download
- Resume interrupted downloads

## License

This tool is provided as-is for seismic data acquisition and research purposes.
