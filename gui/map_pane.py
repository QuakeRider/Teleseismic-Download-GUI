"""
Interactive map component using Leaflet via Folium with drawing capabilities.

This module provides a reusable MapPane widget that embeds a Leaflet map in PyQt5
using QWebEngineView. It supports ROI drawing (rectangles and circles), station/event
marker rendering, center point display, and distance rings.
"""

import json
import os
import tempfile
from typing import List, Dict, Optional, Tuple, Callable
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QUrl
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtWebChannel import QWebChannel

try:
    import folium
    from folium.plugins import Draw
    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False


class MapBridge(QObject):
    """
    Bridge for JavaScript to Python communication via QWebChannel.
    
    Signals are emitted when user interacts with the map (drawing, editing shapes).
    """
    
    # Signals emitted to Python
    shape_drawn = pyqtSignal(str)  # GeoJSON string
    shape_edited = pyqtSignal(str)  # GeoJSON string
    shape_deleted = pyqtSignal()
    
    @pyqtSlot(str)
    def onShapeDrawn(self, geojson_str: str):
        """Called from JS when user draws a shape"""
        self.shape_drawn.emit(geojson_str)
    
    @pyqtSlot(str)
    def onShapeEdited(self, geojson_str: str):
        """Called from JS when user edits a shape"""
        self.shape_edited.emit(geojson_str)
    
    @pyqtSlot()
    def onShapeDeleted(self):
        """Called from JS when user deletes a shape"""
        self.shape_deleted.emit()


