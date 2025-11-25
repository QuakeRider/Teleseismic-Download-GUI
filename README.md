# Seismic Data Downloader

A standalone event and station selector with waveform downloading capabilities for seismic data analysis.

## Overview

This application provides a simplified interface for two complementary workflows:
- **Array-based mode (ROI / array analysis)**: Define a study area using a geographic ROI, select stations within that ROI, then search for events by epicentral distance from the array center.
- **Event-based mode (single-event analysis)**: Define a geographic search region, find and confirm a specific earthquake, then search for stations by epicentral distance from that event.

In both modes, you can:
- **Station Selection**: Query multiple FDSN providers concurrently for seismic stations with rich metadata.
- **Event Selection**: Search earthquake catalogs with distance-based filtering and optional dynamic magnitude-depth cutoffs.
- **Waveform Download**: Download seismic waveforms (bulk or per-trace) with progress tracking, retry logic, and flexible channel selection.

## Features

### Station Selection
- Multi-provider concurrent queries (IRIS, GEOFON, ORFEUS, RESIF, INGV, ETH/ETHZ, NCEDC, SCEDC, USGS, BGR, AUSPASS, ICGC, UIB-NORSAR, IPGP, LMU, KOERI, KNMI, NOA, GEONET, ISC)
- Interactive map for region-of-interest (ROI) selection (array mode) or event-centered station selection (event mode)
- Rectangle and circle ROI drawing for defining study areas or event search regions
- Station filtering by network, station codes, channels (sensor families such as BH/HH/EH/LH/SH/VH/UH), and time ranges
- Automatic deduplication across providers, with provenance tracked
- Exportable station list including latitude/longitude, provider, channel types, and event-centric metadata (distance in degrees, azimuth, back-azimuth)

### Event Selection
- Catalog queries from IRIS, USGS, and ISC
- Array mode: filtering by magnitude, depth, time, and distance from the array (study area) center, with dynamic magnitude-depth cutoff filtering
- Event mode: time and magnitude ranges plus a drawn geographic ROI to find candidate events in a specific region
- Clear event selection: table with a single "Use" checkbox and a **Confirm Event** button; selected and confirmed events are highlighted on the map with colored rings
- **Multi-catalog moment tensor search**: retrieves detailed focal mechanism data from multiple sources
- Computed epicentral distances and optional event statistics
- Exportable event list to CSV and JSON, including moment tensor / focal mechanism details, origin uncertainties, and multiple magnitude estimates

### Waveform Download
- Bulk download support grouped by FDSN provider for efficient, multi-provider data retrieval
- Non-bulk per-trace downloads that honor each station's own provider
- Theoretical arrival time calculations (P and S phases) using TauP with configurable velocity models (IASP91/AK135)
- **Enhanced arrival metadata**: exports include takeoff angles and ray parameters for source studies
- Configurable time windows around P arrival (before/after in seconds)
- **Smart channel selection**: automatically matches requested families (BH/HH/EH) with station-available types
- Progress tracking, retry logic, and cancellation support
- Optional gap detection and cleanup (merge, fill value, max gap)
- Save to SAC or MSEED formats, organized by event

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

### Startup and Modes

On startup, the application shows a **mode selection dialog**:
- **Array-based mode (ROI / array analysis)**
  - Tabs: `Stations | Events | Download`
- **Event-based mode (single-event analysis)**
  - Tabs: `Event | Stations | Download`

You must choose a mode to continue. You can always restart the program to switch modes.

### Array-based Workflow

1. **Set Up Project**
   - Create a new project or load an existing one on the **Project** tab
   - Optionally set the default output directory for downloaded data

2. **Select Study Area & Stations** (Stations tab)
   - Draw a rectangle or circle on the interactive map to define your study area ROI
   - Click **Compute Center from ROI** to define the array center
   - Choose FDSN providers to query (20+ providers available)
   - Set network/station codes and channel filters (e.g., `BH?` auto-expands to `BHZ,BHN,BHE`)
   - Set the station time range
   - Click **Search Stations** to retrieve available stations; they are plotted as triangles (color-coded by network)
   - Click **Save Stations** to write `stations.csv` and station XML metadata (time- and channel-constrained) in the project

3. **Select Events** (Events tab)
   - Set the event time range
   - Configure magnitude and depth ranges
   - Set distance range from the array center (degrees)
   - Optionally enable the dynamic magnitude-depth filtering
   - Click **Search Events** to retrieve events; they are shown on the Events map with geodesic distance rings
   - Click **Save Events** to export events to CSV and JSON (`events.csv` and `events.json`)

