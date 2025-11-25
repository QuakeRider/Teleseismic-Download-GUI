"""
Station search service with multi-provider concurrent queries.

This service handles querying multiple FDSN providers concurrently,
deduplicating results, and returning normalized station records.
"""

import time
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional, Tuple
from datetime import datetime

try:
    from obspy import UTCDateTime
    from obspy.clients.fdsn import Client
    HAS_OBSPY = True
except ImportError:
    HAS_OBSPY = False

from utils.logging_progress import ProgressManager


class StationService:
    """
    Multi-provider station search with concurrency and retry logic.
    
    Queries FDSN providers for stations within a geographic ROI,
    applying time and channel filters.
    """
    
    # FDSN provider endpoints (from PROGRAM_MAPPING.md)
    PROVIDER_ENDPOINTS = {
        "IRIS": "IRIS",
        "GEOFON": "GFZ",
        "ORFEUS": "ORFEUS",
        "RESIF": "RESIF",
        "INGV": "INGV",
        "ETHZ": "ETH",
        "NCEDC": "NCEDC",
        "SCEDC": "SCEDC",
        "USGS": "USGS",
        "BGR": "BGR",
        "AUSPASS": "AUSPASS",
        "ICGC": "ICGC",
        "UIB-NORSAR": "UIB-NORSAR",
        "IPGP": "IPGP",
        "LMU": "LMU",
        "KOERI": "KOERI",
        "KNMI": "KNMI",
        "NOA": "NOA",
        "GEONET": "GEONET",
        "ISC": "ISC",
    }
    
    def __init__(
        self,
        progress_manager: ProgressManager,
        logger: logging.Logger,
        max_workers: int = 4
    ):
        """
        Initialize station service.
        
        Args:
            progress_manager: Progress tracking manager
            logger: Logger instance
            max_workers: Maximum concurrent provider queries
        """
        if not HAS_OBSPY:
            raise ImportError("ObsPy is required for StationService")
        
        self.progress_manager = progress_manager
        self.logger = logger
        self.max_workers = max_workers
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
    
    def _bbox_from_center_and_distance(
        self,
        lat: float,
        lon: float,
        max_distance_deg: float
    ) -> Tuple[float, float, float, float]:
        """Approximate bounding box that encloses a circle of given angular radius.

        Returns (min_lon, min_lat, max_lon, max_lat).
        """
        # Latitude bounds are straightforward in degrees
        lat_min = max(-90.0, lat - max_distance_deg)
        lat_max = min(90.0, lat + max_distance_deg)

        # Approximate longitudinal span accounting for latitude
        if abs(lat) >= 89.0:
            lon_span = 180.0
        else:
            coslat = max(math.cos(math.radians(lat)), 0.1)
            lon_span = min(max_distance_deg / coslat, 180.0)

        lon_min = max(-180.0, lon - lon_span)
        lon_max = min(180.0, lon + lon_span)
        return (lon_min, lat_min, lon_max, lat_max)

    def search_stations(
        self,
        providers: List[str],
        roi_bbox: Tuple[float, float, float, float],  # (min_lon, min_lat, max_lon, max_lat)
        networks: str = "*",
        stations: str = "*",
        channels: str = "BH?",
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        include_closed: bool = False
    ) -> List[dict]:
        """
        Search for stations across multiple providers.
        
        Args:
            providers: List of provider names (e.g., ["IRIS", "GEOFON"])
            roi_bbox: Bounding box (min_lon, min_lat, max_lon, max_lat)
            networks: Network codes (wildcards supported)
            stations: Station codes (wildcards supported)
            channels: Channel codes (wildcards supported)
            start_time: Start time (ISO format or None)
            end_time: End time (ISO format or None)
            include_closed: Include closed stations
            
        Returns:
            List of normalized station dictionaries
        """
        if not providers:
            self.logger.warning("No providers selected")
            return []
        
        task_id = "station_search"
        self.progress_manager.create_task(task_id, len(providers), "Searching stations")
        
        self.logger.info(f"Searching {len(providers)} provider(s) for stations...")
        
        # Submit queries to thread pool
        futures = {}
        for provider in providers:
            future = self.executor.submit(
                self._query_provider,
                provider, roi_bbox, networks, stations, channels,
                start_time, end_time, include_closed
            )
            futures[future] = provider
        
        # Collect results
        all_stations = []
        completed = 0
        
        for future in as_completed(futures):
            provider = futures[future]
            try:
                stations = future.result(timeout=120)  # 2 minute timeout
                if stations:
                    all_stations.extend(stations)
                    self.logger.info(f"Found {len(stations)} stations from {provider}")
                else:
                    self.logger.info(f"No stations found from {provider}")
            except Exception as e:
                self.logger.error(f"Failed to query {provider}: {e}")
            finally:
                completed += 1
                self.progress_manager.update_task(task_id, completed)
        
        # Deduplicate stations
        deduplicated = self._deduplicate_stations(all_stations)
        
        self.progress_manager.complete_task(task_id, success=True)
        self.logger.info(f"Total: {len(deduplicated)} unique stations after deduplication")
        
        return deduplicated

    def search_stations_by_event_distance(
        self,
        providers: List[str],
        event_lat: float,
        event_lon: float,
        min_distance_deg: float,
        max_distance_deg: float,
        networks: str = "*",
        stations: str = "*",
        channels: str = "BH?",
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        include_closed: bool = False
    ) -> List[dict]:
        """Search for stations and filter by epicentral distance from an event.

        This reuses the existing ROI-based search to get candidates, then
        computes event–station distances and azimuths to filter to the
        requested [min_distance_deg, max_distance_deg] range.
        """
        # Build ROI bounding box that covers the maximum distance
        roi_bbox = self._bbox_from_center_and_distance(event_lat, event_lon, max_distance_deg)

        # Use existing search machinery (with concurrency and progress tracking)
        all_stations = self.search_stations(
            providers=providers,
            roi_bbox=roi_bbox,
            networks=networks,
            stations=stations,
            channels=channels,
            start_time=start_time,
            end_time=end_time,
            include_closed=include_closed,
        )
        if not all_stations:
            return []

        try:
            from obspy.geodetics import gps2dist_azimuth, locations2degrees
        except Exception:
            self.logger.error("ObsPy geodetics is required for distance filtering in search_stations_by_event_distance.")
            return all_stations

        filtered: List[dict] = []
        for sta in all_stations:
            try:
                dist_deg = locations2degrees(
                    event_lat, event_lon,
                    sta['latitude'], sta['longitude']
                )
                if not (min_distance_deg <= dist_deg <= max_distance_deg):
                    continue

                distance_m, az, baz = gps2dist_azimuth(
                    event_lat, event_lon,
                    sta['latitude'], sta['longitude']
                )

                sta_with_meta = dict(sta)
                sta_with_meta['distance_deg'] = round(dist_deg, 2)
                sta_with_meta['azimuth'] = round(az, 1)
                sta_with_meta['back_azimuth'] = round(baz, 1)
                filtered.append(sta_with_meta)
            except Exception as exc:
                self.logger.debug(
                    f"Could not compute distance/azimuth for station {sta.get('network','')}.{sta.get('station','')}: {exc}"
                )

        return filtered
    
    def _query_provider(
        self,
        provider: str,
        roi_bbox: Tuple[float, float, float, float],
        networks: str,
        stations: str,
        channels: str,
        start_time: Optional[str],
        end_time: Optional[str],
        include_closed: bool,
        max_retries: int = 3
    ) -> List[dict]:
        """
        Query a single FDSN provider with retry logic.
        
        Args:
            provider: Provider name
            roi_bbox: Bounding box
            networks: Network codes
            stations: Station codes
            channels: Channel codes
            start_time: Start time
            end_time: End time
            include_closed: Include closed stations
            max_retries: Maximum retry attempts
            
        Returns:
            List of station dictionaries from this provider
        """
        if provider not in self.PROVIDER_ENDPOINTS:
            self.logger.warning(f"Unknown provider: {provider}")
            return []
        
        client_name = self.PROVIDER_ENDPOINTS[provider]
        min_lon, min_lat, max_lon, max_lat = roi_bbox
        
        # Build query parameters
        params = {
            'network': networks,
            'station': stations,
            'channel': channels,
            'minlatitude': min_lat,
            'maxlatitude': max_lat,
            'minlongitude': min_lon,
            'maxlongitude': max_lon,
            'level': 'channel'  # Ensure channel selection is applied by providers
        }
        
        # Add time constraints if provided
        if start_time:
            params['starttime'] = UTCDateTime(start_time)
        if end_time:
            params['endtime'] = UTCDateTime(end_time)
        
        # Include restricted metadata to maximize search results (visibility is controlled by providers)
        params['includerestricted'] = True
        
        # Query with retries
        for attempt in range(max_retries):
            try:
                client = Client(client_name, timeout=60)
                inventory = client.get_stations(**params)
                
                # Normalize to dictionary format
                stations_list = []
                for network in inventory:
                    for station in network:
                        site_obj = getattr(station, 'site', None)
                        site_name = getattr(site_obj, 'name', '') if site_obj is not None else ''

                        # Collect available channel codes for this station to derive channel types (e.g., BH, HH, EH)
                        channel_codes = set()
                        try:
                            for cha in station.channels:
                                if hasattr(cha, 'code') and cha.code:
                                    channel_codes.add(cha.code)
                        except Exception:
                            pass
                        channel_types = sorted({code[:2] for code in channel_codes if isinstance(code, str) and len(code) >= 2})

                        station_dict = {
                            'network': network.code,
                            'station': station.code,
                            'latitude': station.latitude,
                            'longitude': station.longitude,
                            'elevation': getattr(station, 'elevation', None),
                            'start_date': str(station.start_date) if station.start_date else None,
                            'end_date': str(station.end_date) if station.end_date else None,
                            'site_name': site_name,
                            'provider': provider,
                            'channels': sorted(channel_codes),
                            'channel_types': channel_types,
                        }
                        stations_list.append(station_dict)
                
                return stations_list
                
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff
                    self.logger.debug(f"Retry {attempt + 1}/{max_retries} for {provider} after {wait_time}s: {e}")
                    time.sleep(wait_time)
                else:
                    # Final attempt failed
                    self.logger.error(f"Failed to query {provider} after {max_retries} attempts: {e}")
                    return []
        
        return []
    
    def _deduplicate_stations(self, stations: List[dict]) -> List[dict]:
        """
        Deduplicate stations by network.station, keeping first occurrence.
        
        Args:
            stations: List of station dictionaries
            
        Returns:
            Deduplicated list with provenance info
        """
        seen = {}
        deduplicated = []
        
        for station in stations:
            key = f"{station['network']}.{station['station']}"
            
            if key not in seen:
                # First occurrence - keep it
                seen[key] = station
                deduplicated.append(station)
            else:
                # Duplicate - add provider to provenance if different
                existing = seen[key]
                if station['provider'] != existing['provider']:
                    # Track multiple providers
                    if 'providers' not in existing:
                        existing['providers'] = [existing['provider']]
                    if station['provider'] not in existing['providers']:
                        existing['providers'].append(station['provider'])
        
        return deduplicated
    
    def filter_by_circle(
        self,
        stations: List[dict],
        center: Tuple[float, float],
        radius_km: float
    ) -> List[dict]:
        """
        Filter stations by circular distance from center.
        
        Useful for refining results when ROI is a circle.
        
        Args:
            stations: List of station dictionaries
            center: (lat, lon) center point
            radius_km: Radius in kilometers
            
        Returns:
            Filtered list of stations within circle
        """
        from obspy.geodetics import gps2dist_azimuth
        
        center_lat, center_lon = center
        filtered = []
        
        for station in stations:
            distance_m, _, _ = gps2dist_azimuth(
                center_lat, center_lon,
                station['latitude'], station['longitude']
            )
            distance_km = distance_m / 1000.0
            
            if distance_km <= radius_km:
                station['distance_from_center_km'] = distance_km
                filtered.append(station)
        
        return filtered
    
    def get_station_availability(
        self,
        stations: List[dict],
        start_time: str,
        end_time: str,
        channel: str = "BHZ"
    ) -> Dict[str, bool]:
        """
        Check availability of stations for a given time window.
        
        This is a lightweight check to see if stations have data.
        
        Args:
            stations: List of station dictionaries
            start_time: Start time (ISO format)
            end_time: End time (ISO format)
            channel: Channel to check
            
        Returns:
            Dictionary {net.sta: availability_bool}
        """
        availability = {}
        
        for station in stations:
            key = f"{station['network']}.{station['station']}"
            
            # Simple heuristic: check if station dates overlap with query window
            if station['start_date'] and station['end_date']:
                try:
                    sta_start = UTCDateTime(station['start_date'])
                    sta_end = UTCDateTime(station['end_date'])
                    query_start = UTCDateTime(start_time)
                    query_end = UTCDateTime(end_time)
                    
                    # Check for overlap
                    has_overlap = (sta_start <= query_end) and (sta_end >= query_start)
                    availability[key] = has_overlap
                except:
                    availability[key] = False
            elif station['start_date']:
                # Station still operating
                try:
                    sta_start = UTCDateTime(station['start_date'])
                    query_end = UTCDateTime(end_time)
                    availability[key] = sta_start <= query_end
                except:
                    availability[key] = False
            else:
                # No date info - assume available
                availability[key] = True
        
        return availability
    
    def shutdown(self):
        """Shutdown executor"""
        self.executor.shutdown(wait=True)

    def save_stationxml(
        self,
        stations: List[dict],
        output_dir: str,
        level: str = 'response',
        timeout: int = 120,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        channels: Optional[str] = None
    ) -> int:
        """Fetch and save StationXML files (Inventory) for given stations.

        Only metadata near the requested time window and for the requested
        channels (sensor families) is requested when possible. Files are
        saved as <NET>.<STA>.xml under ``output_dir``.

        Returns number of files saved.
        """
        from pathlib import Path

        Path(output_dir).mkdir(parents=True, exist_ok=True)

        # Track progress
        task_id = "stationxml_save"
        total = len({f"{s['network']}.{s['station']}" for s in stations}) if stations else 0
        if total <= 0:
            return 0
        self.progress_manager.create_task(task_id, total, "Saving StationXML metadata")

        saved = 0
        processed = 0
        seen = set()
        for s in stations:
            key = f"{s['network']}.{s['station']}"
            if key in seen:
                continue
            seen.add(key)
            try:
                provider = s.get('provider', 'IRIS')
                client_name = self.PROVIDER_ENDPOINTS.get(provider, 'IRIS')
                client = Client(client_name, timeout=timeout)

                params = {
                    'network': s['network'],
                    'station': s['station'],
                    'level': level,
                }
                if channels:
                    params['channel'] = channels
                if start_time:
                    params['starttime'] = UTCDateTime(start_time)
                if end_time:
                    params['endtime'] = UTCDateTime(end_time)

                inv = client.get_stations(**params)
                out_path = Path(output_dir) / f"{key}.xml"
                inv.write(str(out_path), format='STATIONXML')
                saved += 1
            except Exception as e:
                self.logger.warning(f"StationXML fetch failed for {key}: {e}")
            finally:
                processed += 1
                self.progress_manager.update_task(task_id, processed)

        self.progress_manager.complete_task(task_id, success=True)
        return saved