class MapPane(QWidget):
    """
    Interactive map pane with drawing tools and marker rendering.
    
    Features:
    - Draw rectangle or circle ROI
    - Add/remove station markers (blue triangles)
    - Add/remove event markers (red/gray circles)
    - Display center point and distance rings
    - Export/import GeoJSON
    
    Signals:
        roi_changed: Emitted when ROI is drawn/edited/deleted (dict or None)
        center_computed: Emitted when center is computed from ROI (lat, lon)
    """
    
    roi_changed = pyqtSignal(object)  # dict or None
    center_computed = pyqtSignal(float, float)  # lat, lon
    
    def __init__(self, parent=None, add_draw_controls: bool = True):
        super().__init__(parent)
        
        if not HAS_FOLIUM:
            raise ImportError("Folium is required for MapPane. Install with: pip install folium")
        
        self.add_draw_controls = add_draw_controls
        self.current_roi: Optional[dict] = None
        self.current_center: Optional[Tuple[float, float]] = None
        self.station_markers: List[dict] = []
        self.event_markers: List[dict] = []
        
        self._setup_ui()
        self._load_initial_map()
    
    def _setup_ui(self):
        """Setup UI components"""
        layout = QVBoxLayout(self)
        
        # Toolbar (only if drawing controls are enabled)
        if self.add_draw_controls:
            toolbar = QHBoxLayout()
            self.btn_clear_roi = QPushButton("Clear ROI")
            self.btn_clear_roi.clicked.connect(self.clear_roi)
            self.btn_compute_center = QPushButton("Compute Center from ROI")
            self.btn_compute_center.clicked.connect(self._compute_and_emit_center)
            self.btn_compute_center.setEnabled(False)
            self.lbl_info = QLabel("Draw a rectangle or circle to define study area")
            toolbar.addWidget(self.btn_clear_roi)
            toolbar.addWidget(self.btn_compute_center)
            toolbar.addStretch()
            toolbar.addWidget(self.lbl_info)
            layout.addLayout(toolbar)
        
        # Web view for map
        self.web_view = QWebEngineView()
        layout.addWidget(self.web_view)
        
        # Setup web channel for JS communication
        self.bridge = MapBridge()
        self.channel = QWebChannel()
        self.channel.registerObject('qtBridge', self.bridge)
        self.web_view.page().setWebChannel(self.channel)
        
        # Connect bridge signals only if drawing
        if self.add_draw_controls:
            self.bridge.shape_drawn.connect(self._on_shape_drawn)
            self.bridge.shape_edited.connect(self._on_shape_edited)
            self.bridge.shape_deleted.connect(self._on_shape_deleted)
    
    def _load_initial_map(self):
        """Load initial empty map"""
        self.render_map(center=(0, 0), zoom=2)
    
    def render_map(
        self,
        center: Tuple[float, float] = (0, 0),
        zoom: int = 2,
        add_draw_control: bool = True
    ):
        """
        Render Folium map with Leaflet.draw plugin.
        
        Args:
            center: (lat, lon) for map center
            zoom: Initial zoom level
            add_draw_control: Whether to add drawing controls
        """
        # Create folium map
        m = folium.Map(
            location=center,
            zoom_start=zoom,
            tiles='OpenStreetMap'
        )
        
        # Add draw control if requested
        if add_draw_control and self.add_draw_controls:
            draw = Draw(
                export=False,
                draw_options={
                    'polyline': False,
                    'polygon': False,
                    'circle': True,
                    'rectangle': True,
                    'marker': False,
                    'circlemarker': False,
                }
            )
            draw.add_to(m)
        
        # Use the actual JS map variable name generated by Folium
        map_var = m.get_name()  # e.g., 'map_1234567890abcdef'
        # Remember the map variable name so runtime JS can resolve it reliably
        self._map_var = map_var

        # For maps without drawing controls, we rely on the loadFinished
        # runtime script (see _on_page_load_finished) to wire up helper
        # functions like addEventPoint/addStationTriangle/addRing.
        # The more complex __MAP__-based injection is only needed when
        # drawing tools and ROI editing are enabled.
        if not (add_draw_control and self.add_draw_controls):
            # Save to temporary HTML file without extra JS injection
            with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
                html_path = f.name
                m.save(f.name)

            self.web_view.setUrl(QUrl.fromLocalFile(html_path))
            self._current_html_path = html_path

            # Ensure our runtime JS is executed when the page finishes loading
            try:
                self.web_view.loadFinished.disconnect(self._on_page_load_finished)
            except Exception:
                pass
            self.web_view.loadFinished.connect(self._on_page_load_finished)
            return

        # Build JavaScript for QWebChannel + draw handlers; placeholder __MAP__ will be replaced
        js_code = """
        <script src=\"qrc:///qtwebchannel/qwebchannel.js\"></script>
        <script>
        (function(){
          var qtBridge = null;
          function initQWebChannel() {
            try {
              var qobj = (typeof qt !== 'undefined') ? qt : (window.qt || null);
              if (!qobj || typeof QWebChannel === 'undefined') {
                console.warn('QWebChannel setup pending...');
                return false;
              }
              new QWebChannel(qobj.webChannelTransport, function(channel) {
                qtBridge = channel.objects.qtBridge;
                console.log('QWebChannel initialized');
                if (window.lastGeoJSON) {
                  try { qtBridge.onShapeDrawn(JSON.stringify(window.lastGeoJSON)); } catch (e) { console.warn('Deferred ROI push failed', e); }
                }
              });
              return true;
            } catch (e) { console.error('QWebChannel init error:', e); return false; }
          }
          if (!initQWebChannel()) { setTimeout(initQWebChannel, 200); }

          var drawnItems = new L.FeatureGroup();
          __MAP__.addLayer(drawnItems);

          __MAP__.on('draw:created', function(e) {
            var layer = e.layer;
            drawnItems.clearLayers();
            drawnItems.addLayer(layer);
            var geojson = layer.toGeoJSON();
            window.lastGeoJSON = geojson;
            if (e.layerType === 'circle') {
              geojson.properties = geojson.properties || {};
              geojson.properties.radius = layer.getRadius();
              window.lastGeoJSON.properties = window.lastGeoJSON.properties || {};
              window.lastGeoJSON.properties.radius = layer.getRadius();
            }
            if (qtBridge) { try { qtBridge.onShapeDrawn(JSON.stringify(geojson)); } catch (err) { console.warn('onShapeDrawn failed', err); } }
          });

          __MAP__.on('draw:edited', function(e) {
            var layers = e.layers;
            layers.eachLayer(function(layer) {
              var geojson = layer.toGeoJSON();
              window.lastGeoJSON = geojson;
              if (layer instanceof L.Circle) {
                geojson.properties = geojson.properties || {};
                geojson.properties.radius = layer.getRadius();
                window.lastGeoJSON.properties = window.lastGeoJSON.properties || {};
                window.lastGeoJSON.properties.radius = layer.getRadius();
              }
              if (qtBridge) { try { qtBridge.onShapeEdited(JSON.stringify(geojson)); } catch (err) { console.warn('onShapeEdited failed', err); } }
            });
          });

          __MAP__.on('draw:deleted', function(e) {
            window.lastGeoJSON = null;
            if (qtBridge) { try { qtBridge.onShapeDeleted(); } catch (err) { console.warn('onShapeDeleted failed', err); } }
          });

          window.clearAllLayers = function() {
            drawnItems.clearLayers();
            __MAP__.eachLayer(function(layer) {
              if (layer instanceof L.Marker || layer instanceof L.Circle) {
                if (layer !== drawnItems) { __MAP__.removeLayer(layer); }
              }
            });
          };

          window.addMarker = function(lat, lon, icon, popup) {
            var opts = {};
            if (icon) { opts.icon = icon; }
            var marker = L.marker([lat, lon], opts);
            if (popup) { marker.bindPopup(popup); }
            __MAP__.addLayer(marker);
            return marker;
          };

          window.addCenterPoint = function(lat, lon) {
            return L.circleMarker([lat, lon], {radius: 5, color: '#000', fillColor: '#000', fillOpacity: 1, weight: 1}).addTo(__MAP__);
          };

          window.addEventPoint = function(lat, lon, label) {
            var m = L.circleMarker([lat, lon], {radius: 4, color: '#000', fillColor: '#d62728', fillOpacity: 0.9, weight: 1});
            if (label) { m.bindPopup(label); }
            return m.addTo(__MAP__);
          };

          window.addStationTriangle = function(lat, lon, color, label) {
            // Approximate small triangle around point
            var d = 0.05; // degrees ~ 5-6 km; simplistic
            var coslat = Math.cos(lat*Math.PI/180.0);
            var dx = d * Math.max(coslat, 0.2);
            var p1 = [lat + d, lon];
            var p2 = [lat - d, lon - dx];
            var p3 = [lat - d, lon + dx];
            var tri = L.polygon([p1, p2, p3], {color: '#000', weight: 1, fillColor: color || '#1f77b4', fillOpacity: 0.9});
            if (label) { tri.bindPopup(label); }
            return tri.addTo(__MAP__);
          };

          // Draw simple metric circle (fallback)
          window.addRing = function(lat, lon, radius_m, color, dashArray, label) {
            var circle = L.circle([lat, lon], { radius: radius_m, fillColor: 'transparent', fillOpacity: 0, color: color || '#0000ff', weight: 2, dashArray: dashArray || '5, 5' }).addTo(__MAP__);
            if (label) { circle.bindPopup(label); }
            return circle;
          };

          // Draw geodesic ring at a given angular distance (degrees) around a center
          window.addGeodesicRing = function(lat, lon, radius_deg, color, dashArray, label) {
            var R = 6371.0; // Earth radius in km
            var dist_km = radius_deg * (Math.PI/180) * R; // convert degrees to arc length km
            var pts = [];
            for (var b=0; b<360; b+=2) { // 2-degree step for smoothness
              var br = b * Math.PI/180.0;
              var lat1 = lat * Math.PI/180.0;
              var lon1 = lon * Math.PI/180.0;
              var dr = dist_km / R;
              var lat2 = Math.asin(Math.sin(lat1)*Math.cos(dr) + Math.cos(lat1)*Math.sin(dr)*Math.cos(br));
              var lon2 = lon1 + Math.atan2(Math.sin(br)*Math.sin(dr)*Math.cos(lat1), Math.cos(dr)-Math.sin(lat1)*Math.sin(lat2));
              pts.push([lat2*180/Math.PI, lon2*180/Math.PI]);
            }
            // close the ring
            pts.push(pts[0]);
            var poly = L.polyline(pts, { color: color || '#0000ff', weight: 2, dashArray: dashArray || '5, 5' }).addTo(__MAP__);
            if (label) { poly.bindPopup(label); }
            return poly;
          };

          window.setROIFromBounds = function(minLat, minLon, maxLat, maxLon) {
            try {
              drawnItems.clearLayers();
              var bounds = L.latLngBounds([minLat, minLon], [maxLat, maxLon]);
              var rect = L.rectangle(bounds, { color: '#2ca02c', weight: 1 });
              drawnItems.addLayer(rect);
              var gj = rect.toGeoJSON();
              window.lastGeoJSON = gj;
              if (qtBridge) { try { qtBridge.onShapeDrawn(JSON.stringify(gj)); } catch (e) { console.warn('ROI push failed', e); } }
            } catch (e) { console.error('setROIFromBounds error', e); }
          };

          window.setROICircle = function(lat, lon, radius_m) {
            try {
              drawnItems.clearLayers();
              var circle = L.circle([lat, lon], { radius: radius_m, color: '#2ca02c', weight: 1 });
              drawnItems.addLayer(circle);
              var gj = circle.toGeoJSON();
              gj.properties = gj.properties || {};
              gj.properties.radius = radius_m;
              window.lastGeoJSON = gj;
              if (qtBridge) { try { qtBridge.onShapeDrawn(JSON.stringify(gj)); } catch (e) { console.warn('ROI push failed', e); } }
            } catch (e) { console.error('setROICircle error', e); }
          };
        })();
        </script>
        """

        # Also prepare a runtime JS (without <script> tags) to execute after page load as a fallback
        runtime_js = """
        (function(){
          try {
            var qtBridge = null;
            function initQWebChannel() {
              try {
                var qobj = (typeof qt !== 'undefined') ? qt : (window.qt || null);
                if (!qobj || typeof QWebChannel === 'undefined') { return false; }
                new QWebChannel(qobj.webChannelTransport, function(channel) { qtBridge = channel.objects.qtBridge; });
                return true;
              } catch (e) { return false; }
            }
            if (!initQWebChannel()) { setTimeout(initQWebChannel, 200); }

            var drawnItems = new L.FeatureGroup();
            __MAP__.addLayer(drawnItems);

            __MAP__.on('draw:created', function(e){ var layer=e.layer; drawnItems.clearLayers(); drawnItems.addLayer(layer); var gj=layer.toGeoJSON(); window.lastGeoJSON=gj; if(e.layerType==='circle'){ gj.properties=gj.properties||{}; gj.properties.radius=layer.getRadius(); window.lastGeoJSON.properties=window.lastGeoJSON.properties||{}; window.lastGeoJSON.properties.radius=layer.getRadius(); } if(qtBridge){ try{ qtBridge.onShapeDrawn(JSON.stringify(gj)); }catch(err){} } });
            __MAP__.on('draw:edited', function(e){ e.layers.eachLayer(function(layer){ var gj=layer.toGeoJSON(); window.lastGeoJSON=gj; if(layer instanceof L.Circle){ gj.properties=gj.properties||{}; gj.properties.radius=layer.getRadius(); window.lastGeoJSON.properties=window.lastGeoJSON.properties||{}; window.lastGeoJSON.properties.radius=layer.getRadius(); } if(qtBridge){ try{ qtBridge.onShapeEdited(JSON.stringify(gj)); }catch(err){} } }); });
            __MAP__.on('draw:deleted', function(e){ window.lastGeoJSON=null; if(qtBridge){ try{ qtBridge.onShapeDeleted(); }catch(err){} } });

            window.clearAllLayers = function(){ drawnItems.clearLayers(); __MAP__.eachLayer(function(layer){ if(layer instanceof L.Marker || layer instanceof L.Circle){ if(layer!==drawnItems){ __MAP__.removeLayer(layer); } } }); };
            window.addMarker = function(lat,lon,icon,popup){ var opts={}; if(icon){opts.icon=icon;} var marker=L.marker([lat,lon],opts); if(popup){ marker.bindPopup(popup); } __MAP__.addLayer(marker); return marker; };
            window.addRing = function(lat,lon,radius_m,color,dashArray,label){ var circle=L.circle([lat,lon],{radius:radius_m,fillColor:'transparent',fillOpacity:0,color:color||'#0000ff',weight:2,dashArray:dashArray||'5, 5'}).addTo(__MAP__); if(label){ circle.bindPopup(label); } return circle; };
            window.setROIFromBounds = function(minLat,minLon,maxLat,maxLon){ try{ drawnItems.clearLayers(); var bounds=L.latLngBounds([minLat,minLon],[maxLat,maxLon]); var rect=L.rectangle(bounds,{color:'#2ca02c',weight:1}); drawnItems.addLayer(rect); var gj=rect.toGeoJSON(); window.lastGeoJSON=gj; if(qtBridge){ try{ qtBridge.onShapeDrawn(JSON.stringify(gj)); }catch(e){} } }catch(e){} };
            window.setROICircle = function(lat,lon,radius_m){ try{ drawnItems.clearLayers(); var circle=L.circle([lat,lon],{radius:radius_m,color:'#2ca02c',weight:1}); drawnItems.addLayer(circle); var gj=circle.toGeoJSON(); gj.properties=gj.properties||{}; gj.properties.radius=radius_m; window.lastGeoJSON=gj; if(qtBridge){ try{ qtBridge.onShapeDrawn(JSON.stringify(gj)); }catch(e){} } }catch(e){} };
          } catch(e){}
        })();
        """
        
        # Replace placeholder with actual map var to avoid f-string brace issues
        js_code = js_code.replace('__MAP__', map_var)
        runtime_js = runtime_js.replace('__MAP__', map_var)
        # Save runtime JS so we can execute after load as a fallback
        self._runtime_js = runtime_js
        self._map_var = map_var
        
        # Save to temporary HTML file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
            html_path = f.name
            m.save(f.name)
        
        # Read the generated HTML and inject our JavaScript code
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        # Inject our JavaScript code before the closing </body> tag
        html_content = html_content.replace('</body>', js_code + '\n</body>')
        
        # Write the modified HTML back
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        self.web_view.setUrl(QUrl.fromLocalFile(html_path))
        
        # Store path for cleanup
        self._current_html_path = html_path

        # Ensure our JS is executed when the page finishes loading (fallback if injection fails)
        try:
            self.web_view.loadFinished.disconnect(self._on_page_load_finished)
        except Exception:
            pass
        self.web_view.loadFinished.connect(self._on_page_load_finished)
    
    def _on_shape_drawn(self, geojson_str: str):
        """Handle shape drawn event from JavaScript"""
        try:
            geojson = json.loads(geojson_str)
            self.current_roi = geojson
            self.roi_changed.emit(geojson)
            self.btn_compute_center.setEnabled(True)
            self.lbl_info.setText(f"ROI drawn: {geojson['geometry']['type']}")
        except Exception as e:
            print(f"Error parsing drawn shape: {e}")
    
    def _on_shape_edited(self, geojson_str: str):
        """Handle shape edited event from JavaScript"""
        try:
            geojson = json.loads(geojson_str)
            self.current_roi = geojson
            self.roi_changed.emit(geojson)
            self.lbl_info.setText("ROI edited")
        except Exception as e:
            print(f"Error parsing edited shape: {e}")
    
    def _on_shape_deleted(self):
        """Handle shape deleted event from JavaScript"""
        self.current_roi = None
        self.roi_changed.emit(None)
        self.btn_compute_center.setEnabled(False)
        self.lbl_info.setText("ROI cleared")
    
    def clear_roi(self):
        """Clear current ROI from map"""
        self.web_view.page().runJavaScript("clearAllLayers();")
        self.current_roi = None
        self.roi_changed.emit(None)
        self.btn_compute_center.setEnabled(False)
        self.lbl_info.setText("ROI cleared")
    
    def _compute_and_emit_center(self):
        """Compute center from current ROI and emit signal"""
        if self.current_roi is None:
            return
        
        center = self.compute_center_from_roi(self.current_roi)
        if center:
            self.current_center = center
            self.center_computed.emit(center[0], center[1])
            self.lbl_info.setText(f"Center: {center[0]:.3f}°N, {center[1]:.3f}°E")
    
    @staticmethod
    def compute_center_from_roi(roi_geojson: dict) -> Optional[Tuple[float, float]]:
        """
        Compute center point from ROI GeoJSON.
        
        For circles: return the center point
        For rectangles: return the centroid
        
        Args:
            roi_geojson: GeoJSON dictionary
            
        Returns:
            (lat, lon) tuple or None if computation fails
        """
        try:
            geom = roi_geojson['geometry']
            geom_type = geom['type']
            
            if geom_type == 'Point':
                # Circle center
                coords = geom['coordinates']
                return (coords[1], coords[0])  # GeoJSON is [lon, lat]
            
            elif geom_type == 'Polygon':
                # Rectangle or polygon - compute centroid
                coords = geom['coordinates'][0]  # First ring
                lons = [c[0] for c in coords]
                lats = [c[1] for c in coords]
                center_lon = sum(lons) / len(lons)
                center_lat = sum(lats) / len(lats)
                return (center_lat, center_lon)
            
            else:
                print(f"Unsupported geometry type for center computation: {geom_type}")
                return None
                
        except Exception as e:
            print(f"Error computing center from ROI: {e}")
            return None
    
    @staticmethod
    def extract_bbox_from_roi(roi_geojson: dict) -> Optional[Tuple[float, float, float, float]]:
        """
        Extract bounding box from ROI GeoJSON.
        
        Args:
            roi_geojson: GeoJSON dictionary
            
        Returns:
            (min_lon, min_lat, max_lon, max_lat) or None
        """
        try:
            geom = roi_geojson['geometry']
            geom_type = geom['type']
            
            if geom_type == 'Point':
                # Circle - use radius to compute bbox
                coords = geom['coordinates']
                lon, lat = coords[0], coords[1]
                radius = roi_geojson.get('properties', {}).get('radius', 100000)  # meters
                
                # Approximate degrees from meters (rough, for bbox purposes)
                # 1 degree ≈ 111 km at equator
                degree_offset = radius / 111000.0
                
                return (
                    lon - degree_offset,
                    lat - degree_offset,
                    lon + degree_offset,
                    lat + degree_offset
                )
            
            elif geom_type == 'Polygon':
                # Rectangle or polygon
                coords = geom['coordinates'][0]
                lons = [c[0] for c in coords]
                lats = [c[1] for c in coords]
                return (min(lons), min(lats), max(lons), max(lats))
            
            else:
                return None
                
        except Exception as e:
            print(f"Error extracting bbox from ROI: {e}")
            return None
    
    def add_stations(self, stations: List[dict]):
        """
        Add station markers to map.
        
        Args:
            stations: List of station dicts with 'latitude', 'longitude', 'network', 'station'
        """
        self.station_markers = stations
        
        # Color palette for networks
        palette = ['#1f77b4','#ff7f0e','#2ca02c','#d62728','#9467bd','#8c564b','#e377c2','#7f7f7f','#bcbd22','#17becf']
        
        # Generate JavaScript to add styled station markers (triangles)
        for station in stations:
            lat = station['latitude']
            lon = station['longitude']
            net = station.get('network','')
            color = palette[hash(net) % len(palette)]
            label = f"{net}.{station.get('station','')}"
            js = f"addStationTriangle({lat}, {lon}, '{color}', '{label}');"
            self.web_view.page().runJavaScript(js)
        
        if self.add_draw_controls:
            self.lbl_info.setText(f"Displayed {len(stations)} stations on map")
    
    def add_events(self, events: List[dict], filtered_ids: set = None):
        """
        Add event markers to map.
        
        Args:
            events: List of event dicts with 'latitude', 'longitude', 'event_id', 'magnitude'
            filtered_ids: Set of event_ids that are filtered out (will be gray)
        """
        self.event_markers = events
        
        if filtered_ids is None:
            filtered_ids = set()
        
        # Generate JavaScript to add markers
        for event in events:
            lat = event['latitude']
            lon = event['longitude']
            event_id = event['event_id']
            mag = event.get('magnitude', 0)
            label = f"M{mag:.1f} - {event_id}"
            js = f"addEventPoint({lat}, {lon}, '{label}');"
            self.web_view.page().runJavaScript(js)
        
        if self.add_draw_controls:
            self.lbl_info.setText(f"Displayed {len(events)} events on map")
    
    def set_center_and_rings(
        self,
        center: Tuple[float, float],
        distances_deg: List[float]
    ):
        """
        Display center point and distance rings.
        
        Args:
            center: (lat, lon) of center point
            distances_deg: List of distances in degrees for rings
        """
        self.current_center = center
        lat, lon = center
        
        # Add center marker (styled)
        js_center = f"addCenterPoint({lat}, {lon});"
        self.web_view.page().runJavaScript(js_center)
        
        # Add distance rings (fallback circles for reliability)
        for dist_deg in distances_deg:
            radius_m = dist_deg * 111000.0
            js_ring = f"addRing({lat}, {lon}, {radius_m}, '#0000ff', '5, 5', '{dist_deg}°');"
            self.web_view.page().runJavaScript(js_ring)
        
        self.lbl_info.setText(f"Center: {lat:.3f}°N, {lon:.3f}°E with {len(distances_deg)} rings")
    
    def clear_markers(self, marker_type: str = 'all'):
        """
        Clear markers from map.
        
        Args:
            marker_type: 'all', 'stations', 'events', or 'rings'
        """
        if marker_type == 'all':
            self.web_view.page().runJavaScript("clearAllLayers();")
            self.station_markers = []
            self.event_markers = []
        # TODO: Implement selective clearing if needed
    
    def get_current_roi(self) -> Optional[dict]:
        """Get current ROI as GeoJSON dict"""
        return self.current_roi
    
    def get_current_center(self) -> Optional[Tuple[float, float]]:
        """Get current center as (lat, lon)"""
        return self.current_center

    def fetch_roi_async(self, callback: Callable[[Optional[dict]], None]):
        """
        Attempt to read the ROI GeoJSON from the page even if QWebChannel events failed.
        This queries window.lastGeoJSON set by our injected JS and calls the callback
        with a parsed dict or None.
        """
        js = "(function(){ try { return window.lastGeoJSON ? JSON.stringify(window.lastGeoJSON) : null; } catch(e){ return null; } })();"
        def _cb(result):
            try:
                if result:
                    gj = json.loads(result)
                    self.current_roi = gj
                    self.roi_changed.emit(gj)
                    callback(gj)
                else:
                    callback(None)
            except Exception:
                callback(None)
        self.web_view.page().runJavaScript(js, _cb)

    # Programmatic ROI drawing API
    def draw_rectangle(self, min_lat: float, min_lon: float, max_lat: float, max_lon: float):
        """Draw a rectangle ROI and update current ROI in Python via JS callback"""
        js = f"setROIFromBounds({min_lat}, {min_lon}, {max_lat}, {max_lon});"
        self.web_view.page().runJavaScript(js)

    def draw_circle(self, center_lat: float, center_lon: float, radius_km: float):
        """Draw a circle ROI and update current ROI in Python via JS callback"""
        radius_m = float(radius_km) * 1000.0
        js = f"setROICircle({center_lat}, {center_lon}, {radius_m});"
        self.web_view.page().runJavaScript(js)

    def _on_page_load_finished(self, ok: bool):
        # Define functions after page load; robustly find the Leaflet map
        # First, expose the Folium map variable name (if known) to JS
        try:
            map_name = getattr(self, "_map_var", None)
        except Exception:
            map_name = None
        if map_name:
            js_pre = f"(function(){{ window.__LEAFLET_MAP_VAR_NAME = '{map_name}'; }})();"
            try:
                self.web_view.page().runJavaScript(js_pre)
            except Exception:
                pass

        runtime = """
        (function(){
          function getMap(){
            try{
              // Prefer the explicit Folium map variable if known
              if (window.__LEAFLET_MAP_VAR_NAME && window[window.__LEAFLET_MAP_VAR_NAME] &&
                  typeof window[window.__LEAFLET_MAP_VAR_NAME].addLayer==='function' &&
                  typeof window[window.__LEAFLET_MAP_VAR_NAME].getCenter==='function' &&
                  typeof window[window.__LEAFLET_MAP_VAR_NAME].setView==='function') {
                return window[window.__LEAFLET_MAP_VAR_NAME];
              }
              // Fallback: scan globals for a Leaflet map instance
              for (var k in window){
                var v = window[k];
                if (v && typeof v.addLayer==='function' && typeof v.getCenter==='function' && typeof v.setView==='function') return v;
              }
            }catch(e){}
            return null;
          }
          var MAP = getMap();
          if(!MAP){ console.error('Leaflet map not found in MapPane runtime'); return; }
          console.log('MapPane runtime using map', MAP);

          // Drawing layer group (used when drawing is enabled; harmless otherwise)
          try{ if(!window.__drawnItems){ window.__drawnItems = new L.FeatureGroup(); MAP.addLayer(window.__drawnItems); } }catch(e){}

          window.clearAllLayers = function(){
            try{
              if (window.__drawnItems){ window.__drawnItems.clearLayers(); }
              MAP.eachLayer(function(layer){
                if (layer instanceof L.Marker || layer instanceof L.Circle || layer instanceof L.Polyline || layer instanceof L.Polygon) {
                  if (layer !== window.__drawnItems) { MAP.removeLayer(layer); }
                }
              });
            }catch(e){ console.warn('clearAllLayers error', e); }
          };

          window.addCenterPoint = function(lat, lon){
            console.log('addCenterPoint', lat, lon);
            return L.circleMarker([lat, lon], {radius: 5, color: '#000', fillColor: '#000', fillOpacity: 1, weight: 1}).addTo(MAP);
          };

          window.addEventPoint = function(lat, lon, label){
            console.log('addEventPoint', lat, lon, label);
            var m = L.circleMarker([lat, lon], {radius: 4, color: '#000', fillColor: '#d62728', fillOpacity: 0.9, weight: 1});
            if (label) { m.bindPopup(label); }
            return m.addTo(MAP);
          };

          window.addStationTriangle = function(lat, lon, color, label){
            console.log('addStationTriangle', lat, lon, color, label);
            var d = 0.05; var coslat = Math.cos(lat*Math.PI/180.0); var dx = d * Math.max(coslat, 0.2);
            var p1 = [lat + d, lon]; var p2 = [lat - d, lon - dx]; var p3 = [lat - d, lon + dx];
            var tri = L.polygon([p1, p2, p3], {color: '#000', weight: 1, fillColor: color || '#1f77b4', fillOpacity: 0.9});
            if (label) { tri.bindPopup(label); }
            return tri.addTo(MAP);
          };

          window.addRing = function(lat, lon, radius_m, color, dashArray, label){
            console.log('addRing', lat, lon, radius_m, color, label);
            var circle = L.circle([lat, lon], { radius: radius_m, fillColor: 'transparent', fillOpacity: 0, color: color || '#0000ff', weight: 2, dashArray: dashArray || '5, 5' }).addTo(MAP);
            if (label) { circle.bindPopup(label); }
            return circle;
          };

          // Wire draw handlers to set window.lastGeoJSON and notify Qt
          try {
            var qobj = (typeof qt !== 'undefined') ? qt : (window.qt || null);
            var haveChannel = (typeof QWebChannel !== 'undefined') && qobj;
            var qtBridge = null;
            if (haveChannel) {
              new QWebChannel(qobj.webChannelTransport, function(channel) { qtBridge = channel.objects.qtBridge; });
            }
            if (MAP && MAP.on) {
              MAP.on('draw:created', function(e){
                try {
                  var layer = e.layer; var gj = layer.toGeoJSON(); window.lastGeoJSON = gj;
                  if (e.layerType === 'circle') {
                    gj.properties = gj.properties || {}; gj.properties.radius = layer.getRadius();
                    window.lastGeoJSON.properties = window.lastGeoJSON.properties || {}; window.lastGeoJSON.properties.radius = layer.getRadius();
                  }
                  if (qtBridge) { try { qtBridge.onShapeDrawn(JSON.stringify(gj)); } catch(err) {} }
                } catch(err) { console.warn('draw:created wire error', err); }
              });
              MAP.on('draw:edited', function(e){
                try {
                  e.layers.eachLayer(function(layer){
                    var gj = layer.toGeoJSON(); window.lastGeoJSON = gj;
                    if (layer instanceof L.Circle) {
                      gj.properties = gj.properties || {}; gj.properties.radius = layer.getRadius();
                      window.lastGeoJSON.properties = window.lastGeoJSON.properties || {}; window.lastGeoJSON.properties.radius = layer.getRadius();
                    }
                    if (qtBridge) { try { qtBridge.onShapeEdited(JSON.stringify(gj)); } catch(err) {} }
                  });
                } catch(err) { console.warn('draw:edited wire error', err); }
              });
              MAP.on('draw:deleted', function(e){
                window.lastGeoJSON = null; if (qtBridge) { try { qtBridge.onShapeDeleted(); } catch(err) {} }
              });
            }
          } catch(e) { console.warn('wire draw handlers failed', e); }
        })();
        """
        try:
            self.web_view.page().runJavaScript(runtime)
        except Exception:
            pass
