"""
Event search service with catalog queries and magnitude-depth filtering.

This service handles querying earthquake catalogs, computing distances
from study area center, and applying the dynamic magnitude-depth cutoff filter.
"""

import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime

try:
    from obspy import UTCDateTime
    from obspy.clients.fdsn import Client
    from obspy.geodetics import locations2degrees
    import numpy as np
    HAS_OBSPY = True
except ImportError:
    HAS_OBSPY = False
    np = None

from utils.logging_progress import ProgressManager


class MagnitudeDepthFilter:
    """
    Dynamic magnitude-depth cutoff filter for event selection.
    
    Implements the formula:
    evmagmin = 5.2 + (6.0 - 5.0) * (dist - 30.0) / (180.0 - 30.0) - evdep / 700.0
    
    Benchmark values:
    - dist=30°, depth=700km → mag=4.2
    - dist=30°, depth=0km → mag=5.2
    - dist=105°, depth=0km → mag=5.7
    - dist=180°, depth=0km → mag=6.2
    """
    
    @staticmethod
    def compute_cutoff(distance_deg: float, depth_km: float) -> float:
        """
        Compute minimum magnitude cutoff based on distance and depth.
        
        Args:
            distance_deg: Epicentral distance in degrees
            depth_km: Event depth in kilometers
            
        Returns:
            Minimum magnitude threshold
        """
        cutoff = 5.2 + (6.0 - 5.0) * (distance_deg - 30.0) / (180.0 - 30.0) - depth_km / 700.0
        return cutoff
    
    @staticmethod
    def apply_filter(
        events: List[dict],
        enabled: bool = True
    ) -> Tuple[List[dict], List[dict]]:
        """
        Apply dynamic filter to events.
        
        Args:
            events: List of event dictionaries (must have 'distance_deg', 'depth', 'magnitude')
            enabled: Whether filter is enabled
            
        Returns:
            Tuple of (passing_events, filtered_out_events) with 'dynamic_cutoff' field added
        """
        if not enabled:
            return events, []
        
        passing = []
        filtered_out = []
        
        for event in events:
            cutoff = MagnitudeDepthFilter.compute_cutoff(
                event['distance_deg'],
                event['depth']
            )
            
            # Add cutoff to event
            event_with_cutoff = {**event, 'dynamic_cutoff': round(cutoff, 2)}
            
            if event['magnitude'] >= cutoff:
                passing.append(event_with_cutoff)
            else:
                filtered_out.append(event_with_cutoff)
        
        return passing, filtered_out
    
    @staticmethod
    def generate_preview_data(
        distance_range: Tuple[float, float],
        depths: List[float] = None
    ) -> Dict[str, any]:
        """
        Generate preview data for plotting cutoff vs. distance.
        
        Args:
            distance_range: (min_distance, max_distance) in degrees
            depths: List of depths to plot (default: [0, 100, 300, 700] km)
            
        Returns:
            Dictionary with 'distances' and 'cutoffs_by_depth'
        """
        if depths is None:
            depths = [0, 100, 300, 700]
        
        if np is None:
            raise ImportError("NumPy is required for preview generation")
        
        distances = np.linspace(distance_range[0], distance_range[1], 100)
        cutoffs_by_depth = {}
        
        for depth in depths:
            cutoffs = [MagnitudeDepthFilter.compute_cutoff(d, depth) for d in distances]
            cutoffs_by_depth[depth] = cutoffs
        
        return {
            'distances': distances.tolist(),
            'cutoffs_by_depth': cutoffs_by_depth
        }


