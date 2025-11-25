"""
Standalone waveform download functionality.

This module provides functionality to download seismic waveforms from FDSN
services with bulk download support, retry logic, and progress tracking.
"""

import time
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime

try:
    from obspy import Stream, UTCDateTime
    from obspy.clients.fdsn import Client
    from obspy.taup import TauPyModel
    from tqdm import tqdm
    HAS_OBSPY = True
except ImportError:
    HAS_OBSPY = False
    Stream = None
    UTCDateTime = None
    Client = None
    tqdm = None

from utils.logging_progress import ProgressManager


class WaveformDownloader:
    """
    Download waveform data for selected events and stations.
    
    This class handles waveform downloading from FDSN web services with
    features including bulk download, retry logic, progress tracking,
    gap detection, and data validation.
    """

    # Map station 'provider' keys to FDSN client names
    FDSN_PROVIDER_ENDPOINTS = {
        "IRIS": "IRIS",
        "GEOFON": "GFZ",
        "ORFEUS": "ORFEUS",
        "RESIF": "RESIF",
        "INGV": "INGV",
        "ETHZ": "ETH",   # station metadata uses ETHZ, FDSN client uses ETH
        "ETH": "ETH",
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
    
    def __init__(self, progress_manager: ProgressManager, logger: logging.Logger):
        """
        Initialize WaveformDownloader.
        
        Args:
            progress_manager: Progress tracking manager
            logger: Logger instance
        """
        if not HAS_OBSPY:
            raise ImportError("ObsPy is required for WaveformDownloader")
        
        self.progress_manager = progress_manager
        self.logger = logger
        self.taup_model = None
        self._cancel = False
    
    def _ensure_taup_model(self, model_name='iasp91'):
        """Lazy load TauP model for arrival calculations."""
        if self.taup_model is None:
            self.taup_model = TauPyModel(model=model_name)
        return self.taup_model
    
    def cancel(self):
        """Request cancellation of an in-progress download."""
        self._cancel = True

    def reset_cancel(self):
        """Reset cancellation flag."""
        self._cancel = False

    def compute_theoretical_arrivals(
        self,
        events: List[dict],
        stations: List[dict],
        phases: List[str] = None,
        model: str = 'iasp91'
    ) -> Dict[str, Dict[str, float]]:
        """Compute theoretical phase arrival *times* for event-station pairs.

        This legacy helper returns only per-phase travel times (in seconds)
        and is used by the download code to define time windows around P.
        For richer metadata (takeoff angle, ray parameter, etc.), see
        :meth:`compute_arrival_details`.
        """
        if phases is None:
            phases = ['P', 'S']

        arrivals_dict: Dict[str, Dict[str, float]] = {}
        taup = self._ensure_taup_model(model)

        from obspy.geodetics import locations2degrees

        for event in events:
            event_id = event['event_id']
            event_lat = event['latitude']
            event_lon = event['longitude']
            event_depth = event['depth']

            for station in stations:
                net_sta = f"{station['network']}.{station['station']}"
                key = f"{event_id}-{net_sta}"

                # Calculate distance
                distance_deg = locations2degrees(
                    event_lat, event_lon,
                    station['latitude'], station['longitude']
                )

                # Get arrivals
                try:
                    arrivals = taup.get_travel_times(
                        source_depth_in_km=event_depth,
                        distance_in_degree=distance_deg,
                        phase_list=phases
                    )

                    phase_times: Dict[str, float] = {}
                    for phase in phases:
                        matching_arrivals = [a for a in arrivals if a.name == phase]
                        if matching_arrivals:
                            phase_times[phase] = float(matching_arrivals[0].time)

                    if phase_times:
                        arrivals_dict[key] = phase_times

                except Exception as e:
                    self.logger.warning(f"Could not compute arrivals for {key}: {e}")

        return arrivals_dict

    def compute_arrival_details(
        self,
        events: List[dict],
        stations: List[dict],
        phases: List[str] = None,
        model: str = 'iasp91'
    ) -> Dict[str, Dict[str, Any]]:
        """Compute rich TauP-based arrival metadata for event-station pairs.

        Returns a mapping keyed by "<event_id>-<NET.STA>" with values of the
        form::

            {
              "event_id": str,
              "network": str,
              "station": str,
              "distance_deg": float,
              "distance_km": float,
              "phases": {
                  "P": {
                      "time_s": float,
                      "takeoff_angle_deg": float | None,
                      "ray_param_sec_deg": float | None,
                  },
                  ...
              }
            }

        This is intended for downstream source-parameter analysis and JSON
        export, without affecting the existing download behavior which only
        needs arrival times.
        """
        if phases is None:
            phases = ['P', 'S']

        details: Dict[str, Dict[str, Any]] = {}
        taup = self._ensure_taup_model(model)

        from obspy.geodetics import locations2degrees, gps2dist_azimuth

        for event in events:
            event_id = event['event_id']
            event_lat = event['latitude']
            event_lon = event['longitude']
            event_depth = event['depth']

            for station in stations:
                net = station['network']
                sta = station['station']
                net_sta = f"{net}.{sta}"
                key = f"{event_id}-{net_sta}"

                try:
                    distance_deg = locations2degrees(
                        event_lat, event_lon,
                        station['latitude'], station['longitude']
                    )
                    dist_m, _, _ = gps2dist_azimuth(
                        event_lat, event_lon,
                        station['latitude'], station['longitude']
                    )
                    distance_km = float(dist_m) / 1000.0
                except Exception as e:
                    self.logger.warning(f"Could not compute distance for {key}: {e}")
                    continue

                try:
                    arrivals = taup.get_travel_times(
                        source_depth_in_km=event_depth,
                        distance_in_degree=distance_deg,
                        phase_list=phases
                    )
                except Exception as e:
                    self.logger.warning(f"Could not compute arrivals for {key}: {e}")
                    continue

                phase_info: Dict[str, Dict[str, Any]] = {}
                for phase in phases:
                    matching_arrivals = [a for a in arrivals if a.name == phase]
                    if not matching_arrivals:
                        continue
                    arr = matching_arrivals[0]
                    info: Dict[str, Any] = {}
                    try:
                        info['time_s'] = float(arr.time)
                    except Exception:
                        continue
                    # Optional fields; presence depends on TauP version/model
                    takeoff = getattr(arr, 'takeoff_angle', None)
                    if takeoff is not None:
                        try:
                            info['takeoff_angle_deg'] = float(takeoff)
                        except Exception:
                            pass
                    rp = getattr(arr, 'ray_param_sec_degree', None)
                    if rp is not None:
                        try:
                            info['ray_param_sec_deg'] = float(rp)
                        except Exception:
                            pass
                    phase_info[phase] = info

                if not phase_info:
                    continue

                details[key] = {
                    'event_id': event_id,
                    'network': net,
                    'station': sta,
                    'distance_deg': float(distance_deg),
                    'distance_km': distance_km,
                    'phases': phase_info,
                }

        return details
    
    def download_waveforms(
        self,
        events: List[dict],
        stations: List[dict],
        theoretical_arrivals: Dict[str, Dict[str, float]],
        time_before: float = 10.0,
        time_after: float = 120.0,
        channels: str = "BHZ,BHN,BHE",
        location: str = "*",
        bulk_download: bool = True,
        chunk_size: int = 50,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        provider: str = "IRIS",
        username: Optional[str] = None,
        password: Optional[str] = None,
        clean_gaps: bool = False,
        fill_value: float = 0.0,
        max_gap: float = 10.0
    ) -> Optional[Stream]:
        """
        Download waveforms for all event-station pairs.
        
        Args:
            events: List of event dictionaries
            stations: List of station dictionaries
            theoretical_arrivals: Pre-computed arrival times
            time_before: Seconds before P arrival
            time_after: Seconds after P arrival
            channels: Comma-separated channel codes
            location: Location code
            bulk_download: Use bulk download if True
            chunk_size: Events per chunk for bulk download
            max_retries: Maximum retry attempts
            retry_delay: Seconds between retries
            provider: FDSN provider name
            
        Returns:
            ObsPy Stream with downloaded waveforms, or None on failure
        """
        try:
            task_id = "waveform_download"
            self.progress_manager.create_task(task_id, len(events) * len(stations), "Downloading waveforms")
            
            self.logger.info(f"Downloading waveforms for {len(events)} events and {len(stations)} stations...")
            
            # Parse channel codes
            channel_list = [ch.strip() for ch in channels.split(',')]
            
            # Initialize FDSN client kwargs (reused across providers)
            client_kwargs = {}
            if username:
                client_kwargs['user'] = username
            if password:
                client_kwargs['password'] = password

            # Helper to get or create a client per provider key
            client_cache: Dict[str, Client] = {}

            def get_client_for_provider(provider_key: Optional[str]) -> Client:
                """Resolve provider key from stations or UI into an ObsPy Client instance."""
                key = provider_key or provider
                # Map station provider (e.g. ETHZ) to FDSN endpoint
                client_name = self.FDSN_PROVIDER_ENDPOINTS.get(key, provider)
                if client_name not in client_cache:
                    client_cache[client_name] = Client(client_name, **client_kwargs)
                return client_cache[client_name]
            
            # Initialize stream to collect all waveforms
            all_streams = Stream()
            
            if self._cancel:
                self.logger.info("Download cancelled before start.")
                self.progress_manager.complete_task(task_id, success=False, error_message="cancelled")
                return all_streams

            if bulk_download:
                # Group stations by provider so we can use multiple FDSN endpoints in one run
                stations_by_provider: Dict[Optional[str], List[dict]] = {}
                for sta in stations:
                    prov_key = sta.get('provider')
                    stations_by_provider.setdefault(prov_key, []).append(sta)

                for prov_key, prov_stations in stations_by_provider.items():
                    client_for_group = get_client_for_provider(prov_key)
                    # Build bulk request list for this provider's stations
                    bulk_list, bulk_event_ids = self._build_bulk_request(
                        events, prov_stations, theoretical_arrivals,
                        time_before, time_after, channel_list, location
                    )
                    if not bulk_list:
                        continue

                    # Download in chunks with progress bar
                    n_chunks = (len(bulk_list) + chunk_size - 1) // chunk_size

                    for i in range(n_chunks):
                        start_idx = i * chunk_size
                        end_idx = min((i + 1) * chunk_size, len(bulk_list))
                        chunk = bulk_list[start_idx:end_idx]

                        if self._cancel:
                            self.logger.info("Download cancelled by user.")
                            self.progress_manager.complete_task(task_id, success=False, error_message="cancelled")
                            return all_streams
                        # Try downloading chunk with retries
                        for attempt in range(max_retries):
                            try:
                                st = client_for_group.get_waveforms_bulk(chunk)
                                # Annotate event_id per returned trace (best-effort by order)
                                try:
                                    for j, tr in enumerate(st):
                                        idx = start_idx + j
                                        if idx < len(bulk_event_ids):
                                            tr.stats.event_id = bulk_event_ids[idx]
                                except Exception:
                                    pass
                                all_streams += st
                                self.progress_manager.update_task(task_id, end_idx)
                                break
                            except Exception as e:
                                if attempt < max_retries - 1:
                                    self.logger.warning(
                                        f"Chunk {i+1}/{n_chunks} for provider {prov_key or provider} "
                                        f"failed (attempt {attempt+1}): {str(e)}. Retrying..."
                                    )
                                    time.sleep(retry_delay)
                                else:
                                    self.logger.error(
                                        f"Chunk {i+1}/{n_chunks} for provider {prov_key or provider} "
                                        f"failed after {max_retries} attempts: {str(e)}"
                                    )
            
            else:
                # Individual downloads
                processed = 0
                for event in events:
                    if self._cancel:
                        self.logger.info("Download cancelled by user.")
                        self.progress_manager.complete_task(task_id, success=False, error_message="cancelled")
                        return all_streams
                    event_id = event['event_id']
                    event_time = UTCDateTime(event['time'])
                    
                    for station in stations:
                        net_sta = f"{station['network']}.{station['station']}"
                        key = f"{event_id}-{net_sta}"
                        
                        # Get theoretical P arrival if available
                        p_arrival = theoretical_arrivals.get(key, {}).get('P', 0)
                        
                        # Calculate time window
                        starttime = event_time + p_arrival - time_before
                        endtime = event_time + p_arrival + time_after
                        
                        # Resolve channel list for this station using its available channel types
                        per_station_channels = self._resolve_station_channels(channel_list, station)
                        # Resolve provider for this station (fall back to UI provider)
                        sta_provider_key = station.get('provider')
                        client_for_station = get_client_for_provider(sta_provider_key)
                        for channel in per_station_channels:
                            # Try downloading with retries
                            for attempt in range(max_retries):
                                try:
                                    st = client_for_station.get_waveforms(
                                        network=station['network'],
                                        station=station['station'],
                                        location=location,
                                        channel=channel,
                                        starttime=starttime,
                                        endtime=endtime
                                    )
                                    # Annotate event_id on returned traces
                                    try:
                                        for tr in st:
                                            tr.stats.event_id = event_id
                                    except Exception:
                                        pass
                                    all_streams += st
                                    break
                                except Exception as e:
                                    if attempt < max_retries - 1:
                                        time.sleep(retry_delay)
                                    else:
                                        self.logger.debug(f"Failed to download {net_sta}.{channel}: {str(e)}")
                        
                        processed += 1
                        self.progress_manager.update_task(task_id, processed)
            
            # Optionally clean gaps
            if clean_gaps and len(all_streams) > 0:
                try:
                    all_streams = self.merge_and_cleanup(all_streams, fill_value=fill_value, max_gap=max_gap)
                except Exception as e:
                    self.logger.warning(f"Cleanup failed: {e}")

            self.progress_manager.complete_task(task_id, success=True)
            self.logger.info(f"Downloaded {len(all_streams)} traces")
            return all_streams
            
        except Exception as e:
            self.logger.error(f"Failed to download waveforms: {str(e)}")
            self.progress_manager.complete_task(task_id, success=False, error_message=str(e))
            return None
    
    def _build_bulk_request(
        self,
        events: List[dict],
        stations: List[dict],
        theoretical_arrivals: Dict,
        time_before: float,
        time_after: float,
        channels: List[str],
        location: str
) -> Tuple[List[tuple], List[str]]:
        """
        Build bulk request list for efficient waveform downloading.
        
        Returns:
            List of tuples for bulk request (net, sta, loc, cha, start, end)
        """
        bulk_list = []
        bulk_event_ids: List[str] = []
        
        for event in events:
            event_id = event['event_id']
            event_time = UTCDateTime(event['time'])
            
            for station in stations:
                net_sta = f"{station['network']}.{station['station']}"
                key = f"{event_id}-{net_sta}"
                
                # Get theoretical P arrival if available
                p_arrival = theoretical_arrivals.get(key, {}).get('P', 0)
                
                # Calculate time window
                starttime = event_time + p_arrival - time_before
                endtime = event_time + p_arrival + time_after
                
                # Use station-specific channel restriction
                per_station_channels = self._resolve_station_channels(channels, station)
                for channel in per_station_channels:
                    bulk_list.append((
                        station['network'],
                        station['station'],
                        location,
                        channel,
                        starttime,
                        endtime
                    ))
                    bulk_event_ids.append(event_id)
        
        return bulk_list, bulk_event_ids
    
    def _resolve_station_channels(self, channels: List[str], station: dict) -> List[str]:
        """
        Restrict requested channels to the station's available channel type(s) (e.g., BH/HH/EH).
        Expands patterns like 'BH?' to BHZ,BHN,BHE and filters by station['channel_types'].
        """
        # Determine station available prefixes
        station_prefixes = set(station.get('channel_types') or [])
        if not station_prefixes:
            # Fallback: no info; return original list expanded
            return self._expand_channel_patterns(channels)

        # Determine requested prefixes
        requested_prefixes = set()
        for ch in channels:
            ch = ch.strip()
            if len(ch) >= 2:
                requested_prefixes.add(ch[:2])

        # Choose intersection if any, else keep requested
        chosen_prefixes = station_prefixes.intersection(requested_prefixes) or requested_prefixes

        # Expand patterns and filter by chosen prefixes
        expanded = self._expand_channel_patterns(channels)
        filtered = [c for c in expanded if len(c) >= 2 and c[:2] in chosen_prefixes]
        # Ensure unique and stable order
        seen = set()
        result = []
        for c in filtered:
            if c not in seen:
                seen.add(c)
                result.append(c)
        return result or expanded

    def _expand_channel_patterns(self, channels: List[str]) -> List[str]:
        """Expand patterns like 'BH?' into BHZ,BHN,BHE; leave explicit codes as-is."""
        expanded = []
        for ch in channels:
            ch = ch.strip()
            if ch.endswith('?') and len(ch) == 3:
                prefix = ch[:2]
                expanded.extend([prefix + 'Z', prefix + 'N', prefix + 'E'])
            else:
                expanded.append(ch)
        return expanded

    def save_waveforms(
        self,
        stream: Stream,
        output_dir: str,
        save_format: str = 'SAC'
    ) -> bool:
        """
        Save waveforms to disk with standardized naming.
        
        Args:
            stream: ObsPy Stream to save
            output_dir: Output directory
            save_format: Format to save ('SAC' or 'MSEED')
            
        Returns:
            True if save successful
        """
        try:
            output_path = Path(output_dir)
            # Save under 'waveforms' subdirectory for organization
            output_path = output_path / 'waveforms'
            output_path.mkdir(parents=True, exist_ok=True)
            
            # Group traces by event_id if available, else fallback to starttime
            event_groups = {}
            for tr in stream:
                ev_id = getattr(tr.stats, 'event_id', None)
                key = ev_id if ev_id else tr.stats.starttime.strftime("%Y%m%d_%H%M%S")
                event_groups.setdefault(key, []).append(tr)
            
            # Save each trace
            saved_count = 0
            for key, traces in event_groups.items():
                # Directory per event id (or time fallback)
                safe_key = re.sub(r'[<>:"/\\|?*]+', '_', str(key))
                event_dir = output_path / safe_key
                event_dir.mkdir(exist_ok=True)
                
                for tr in traces:
                    # Generate filename
                    filename = f"{tr.stats.network}.{tr.stats.station}.{tr.stats.location}.{tr.stats.channel}"
                    
                    if save_format.upper() == 'SAC':
                        filepath = event_dir / f"{filename}.sac"
                        tr.write(str(filepath), format='SAC')
                    elif save_format.upper() == 'MSEED':
                        filepath = event_dir / f"{filename}.mseed"
                        tr.write(str(filepath), format='MSEED')
                    else:
                        self.logger.error(f"Unknown format: {save_format}")
                        return False
                    
                    saved_count += 1
            
            self.logger.info(f"Saved {saved_count} traces to {output_dir}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to save waveforms: {str(e)}")
            return False
    
    def merge_and_cleanup(
        self,
        stream: Stream,
        fill_value: float = 0.0,
        max_gap: float = 10.0
    ) -> Stream:
        """
        Merge traces and clean up gaps/overlaps.
        
        Args:
            stream: ObsPy Stream to clean
            fill_value: Value to fill gaps
            max_gap: Maximum gap length in seconds to keep trace
            
        Returns:
            Cleaned Stream
        """
        try:
            self.logger.info(f"Cleaning {len(stream)} traces...")
            
            # Create a copy to avoid modifying original
            cleaned_stream = stream.copy()
            
            # Merge traces with same ID
            cleaned_stream.merge(method=1, fill_value=fill_value, interpolation_samples=0)
            
            # Remove traces with excessive gaps if needed
            if max_gap is not None:
                traces_to_remove = []
                
                for tr in cleaned_stream:
                    # Check if trace has masked array (indicating gaps)
                    if hasattr(tr.data, 'mask'):
                        # Count gap length
                        gap_samples = tr.data.mask.sum()
                        gap_duration = gap_samples / tr.stats.sampling_rate
                        
                        if gap_duration > max_gap:
                            traces_to_remove.append(tr)
                            self.logger.warning(
                                f"Removing trace {tr.id} with {gap_duration:.1f}s gap"
                            )
                
                # Remove traces
                for tr in traces_to_remove:
                    cleaned_stream.remove(tr)
            
            self.logger.info(f"Cleaned stream has {len(cleaned_stream)} traces")
            return cleaned_stream
            
        except Exception as e:
            self.logger.error(f"Failed to clean stream: {str(e)}")
            return stream