4. **Download Waveforms** (Download tab)
   - Configure download parameters (time before/after P, channel families, location code, phases, velocity model, provider/auth, bulk vs non-bulk, retries, cleanup)
   - Click **Compute Arrivals** to calculate theoretical P/S arrival times with TauP
   - Click **Download Waveforms** to retrieve data (bulk or per-trace); progress is shown at the bottom
   - **Multi-provider downloads** automatically route requests to each station's preferred provider
   - When finished, optionally click **Save to Disk** to write traces under the configured output directory

5. **Export & Save**
   - Save project checkpoints for later resumption
   - Use the DataManager utilities (or downstream tools) to inspect CSV/JSON outputs and waveform files

### Event-based Workflow

1. **Set Up Project**
   - Same as array mode

2. **Find and Confirm an Event** (Event tab)
   - Draw a rectangle or circle on the event map to define the general region where the event occurred
   - Set the event time range and magnitude range (e.g., all events with M ≥ 5)
   - Choose a catalog (IRIS/USGS/ISC)
   - Click **Search Events**; all events matching the time/magnitude/ROI criteria are listed and plotted as red circles
   - Use the **Use** checkbox column to select a single event candidate; the selected and confirmed events are highlighted with colored rings (yellow=selected, blue=confirmed)
   - Click **Confirm Event** to finalize the event for the session
     - **New**: Retrieves detailed moment tensor information from selected catalogs (IRIS/USGS/ISC)
     - The confirmed event's origin time is used to set the station time window automatically
   - Optionally click **Save Event** to export the event(s) to CSV and JSON (including moment tensor data)

3. **Select Stations Around the Event** (Stations tab)
   - Choose providers and sensor families (BH/HH/EH/etc.)
   - Adjust the epicentral distance range (e.g., 30–90° from the confirmed event)
   - Review or adjust the automatically populated time window around the event
   - Click **Search Stations** to find stations that fall within the distance/time/channel constraints; they are plotted as triangles around the event
   - **Smart channel matching** ensures only available sensor types are requested per station
   - Click **Save Stations** to export the station list and download time- and channel-filtered StationXML metadata in the background

4. **Download Waveforms** (Download tab)
   - Same as in array mode; the Download tab uses the confirmed event plus the selected stations

5. **Export & Save**
   - Same as in array mode

## Project Structure

```
seismic-data-downloader/
├── data/
│   └── data_manager.py      # State management
├── services/
│   ├── station_service.py   # Multi-provider station queries
│   ├── event_service.py     # Event catalog queries with MT retrieval
│   └── waveform_downloader.py  # Waveform download logic
├── gui/
│   ├── main_window.py       # Main application window
│   └── map_pane.py          # Interactive Leaflet map component
├── utils/
│   └── logging_progress.py  # Logging and progress tracking
├── main.py                  # Application entry point
├── requirements.txt         # Python dependencies
└── README.md               # This file
```

## Output

By default, a project initialized with the downloader uses this structure:
```
project/
├── events.csv                     # Tabular event list (with uncertainties, multiple mags, MT flags)
├── events.json                    # Full event metadata (including moment tensors)
├── arrivals.json                  # Theoretical arrival details (times, takeoff angles, ray params)
├── stations.csv                   # Tabular station list (includes distance_deg, azimuth, back_azimuth)
├── stations.json                  # Full station metadata
├── waveforms/                     # Waveform files organized by event ID
│   └── <event_id_sanitized>/
│       ├── NET.STA.LOC.CHA.sac (or .mseed)
│       └── ...
└── stationxml/
    └── NET.STA.xml                # Station response XML files (one per station, time/channel constrained)
```

**Notes:**
- Invalid characters in event IDs are sanitized for filesystem safety (e.g., `?` → `_`)
- If an event ID is unavailable, a time-based folder is used as a fallback
- StationXML files include instrument responses used later by processing tools for deconvolution
- Arrivals JSON includes rich TauP metadata for source studies (optional)

## Configuration

### Station Search Parameters
- **Providers**: Select one or more FDSN providers from 20+ available sources
- **ROI**: Define geographic bounds (rectangle or circle)
- **Networks**: Network codes (supports wildcards like `*`, `IU`)
- **Stations**: Station codes (supports wildcards)
- **Channels**: Channel codes or families (e.g., `BH?` expands to `BHZ,BHN,BHE`)
- **Time Range**: Station operation dates

### Event Search Parameters
- **Catalog Source**: IRIS, USGS, or ISC
- **Time Range**: Event occurrence dates
- **Magnitude Range**: Minimum and maximum magnitudes
- **Depth Range**: Event depth in kilometers
- **Distance Range**: Epicentral distance from study area (degrees)
- **Dynamic Filter**: Optional magnitude-depth cutoff
- **MT Catalogs**: Select sources for moment tensor retrieval (IRIS/USGS/ISC)

