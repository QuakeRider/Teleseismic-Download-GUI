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
        """
        try:
            fm = None
            try:
                fm = event.preferred_focal_mechanism()
            except Exception:
                fm = None
            if fm is None and getattr(event, "focal_mechanisms", None):
                fm = event.focal_mechanisms[0]
            if fm is None:
                return None

            info: Dict[str, object] = {}

            mt = getattr(fm, "moment_tensor", None)
            if mt is not None:
                tensor = getattr(mt, "tensor", None)
                tensor_dict: Dict[str, float] = {}
                if tensor is not None:
                    for comp in ("m_rr", "m_tt", "m_pp", "m_rt", "m_rp", "m_tp"):
                        val = getattr(tensor, comp, None)
                        if val is not None:
                            tensor_dict[comp] = float(val)
                if tensor_dict:
                    info["tensor"] = tensor_dict
                scalar_moment = getattr(mt, "scalar_moment", None)
                if scalar_moment is not None:
                    info["scalar_moment"] = float(scalar_moment)

            nodal_planes = []
            np_obj = getattr(fm, "nodal_planes", None)
            if np_obj is not None:
                for plane_name in ("nodal_plane_1", "nodal_plane_2"):
                    plane = getattr(np_obj, plane_name, None)
                    if plane is not None:
                        nodal_planes.append({
                            "name": plane_name,
                            "strike": float(getattr(plane, "strike", 0.0)) if getattr(plane, "strike", None) is not None else None,
                            "dip": float(getattr(plane, "dip", 0.0)) if getattr(plane, "dip", None) is not None else None,
                            "rake": float(getattr(plane, "rake", 0.0)) if getattr(plane, "rake", None) is not None else None,
                        })
            if nodal_planes:
                info["nodal_planes"] = nodal_planes

            if not info:
                return None
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
            
            # Query catalog
            catalog = client.get_events(
                starttime=starttime,
                endtime=endtime,
                minmagnitude=min_magnitude,
                maxmagnitude=max_magnitude,
                mindepth=min_depth * 1000,  # Convert to meters
                maxdepth=max_depth * 1000
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
                    event_dict = {
                        'event_id': str(event.resource_id).split('/')[-1],
                        'time': origin.time.datetime.isoformat(),
                        'latitude': origin.latitude,
                        'longitude': origin.longitude,
                        'depth': origin.depth / 1000.0,  # Convert to km
                        'magnitude': magnitude.mag,
                        'magnitude_type': magnitude.magnitude_type,
                        'distance_deg': round(distance_deg, 2),
                        'catalog_source': catalog_source
                    }

                    # Attach moment tensor / focal mechanism info if available
                    mt_info = self._extract_moment_tensor(event)
                    if mt_info is not None:
                        event_dict['moment_tensor'] = mt_info

                    events_with_distance.append(event_dict)
                
                # Update progress
                if i % 10 == 0:
                    progress = 50 + int((i / len(catalog)) * 50)
                    self.progress_manager.update_task(task_id, progress)
            
            self.progress_manager.complete_task(task_id, success=True)
            self.logger.info(f"Filtered to {len(events_with_distance)} events in distance range")
            
            return events_with_distance
            
        except Exception as e:
            self.logger.error(f"Event search failed: {e}")
            self.progress_manager.complete_task(task_id, success=False, error_message=str(e))
            return []
    
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