class EventService:
    """
    Event catalog search with distance filtering and magnitude-depth cutoff.
    
    Queries FDSN catalogs for events within specified parameters and
    computes distances from study area center.
    """
    
    # Catalog sources (from PROGRAM_MAPPING.md)
    CATALOG_SOURCES = {
        "IRIS": "IRIS",
        "USGS": "USGS",
        "ISC": "ISC"
    }
    
    def __init__(
        self,
        progress_manager: ProgressManager,
        logger: logging.Logger
    ):
        """
        Initialize event service.
        
        Args:
            progress_manager: Progress tracking manager
            logger: Logger instance
        """
        if not HAS_OBSPY:
            raise ImportError("ObsPy is required for EventService")
        
        self.progress_manager = progress_manager
        self.logger = logger
    
    def _extract_moment_tensor(self, event) -> Optional[Dict]:
        """Extract moment tensor and focal mechanism info from an ObsPy Event, if available.

        The returned dictionary is JSON-serializable and may contain:
        - tensor: dict with m_rr, m_tt, m_pp, m_rt, m_rp, m_tp (if present)
        - scalar_moment: scalar moment value (if present)
        - nodal_planes: list of nodal plane dictionaries with strike/dip/rake
        - source_agency / source_author: provenance from creation_info if available

        This helper is intentionally defensive so that partial or slightly
        malformed catalog metadata (e.g. some GCMT solutions) do not cause the
        entire event to be dropped.
        """
        try:
            fm = None
            try:
                fm = event.preferred_focal_mechanism()
            except Exception:
                fm = None
            if fm is None and getattr(event, "focal_mechanisms", None):
                # Fall back to first focal mechanism if no "preferred" is marked
                try:
                    fm = event.focal_mechanisms[0]
                except Exception:
                    fm = None
            if fm is None:
                return None

            info: Dict[str, object] = {}

            # Provenance from creation_info if present (GCMT/USGS/etc.)
            try:
                ci = getattr(fm, "creation_info", None)
                if ci is not None:
                    agency_id = getattr(ci, "agency_id", None)
                    author = getattr(ci, "author", None)
                    if agency_id:
                        info["source_agency"] = str(agency_id)
                    if author:
                        info["source_author"] = str(author)
            except Exception:
                # Best-effort only; do not fail event on provenance
                pass

            mt = getattr(fm, "moment_tensor", None)
            if mt is not None:
                # Moment-tensor-specific provenance can override focal-mechanism level
                try:
                    mt_ci = getattr(mt, "creation_info", None)
                    if mt_ci is not None:
                        agency_id = getattr(mt_ci, "agency_id", None)
                        author = getattr(mt_ci, "author", None)
                        if agency_id:
                            info["source_agency"] = str(agency_id)
                        if author:
                            info["source_author"] = str(author)
                except Exception:
                    pass

                tensor = getattr(mt, "tensor", None)
                tensor_dict: Dict[str, float] = {}
                if tensor is not None:
                    for comp in ("m_rr", "m_tt", "m_pp", "m_rt", "m_rp", "m_tp"):
                        try:
                            val = getattr(tensor, comp, None)
                        except Exception:
                            val = None
                        if val is not None:
                            try:
                                tensor_dict[comp] = float(val)
                            except Exception:
                                continue
                if tensor_dict:
                    info["tensor"] = tensor_dict
                try:
                    scalar_moment = getattr(mt, "scalar_moment", None)
                    if scalar_moment is not None:
                        info["scalar_moment"] = float(scalar_moment)
                except Exception:
                    pass

            # Nodal planes (strike/dip/rake)
            nodal_planes = []
            np_obj = getattr(fm, "nodal_planes", None)
            if np_obj is not None:
                for plane_name in ("nodal_plane_1", "nodal_plane_2"):
                    try:
                        plane = getattr(np_obj, plane_name, None)
                    except Exception:
                        plane = None
                    if plane is not None:
                        try:
                            strike = getattr(plane, "strike", None)
                            dip = getattr(plane, "dip", None)
                            rake = getattr(plane, "rake", None)
                            nodal_planes.append({
                                "name": plane_name,
                                "strike": float(strike) if strike is not None else None,
                                "dip": float(dip) if dip is not None else None,
                                "rake": float(rake) if rake is not None else None,
                            })
                        except Exception:
                            continue
            if nodal_planes:
                info["nodal_planes"] = nodal_planes

            if not info:
                return None
            # Explicit marker for callers
            info["has_moment_tensor"] = True

            # Log successful extraction at INFO level for visibility
            try:
                ev_id = str(getattr(event, "resource_id", "")).split('/')[-1]
                components = []
                if "tensor" in info:
                    components.append("tensor")
                if "nodal_planes" in info:
                    components.append(f"{len(info['nodal_planes'])} nodal planes")
                if "source_agency" in info:
                    components.append(f"agency={info['source_agency']}")
                self.logger.info(f"Found moment tensor for event {ev_id}: {', '.join(components)}")
            except Exception:
                pass

            return info
        except Exception as exc:
            try:
                ev_id = str(getattr(event, "resource_id", ""))
            except Exception:
                ev_id = "unknown"
            self.logger.debug(f"Could not extract moment tensor for event {ev_id}: {exc}")
            return None
    
    def search_events(
        self,
        catalog_source: str,
        center: Tuple[float, float],
        start_time: str,
        end_time: str,
        min_magnitude: float,
        max_magnitude: float,
        min_depth: float,
        max_depth: float,
        min_distance: float,
        max_distance: float
    ) -> List[dict]:
        """
        Search for events and filter by distance from center.
        
        Args:
            catalog_source: Catalog name (IRIS, USGS, ISC)
            center: (lat, lon) study area center
            start_time: Start time (ISO format)
            end_time: End time (ISO format)
            min_magnitude: Minimum magnitude
            max_magnitude: Maximum magnitude
            min_depth: Minimum depth in km
            max_depth: Maximum depth in km
            min_distance: Minimum epicentral distance in degrees
            max_distance: Maximum epicentral distance in degrees
            
        Returns:
            List of event dictionaries with distance information
        """
        if catalog_source not in self.CATALOG_SOURCES:
            self.logger.error(f"Unknown catalog source: {catalog_source}")
            return []
        
        task_id = "event_search"
        self.progress_manager.create_task(task_id, 100, f"Searching events from {catalog_source}")
        
        self.logger.info(f"Querying {catalog_source} for events...")
        
        try:
            # Create client
            client_name = self.CATALOG_SOURCES[catalog_source]
            client = Client(client_name, timeout=120)
            
            # Convert times
            starttime = UTCDateTime(start_time)
            endtime = UTCDateTime(end_time)
            
            self.progress_manager.update_task(task_id, 10)

            # Query catalog with parameters to include all available metadata
            # includeallmagnitudes: retrieve all magnitude estimates (Mw, mb, Ms, etc.)
            # includeallorigins: retrieve all origin estimates (for uncertainties)
            # Note: includearrivals is NOT used because USGS and some others don't support it
            catalog = client.get_events(
                starttime=starttime,
                endtime=endtime,
                minmagnitude=min_magnitude,
                maxmagnitude=max_magnitude,
                mindepth=min_depth * 1000,  # Convert to meters
                maxdepth=max_depth * 1000,
                includeallmagnitudes=True,
                includeallorigins=True
            )
            
            self.progress_manager.update_task(task_id, 50)
            self.logger.info(f"Retrieved {len(catalog)} events from {catalog_source}")
            
            # Compute distances and filter
            events_with_distance = []
            center_lat, center_lon = center
            
            for i, event in enumerate(catalog):
                origin = event.preferred_origin() or event.origins[0]
                magnitude = event.preferred_magnitude() or event.magnitudes[0]

                # Compute epicentral distance
                distance_deg = locations2degrees(
                    center_lat, center_lon,
                    origin.latitude, origin.longitude
                )

                # Filter by distance
                if min_distance <= distance_deg <= max_distance:
                    # Base origin/magnitude information
                    event_dict: Dict[str, object] = {
                        'event_id': str(event.resource_id).split('/')[-1],
                        'time': origin.time.datetime.isoformat(),
                        'latitude': origin.latitude,
                        'longitude': origin.longitude,
                        'depth': origin.depth / 1000.0,  # Convert to km
                        'magnitude': magnitude.mag,
                        'magnitude_type': magnitude.magnitude_type,
                        'distance_deg': round(distance_deg, 2),
                        'catalog_source': catalog_source,
                    }

                    # Origin uncertainties (best-effort; all fields optional)
                    try:
                        # ObsPy Origin may expose *_errors attributes with .uncertainty
                        time_errors = getattr(origin, 'time_errors', None)
                        if time_errors is not None:
                            u = getattr(time_errors, 'uncertainty', None)
                            if u is not None:
                                event_dict['origin_time_uncertainty_s'] = float(u)
                        lat_errors = getattr(origin, 'latitude_errors', None)
                        if lat_errors is not None:
                            u = getattr(lat_errors, 'uncertainty', None)
                            if u is not None:
                                event_dict['latitude_uncertainty_deg'] = float(u)
                        lon_errors = getattr(origin, 'longitude_errors', None)
                        if lon_errors is not None:
                            u = getattr(lon_errors, 'uncertainty', None)
                            if u is not None:
                                event_dict['longitude_uncertainty_deg'] = float(u)
                        depth_errors = getattr(origin, 'depth_errors', None)
                        if depth_errors is not None:
                            u = getattr(depth_errors, 'uncertainty', None)
                            if u is not None:
                                # depth is in meters, convert to km
                                event_dict['depth_uncertainty_km'] = float(u) / 1000.0
                    except Exception:
                        # Uncertainties are optional; ignore problems here
                        pass

                    # Additional magnitudes (Mw, mb, Ms) if available
                    try:
                        mw_mag = None; mb_mag = None; ms_mag = None
                        for mag in getattr(event, 'magnitudes', []) or []:
                            mtype = getattr(mag, 'magnitude_type', None)
                            key = (mtype or '').upper()
                            if key == 'MW' and mw_mag is None:
                                mw_mag = mag
                            elif key == 'MB' and mb_mag is None:
                                mb_mag = mag
                            elif key in ('MS', 'MS_BB') and ms_mag is None:
                                ms_mag = mag
                        def _store_mag(prefix: str, mag_obj) -> None:
                            if mag_obj is None:
                                return
                            try:
                                event_dict[f'{prefix}'] = float(mag_obj.mag)
                            except Exception:
                                pass
                            mtype = getattr(mag_obj, 'magnitude_type', None)
                            if mtype:
                                event_dict[f'{prefix}_type'] = str(mtype)
                            ci = getattr(mag_obj, 'creation_info', None)
                            if ci is not None:
                                author = getattr(ci, 'author', None)
                                if author:
                                    event_dict[f'{prefix}_author'] = str(author)
                        _store_mag('mw', mw_mag)
                        _store_mag('mb', mb_mag)
                        _store_mag('ms', ms_mag)
                    except Exception:
                        # Magnitudes list may be missing or oddly structured; ignore errors
                        pass

                    # Attach moment tensor / focal mechanism info if available
                    mt_info = self._extract_moment_tensor(event)
                    if mt_info is not None:
                        event_dict['moment_tensor'] = mt_info
                        event_dict['has_moment_tensor'] = True
                    else:
                        event_dict['has_moment_tensor'] = False

                    events_with_distance.append(event_dict)
                
                # Update progress
                if i % 10 == 0:
                    progress = 50 + int((i / len(catalog)) * 50)
                    self.progress_manager.update_task(task_id, progress)
            
            self.progress_manager.complete_task(task_id, success=True)

            # Log summary statistics
            events_with_mt = sum(1 for e in events_with_distance if e.get('has_moment_tensor', False))
            self.logger.info(f"Filtered to {len(events_with_distance)} events in distance range")
            self.logger.info(f"Events with moment tensor information: {events_with_mt}/{len(events_with_distance)}")
            if events_with_mt == 0 and len(events_with_distance) > 0:
                self.logger.warning("No moment tensors found. Try larger magnitude events (M > 5.5) or a different catalog.")

            return events_with_distance
            
        except Exception as e:
            self.logger.error(f"Event search failed: {e}")
            self.progress_manager.complete_task(task_id, success=False, error_message=str(e))
            return []
    
    def get_event_details(
        self,
        catalog_source: str,
        event_id: str,
        event_time: str,
        time_window_seconds: float = 60.0
    ) -> Optional[dict]:
        """
        Retrieve detailed information for a specific event, including moment tensor.

        This method queries the catalog for a specific event using its ID and time,
        requesting all available metadata including focal mechanisms and moment tensors.
        Use this after confirming an event to get complete moment tensor information.

        Args:
            catalog_source: Catalog name (IRIS, USGS, ISC)
            event_id: Event resource ID
            event_time: Event origin time (ISO format)
            time_window_seconds: Time window around event (default: 60 seconds)

        Returns:
            Detailed event dictionary with moment tensor, or None if not found
        """
        if catalog_source not in self.CATALOG_SOURCES:
            self.logger.error(f"Unknown catalog source: {catalog_source}")
            return None

        task_id = "event_detail"
        self.progress_manager.create_task(task_id, 100, f"Retrieving event details from {catalog_source}")

        try:
            # Create client
            client_name = self.CATALOG_SOURCES[catalog_source]
            client = Client(client_name, timeout=120)

            # Parse event time and create search window
            event_utc = UTCDateTime(event_time)
            starttime = event_utc - time_window_seconds
            endtime = event_utc + time_window_seconds

            self.progress_manager.update_task(task_id, 30)

            # Query for the specific event using eventid if possible, otherwise time window
            try:
                # Try using eventid parameter (works for some services)
                self.logger.info(f"Querying {catalog_source} for event ID: {event_id}")
                catalog = client.get_events(
                    eventid=event_id,
                    includeallmagnitudes=True,
                    includeallorigins=True
                )
            except Exception as e:
                # Fallback: query by time window if eventid doesn't work
                self.logger.info(f"Event ID query failed, trying time window: {e}")
                catalog = client.get_events(
                    starttime=starttime,
                    endtime=endtime,
                    includeallmagnitudes=True,
                    includeallorigins=True
                )

            self.progress_manager.update_task(task_id, 70)

            if len(catalog) == 0:
                self.logger.warning(f"Event not found: {event_id}")
                self.progress_manager.complete_task(task_id, success=False, error_message="Event not found")
                return None

            # Use the first event (should be the only one if eventid worked)
            event = catalog[0]
            origin = event.preferred_origin() or event.origins[0]
            magnitude = event.preferred_magnitude() or event.magnitudes[0]

            # Build detailed event dictionary
            event_dict: Dict[str, object] = {
                'event_id': str(event.resource_id).split('/')[-1],
                'time': origin.time.datetime.isoformat(),
                'latitude': origin.latitude,
                'longitude': origin.longitude,
                'depth': origin.depth / 1000.0,  # Convert to km
                'magnitude': magnitude.mag,
                'magnitude_type': magnitude.magnitude_type,
                'catalog_source': catalog_source,
            }

            # Origin uncertainties
            try:
                time_errors = getattr(origin, 'time_errors', None)
                if time_errors is not None:
                    u = getattr(time_errors, 'uncertainty', None)
                    if u is not None:
                        event_dict['origin_time_uncertainty_s'] = float(u)
                lat_errors = getattr(origin, 'latitude_errors', None)
                if lat_errors is not None:
                    u = getattr(lat_errors, 'uncertainty', None)
                    if u is not None:
                        event_dict['latitude_uncertainty_deg'] = float(u)
                lon_errors = getattr(origin, 'longitude_errors', None)
                if lon_errors is not None:
                    u = getattr(lon_errors, 'uncertainty', None)
                    if u is not None:
                        event_dict['longitude_uncertainty_deg'] = float(u)
                depth_errors = getattr(origin, 'depth_errors', None)
                if depth_errors is not None:
                    u = getattr(depth_errors, 'uncertainty', None)
                    if u is not None:
                        event_dict['depth_uncertainty_km'] = float(u) / 1000.0
            except Exception:
                pass

            # Additional magnitudes
            try:
                mw_mag = None; mb_mag = None; ms_mag = None
                for mag in getattr(event, 'magnitudes', []) or []:
                    mtype = getattr(mag, 'magnitude_type', None)
                    key = (mtype or '').upper()
                    if key == 'MW' and mw_mag is None:
                        mw_mag = mag
                    elif key == 'MB' and mb_mag is None:
                        mb_mag = mag
                    elif key in ('MS', 'MS_BB') and ms_mag is None:
                        ms_mag = mag
                def _store_mag(prefix: str, mag_obj) -> None:
                    if mag_obj is None:
                        return
                    try:
                        event_dict[f'{prefix}'] = float(mag_obj.mag)
                    except Exception:
                        pass
                    mtype = getattr(mag_obj, 'magnitude_type', None)
                    if mtype:
                        event_dict[f'{prefix}_type'] = str(mtype)
                    ci = getattr(mag_obj, 'creation_info', None)
                    if ci is not None:
                        author = getattr(ci, 'author', None)
                        if author:
                            event_dict[f'{prefix}_author'] = str(author)
                _store_mag('mw', mw_mag)
                _store_mag('mb', mb_mag)
                _store_mag('ms', ms_mag)
            except Exception:
                pass

            # Extract moment tensor
            mt_info = self._extract_moment_tensor(event)
            if mt_info is not None:
                event_dict['moment_tensor'] = mt_info
                event_dict['has_moment_tensor'] = True
            else:
                event_dict['has_moment_tensor'] = False

            self.progress_manager.complete_task(task_id, success=True)
            self.logger.info(f"Retrieved detailed event information for {event_id}")

            return event_dict

        except Exception as e:
            self.logger.error(f"Failed to retrieve event details: {e}")
            self.progress_manager.complete_task(task_id, success=False, error_message=str(e))
            return None

    def compute_event_distances(
        self,
        events: List[dict],
        center: Tuple[float, float]
    ) -> List[dict]:
        """
        Compute distances from center for existing events.
        
        Useful when center changes or for recomputing distances.
        
        Args:
            events: List of event dictionaries with 'latitude', 'longitude'
            center: (lat, lon) center point
            
        Returns:
            Events with updated 'distance_deg' field
        """
        center_lat, center_lon = center
        
        for event in events:
            distance_deg = locations2degrees(
                center_lat, center_lon,
                event['latitude'], event['longitude']
            )
            event['distance_deg'] = round(distance_deg, 2)
        
        return events
    
    def get_distance_statistics(self, events: List[dict]) -> Dict[str, float]:
        """
        Get distance statistics for events.
        
        Args:
            events: List of events with 'distance_deg'
            
        Returns:
            Dictionary with min, max, mean, median distances
        """
        if not events:
            return {
                'min': 0, 'max': 0, 'mean': 0, 'median': 0, 'count': 0
            }
        
        if np is None:
            distances = [e['distance_deg'] for e in events]
            return {
                'min': min(distances),
                'max': max(distances),
                'mean': sum(distances) / len(distances),
                'median': sorted(distances)[len(distances) // 2],
                'count': len(distances)
            }
        
        distances = np.array([e['distance_deg'] for e in events])
        
        return {
            'min': float(np.min(distances)),
            'max': float(np.max(distances)),
            'mean': float(np.mean(distances)),
            'median': float(np.median(distances)),
            'count': len(distances)
        }
    
    def get_magnitude_statistics(self, events: List[dict]) -> Dict[str, float]:
        """
        Get magnitude statistics for events.
        
        Args:
            events: List of events with 'magnitude'
            
        Returns:
            Dictionary with min, max, mean, median magnitudes
        """
        if not events:
            return {
                'min': 0, 'max': 0, 'mean': 0, 'median': 0, 'count': 0
            }
        
        if np is None:
            mags = [e['magnitude'] for e in events]
            return {
                'min': min(mags),
                'max': max(mags),
                'mean': sum(mags) / len(mags),
                'median': sorted(mags)[len(mags) // 2],
                'count': len(mags)
            }
        
        mags = np.array([e['magnitude'] for e in events])
        
        return {
            'min': float(np.min(mags)),
            'max': float(np.max(mags)),
            'mean': float(np.mean(mags)),
            'median': float(np.median(mags)),
            'count': len(mags)
        }
    
    def sort_events(
        self,
        events: List[dict],
        sort_by: str = 'time',
        reverse: bool = False
    ) -> List[dict]:
        """
        Sort events by specified field.
        
        Args:
            events: List of events
            sort_by: Field to sort by ('time', 'magnitude', 'distance_deg', 'depth')
            reverse: Sort in reverse order
            
        Returns:
            Sorted list of events
        """
        if sort_by == 'time':
            return sorted(events, key=lambda e: e['time'], reverse=reverse)
        elif sort_by in ('magnitude', 'distance_deg', 'depth'):
            return sorted(events, key=lambda e: e[sort_by], reverse=reverse)
        else:
            self.logger.warning(f"Unknown sort field: {sort_by}")
            return events