### Download Parameters
- **Phases**: Select arrival phases for travel-time calculation (e.g., P, S)
- **Velocity Model**: IASP91 or AK135 for arrivals
- **Time Before/After**: Time window around P arrival (seconds)
- **Channel Families**: Select families (EH/HH/BH/LH/SH/VH/UH) and components (Z/N/E); automatically matched with station capabilities
- **Provider/Auth**: Provider dropdown and optional username/password; per-station providers override when available
- **Bulk Download**: Enable for efficiency (recommended); requests are grouped by provider
- **Chunk Size**: Entries per bulk request
- **Max Retries / Retry Delay**: Resilience settings for network errors
- **Clean Gaps**: Optional merge traces, fill value, max gap seconds
- **Save Format**: SAC or MSEED

## Dependencies

- **ObsPy**: Seismic data processing framework
- **PyQt5**: GUI framework
- **PyQtWebEngine**: Web view for interactive maps
- **NumPy/SciPy**: Numerical operations
- **Folium**: Interactive mapping
- **tqdm**: Progress bars

See `requirements.txt` for full list with version constraints.

## Troubleshooting

### Common Issues

1. **Map not displaying or markers not visible**
   - **Recent fix**: Map initialization now uses multiple fallback mechanisms
   - Ensure you have an internet connection for loading map tiles
   - Try closing and reopening the application after PyQt5/PyQtWebEngine updates
   - Check the log panel for JavaScript errors

2. **ROI not detected**
   - Draw the rectangle/circle and wait a moment before clicking Search
   - If needed, click **Clear ROI** and redraw
   - In event mode, an event search requires an ROI on the Event tab map
   - In array mode, the Events tab requires a center computed from the Stations ROI
   - **Recent fix**: Improved ROI persistence with JavaScript callbacks

3. **Markers (events or stations) not visible on some maps**
   - Ensure the search actually returned non-empty results (check the table rows)
   - For Events in array mode: confirm that the Stations ROI center has been computed and that distance/magnitude ranges are reasonable
   - For Stations in event mode: confirm an event has been confirmed on the Event tab and that the epicentral distance/time ranges are not overly restrictive
   - **Recent fix**: Geodesic rings now properly wrap around the globe

4. **No stations found**
   - Expand your geographic ROI or distance range
   - Check network/station codes and channel families
   - Try additional FDSN providers (20+ now available)
   - Verify station time range overlaps your query window
   - **Recent improvement**: Extended provider list and better error messages

5. **Download failures**
   - Check internet connection and provider availability
   - Reduce chunk size, increase retries or retry delay
   - Use the **Stop** button to cancel and rerun with adjusted settings
   - Try disabling bulk mode if a provider doesn't handle large bulk requests reliably
   - **Recent fix**: Multi-provider downloads now route to correct endpoints automatically

6. **Channel mismatch warnings**
   - **Recent fix**: Smart channel resolution now matches requested families with station capabilities
   - The application automatically filters to available channel types (e.g., won't request BH channels from HH-only stations)
   - Check station table "Channels" column to see what's available

7. **Missing arrivals or moment tensors**
   - Ensure both stations and events are selected
   - Verify phases and velocity model in Download tab
   - Check distance range and event depths
   - **New**: Moment tensors are retrieved from multiple catalogs when you confirm an event
   - Not all events have moment tensor solutions; check the log panel for catalog responses

8. **StationXML export appears slow or "Not Responding"**
   - StationXML is fetched in the background via worker threads, with progress reported in the status/progress bar and log panel
   - For large station sets and long time windows, this can still take some time
   - **Recent improvement**: Now time- and channel-constrained to only download needed metadata
   - Avoid closing the program while StationXML is being written

9. **Project loading issues**
   - **Recent fix**: Supports both legacy (`data/events/events.csv`) and new (project root) structures
   - If loading an old project, files will be found automatically
   - New projects use simplified flat structure for easier management

## Future Enhancements

Potential improvements for this standalone tool:
- Real-time progress for individual trace downloads in non-bulk mode
- Import existing station/event lists (CSV/JSON) and merge with freshly queried results
- Parallel waveform and StationXML downloads using multiprocessing or async I/O
- Waveform preview plots and basic QC (SNR, gaps, clipping) before saving
- Data availability checks before download to avoid empty requests
- Resume interrupted downloads and partial project recovery
- Optional integration with downstream processing tools (e.g., RF computation pipelines)
- Batch processing for multiple events/arrays

## License

This tool is provided as-is for seismic data acquisition and research purposes.

## Support

For issues, questions, or suggestions:
1. Check the **Troubleshooting** section above
2. Review the log panel in the application for detailed error messages
3. Ensure all dependencies are installed with correct versions
4. Check that FDSN providers are accessible from your network

**Tip**: Enable debug logging with `python main.py --debug` for detailed diagnostic information.