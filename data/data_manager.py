"""
Simplified data manager for standalone seismic data downloader.

This module provides a lightweight state manager for event selection,
station selection, and waveform downloading without RF processing dependencies.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime


class DataManager:
    """
    Simplified singleton data manager for seismic data download workflow.
    
    Manages state for:
    - Station inventory (list of dictionaries)
    - Event catalog (list of dictionaries)
    - Downloaded waveforms metadata
    - Project configuration
    """
    
    _instance = None
    
    def __new__(cls):
        """Ensure only one instance exists (Singleton pattern)."""
        if cls._instance is None:
            cls._instance = super(DataManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """Initialize data manager (only runs once due to singleton)."""
        if self._initialized:
            return
        
        self.project_dir = None
        self.state = {
            'stations': [],  # List of station dictionaries
            'events': [],  # List of event dictionaries
            'waveforms_metadata': [],  # List of downloaded waveform metadata
            'study_area': None,  # Study area ROI (dict with bbox or circle)
            'download_config': {},  # Download configuration parameters
            'arrivals': {},  # Theoretical arrival details per event-station pair
            'history': []  # Processing history
        }
        self._initialized = True
    
    def initialize_project(self, project_dir: str) -> bool:
        """
        Initialize project directory structure with simplified layout.

        New simplified structure:
            project/
            ├── events.csv, events.json, arrivals.json
            ├── stations.csv, stations.json
            ├── waveforms/
            │   └── <event_id>/
            │       └── *.sac or *.mseed
            └── stationxml/
                └── *.xml

        Args:
            project_dir: Path to project directory

        Returns:
            True if successful
        """
        try:
            self.project_dir = Path(project_dir)
            self.project_dir.mkdir(parents=True, exist_ok=True)

            # Create only the subdirectories needed for waveforms and stationxml
            (self.project_dir / 'waveforms').mkdir(parents=True, exist_ok=True)
            (self.project_dir / 'stationxml').mkdir(parents=True, exist_ok=True)

            return True
        except Exception as e:
            print(f"Failed to initialize project: {e}")
            return False

    def load_project(self, project_dir: str, mode: str = 'array') -> bool:
        """
        Load existing project by scanning for data files and restoring state.

        Scans for:
        - events.csv / events.json / arrivals.json
        - stations.csv / stations.json
        - Falls back to legacy data/* structure if files not found in root

        Args:
            project_dir: Path to project directory
            mode: 'array' or 'event' mode (determines what to load)

        Returns:
            True if successful (even if some files are missing)
        """
        try:
            self.project_dir = Path(project_dir)

            if not self.project_dir.exists():
                print(f"Project directory does not exist: {project_dir}")
                return False

            # Try to load events
            events_loaded = False

            # Check for events in root directory (new structure)
            events_csv = self.project_dir / 'events.csv'
            events_json = self.project_dir / 'events.json'
            arrivals_json = self.project_dir / 'arrivals.json'

            # Fall back to legacy data/events structure
            if not events_csv.exists() and not events_json.exists():
                events_csv = self.project_dir / 'data' / 'events' / 'events.csv'
                events_json = self.project_dir / 'data' / 'events' / 'events.json'
                arrivals_json = self.project_dir / 'data' / 'events' / 'arrivals.json'

            # Load events from JSON if available (more complete), otherwise CSV
            if events_json.exists():
                try:
                    with open(events_json, 'r', encoding='utf-8') as f:
                        events = json.load(f)
                        self.set_events(events)
                        events_loaded = True
                        print(f"Loaded {len(events)} events from {events_json}")
                except Exception as e:
                    print(f"Failed to load events from JSON: {e}")
            elif events_csv.exists():
                try:
                    import csv
                    events = []
                    with open(events_csv, 'r', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            # Convert numeric fields
                            for key in ['latitude', 'longitude', 'depth', 'magnitude', 'distance_deg']:
                                if key in row and row[key]:
                                    try:
                                        row[key] = float(row[key])
                                    except (ValueError, TypeError):
                                        pass
                            events.append(row)
                    self.set_events(events)
                    events_loaded = True
                    print(f"Loaded {len(events)} events from {events_csv}")
                except Exception as e:
                    print(f"Failed to load events from CSV: {e}")

            # Load arrivals if available
            if arrivals_json.exists():
                try:
                    with open(arrivals_json, 'r', encoding='utf-8') as f:
                        arrivals = json.load(f)
                        self.set_arrivals(arrivals)
                        print(f"Loaded arrivals data from {arrivals_json}")
                except Exception as e:
                    print(f"Failed to load arrivals: {e}")

            # Try to load stations
            stations_loaded = False

            # Check for stations in root directory (new structure)
            stations_csv = self.project_dir / 'stations.csv'
            stations_json = self.project_dir / 'stations.json'

            # Fall back to legacy data/stations structure
            if not stations_csv.exists() and not stations_json.exists():
                stations_csv = self.project_dir / 'data' / 'stations' / 'stations.csv'
                stations_json = self.project_dir / 'data' / 'stations' / 'stations.json'

            # Load stations from JSON if available (more complete), otherwise CSV
            if stations_json.exists():
                try:
                    with open(stations_json, 'r', encoding='utf-8') as f:
                        stations = json.load(f)
                        self.set_stations(stations)
                        stations_loaded = True
                        print(f"Loaded {len(stations)} stations from {stations_json}")
                except Exception as e:
                    print(f"Failed to load stations from JSON: {e}")
            elif stations_csv.exists():
                try:
                    import csv
                    stations = []
                    with open(stations_csv, 'r', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            # Convert numeric fields
                            for key in ['latitude', 'longitude', 'elevation', 'distance_deg', 'azimuth', 'back_azimuth']:
                                if key in row and row[key]:
                                    try:
                                        row[key] = float(row[key])
                                    except (ValueError, TypeError):
                                        pass
                            # Parse channel_types comma-separated list
                            if 'channel_types' in row and row['channel_types']:
                                row['channel_types'] = row['channel_types'].split(',')
                            stations.append(row)
                    self.set_stations(stations)
                    stations_loaded = True
                    print(f"Loaded {len(stations)} stations from {stations_csv}")
                except Exception as e:
                    print(f"Failed to load stations from CSV: {e}")

            if not events_loaded and not stations_loaded:
                print(f"Warning: No project data found in {project_dir}")
                return False

            return True

        except Exception as e:
            print(f"Failed to load project: {e}")
            return False

    def set_stations(self, stations: List[Dict]):
        """Store station list."""
        self.state['stations'] = stations
        self._add_history('set_stations', {'count': len(stations)})
    
    def get_stations(self) -> List[Dict]:
        """Retrieve station list."""
        return self.state['stations']
    
    def set_events(self, events: List[Dict]):
        """Store event list."""
        self.state['events'] = events
        self._add_history('set_events', {'count': len(events)})
    
    def get_events(self) -> List[Dict]:
        """Retrieve event list."""
        return self.state['events']

    def set_arrivals(self, arrivals: Dict):
        """Store theoretical arrival details for event-station pairs."""
        self.state['arrivals'] = arrivals or {}
        self._add_history('set_arrivals', {'count': len(self.state['arrivals'])})

    def get_arrivals(self) -> Dict:
        """Retrieve stored arrivals mapping."""
        return self.state.get('arrivals', {})
    
    def set_study_area(self, study_area: Dict):
        """Store study area ROI."""
        self.state['study_area'] = study_area
        self._add_history('set_study_area', study_area)
    
    def get_study_area(self) -> Optional[Dict]:
        """Retrieve study area ROI."""
        return self.state['study_area']
    
    def add_waveform_metadata(self, metadata: Dict):
        """Add waveform download metadata."""
        self.state['waveforms_metadata'].append(metadata)
    
    def get_waveforms_metadata(self) -> List[Dict]:
        """Retrieve waveform metadata list."""
        return self.state['waveforms_metadata']
    
    def set_download_config(self, config: Dict):
        """Store download configuration."""
        self.state['download_config'] = config
    
    def get_download_config(self) -> Dict:
        """Retrieve download configuration."""
        return self.state['download_config']
    
    def export_summary(self, output_file: str) -> bool:
        """
        Export state summary to JSON file.
        
        Args:
            output_file: Path to output JSON file
            
        Returns:
            True if successful
        """
        try:
            summary = {
                'project_dir': str(self.project_dir) if self.project_dir else None,
                'station_count': len(self.state['stations']),
                'event_count': len(self.state['events']),
                'waveform_count': len(self.state['waveforms_metadata']),
                'study_area': self.state['study_area'],
                'download_config': self.state['download_config'],
                'history': self.state['history']
            }
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(summary, f, indent=2)
            
            return True
        except Exception as e:
            print(f"Failed to export summary: {e}")
            return False

    def export_stations_csv(self, output_file: str) -> bool:
        """Export current stations list to CSV for downstream tools."""
        try:
            import csv
            fieldnames = [
                'network','station','latitude','longitude','elevation',
                'start_date','end_date','site_name','provider','channel_types',
                'distance_deg','azimuth','back_azimuth'
            ]
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for s in self.state['stations']:
                    row = {k: s.get(k) for k in fieldnames}
                    # channel_types list to comma string
                    ct = s.get('channel_types') or []
                    row['channel_types'] = ','.join(ct)
                    writer.writerow(row)
            return True
        except Exception as e:
            print(f"Failed to export stations CSV: {e}")
            return False

    def export_events_csv(self, output_file: str) -> bool:
        """Export current events list to CSV for downstream tools.

        The CSV remains relatively flat and human-readable but includes
        additional scalar fields useful for source-parameter analysis when
        available (uncertainties, multiple magnitudes, MT flag).
        """
        try:
            import csv
            fieldnames = [
                'event_id','time','latitude','longitude','depth','magnitude',
                'magnitude_type','distance_deg','catalog_source',
                'origin_time_uncertainty_s','latitude_uncertainty_deg',
                'longitude_uncertainty_deg','depth_uncertainty_km',
                'mw','mw_type','mw_author',
                'mb','mb_type','mb_author',
                'ms','ms_type','ms_author',
                'has_moment_tensor',
            ]
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for e in self.state['events']:
                    row = {k: e.get(k) for k in fieldnames}
                    writer.writerow(row)
            return True
        except Exception as e:
            print(f"Failed to export events CSV: {e}")
            return False

    def export_events_json(self, output_file: str) -> bool:
        """Export current events list (with full metadata) to a JSON file."""
        try:
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, 'w', encoding='utf-8') as f:
                # state['events'] should already be JSON-serializable (including
                # nested moment_tensor, uncertainties, and multiple magnitudes).
                json.dump(self.state['events'], f, indent=2)
            return True
        except Exception as e:
            print(f"Failed to export events JSON: {e}")
            return False

    def export_stations_json(self, output_file: str) -> bool:
        """Export current stations list (full metadata) to a JSON file."""
        try:
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(self.state['stations'], f, indent=2)
            return True
        except Exception as e:
            print(f"Failed to export stations JSON: {e}")
            return False

    def export_arrivals_json(self, output_file: str) -> bool:
        """Export stored arrival details to JSON for downstream analysis."""
        try:
            Path(output_file).parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(self.get_arrivals(), f, indent=2)
            return True
        except Exception as e:
            print(f"Failed to export arrivals JSON: {e}")
            return False
    
    def clear_state(self):
        """Reset state to initial values."""
        self.state = {
            'stations': [],
            'events': [],
            'waveforms_metadata': [],
            'study_area': None,
            'download_config': {},
            'arrivals': {},
            'history': []
        }
    
    def _add_history(self, action: str, details: Dict):
        """Add entry to processing history."""
        entry = {
            'timestamp': datetime.now().isoformat(),
            'action': action,
            'details': details
        }
        self.state['history'].append(entry)
