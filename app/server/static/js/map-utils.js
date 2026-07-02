/**
 * Shared map/Leaflet utilities — marker creation, power line rendering, trajectory
 */
var MapUtils = (function() {
  var STATUS_COLORS = { active: '#16a34a', warning: '#ca8a04', severe: '#ea580c', critical: '#dc262e', offline: '#bbb', gone: '#bbb' };
  var STATUS_ZH = { active: '正常', warning: '警告', severe: '严重', critical: '危险', offline: '离线', gone: '离线' };
  function esc(v) { return window.UI ? UI.escapeHtml(v) : String(v == null ? '' : v); }
  function attr(v) { return window.UI ? UI.escapeAttr(v) : esc(v); }

  function markerColor(s) { return STATUS_COLORS[s] || '#16a34a'; }
  function markerRadius(s) {
    if (s === 'critical') return 10;
    if (s === 'severe') return 8;
    if (s === 'warning') return 6;
    return 5;
  }

  /** Create a Leaflet circleMarker for a drone */
  function createDroneMarker(map, dr) {
    var lat = dr.last_lat, lon = dr.last_lon;
    if (lat == null || lon == null) return null;
    var s = dr.status || 'active';
    return L.circleMarker([lat, lon], {
      radius: markerRadius(s),
      color: markerColor(s),
      fillColor: markerColor(s),
      fillOpacity: 0.55,
      weight: 2.5
    });
  }

  /** Update existing marker position and style */
  function updateDroneMarker(marker, dr) {
    var lat = dr.last_lat, lon = dr.last_lon;
    if (lat == null || lon == null) return;
    var s = dr.status || 'active';
    marker.setLatLng([lat, lon]);
    marker.setStyle({ color: markerColor(s), fillColor: markerColor(s) });
    marker.setRadius(markerRadius(s));
  }

  /** Build a popup HTML string for a drone */
  function dronePopup(dr, activeTrajId) {
    var s = dr.status || 'active';
    var dist = dr.min_distance != null ? dr.min_distance.toFixed(0) + ' m' : '--';
    var alt = dr.last_alt != null ? dr.last_alt.toFixed(0) + ' m' : '--';
    var model = dr.product_model || '';
    var isActive = activeTrajId === dr.id;
    var color = markerColor(s);
    var status = STATUS_ZH[s];
    return '<b>' + esc(dr.id) + '</b>'
      + (model ? '<div class="popup-row"><span>型号:</span><span style="color:var(--blue)">' + esc(model) + '</span></div>' : '')
      + '<div class="popup-row"><span>状态:</span><span style="color:' + color + ';font-weight:600">' + status + '</span></div>'
      + '<div class="popup-row"><span>经度:</span><span>' + (dr.last_lon || 0).toFixed(5) + '</span></div>'
      + '<div class="popup-row"><span>纬度:</span><span>' + (dr.last_lat || 0).toFixed(5) + '</span></div>'
      + '<div class="popup-row"><span>高度:</span><span>' + alt + '</span></div>'
      + '<div class="popup-row"><span>距离:</span><span>' + dist + '</span></div>'
      + '<div class="popup-row"><span>最近电力线:</span><span>' + esc(dr.line_name || '--') + '</span></div>'
      + '<button class="popup-btn' + (isActive ? ' traj-active' : '') + '" data-action="toggle-trajectory" data-drone-id="' + attr(dr.id) + '">' + (isActive ? '隐藏轨迹' : '显示轨迹') + '</button>';
  }

  /** Render power lines as Leaflet polylines + tooltip labels */
  function renderPowerLines(map, lines, opts) {
    opts = opts || {};
    var color = opts.color || '#e74c3c';
    var width = opts.width || 3;
    var polylines = [], labels = [];

    lines.forEach(function(l) {
      var latlngs = [[l.lat1, l.lon1], [l.lat2, l.lon2]];
      var poly = L.polyline(latlngs, {
        color: color, weight: width, dashArray: '10,8', opacity: 0.85
      }).addTo(map);
      var label = L.tooltip({ permanent: true, direction: 'center' })
        .setLatLng([(l.lat1 + l.lat2) / 2, (l.lon1 + l.lon2) / 2])
        .setContent('<span style="font-size:10px;background:rgba(255,255,255,.92);padding:2px 6px;border-radius:4px;color:' + color + ';font-weight:600">' + esc(l.name) + (l.voltage_level ? ' ' + esc(l.voltage_level) : '') + '</span>')
        .addTo(map);
      polylines.push(poly); labels.push(label);
    });
    return { polylines: polylines, labels: labels };
  }

  /** Render buffer zones around power lines */
  function buildBufferZones(map, lines) {
    var zones = [];
    lines.forEach(function(l) {
      var latlngs = [[l.lat1, l.lon1], [l.lat2, l.lon2]];
      zones.push(L.polyline(latlngs, { color: 'rgba(220,38,38,0.10)', weight: 8, opacity: 1, smoothFactor: 1, interactive: false }).addTo(map));
      zones.push(L.polyline(latlngs, { color: 'rgba(234,88,12,0.06)', weight: 18, opacity: 1, smoothFactor: 1, interactive: false }).addTo(map));
      zones.push(L.polyline(latlngs, { color: 'rgba(202,138,4,0.04)', weight: 34, opacity: 1, smoothFactor: 1, interactive: false }).addTo(map));
    });
    return zones;
  }

  return {
    STATUS_COLORS: STATUS_COLORS,
    STATUS_ZH: STATUS_ZH,
    markerColor: markerColor,
    markerRadius: markerRadius,
    createDroneMarker: createDroneMarker,
    updateDroneMarker: updateDroneMarker,
    dronePopup: dronePopup,
    renderPowerLines: renderPowerLines,
    buildBufferZones: buildBufferZones
  };
})();

window.MapUtils = MapUtils;
