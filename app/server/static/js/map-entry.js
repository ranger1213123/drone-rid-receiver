/**
 * Map page entry point — all page-specific logic extracted from map.html inline script.
 * Imports shared modules and bundles all map functionality.
 */
import './api.js';
import './ui.js';
import './chart-utils.js';
import regionData from './region-data.js';
import * as L from 'leaflet';
import 'leaflet/dist/leaflet.css';
window.L = L;

// Alias shared UI functions for backward compatibility
var showToast = UI.toast;
var catchErr = function(msg){
  return function(e){
    console.warn(msg, e);
    UI.toast((msg||'请求失败')+': '+(e.message||'网络错误'), 'error');
  };
};

// ── 塔高参考 (GB 50545 / DL/T 5092) ──
var TOWER_HEIGHTS = {'10kV':15,'35kV':18,'66kV':22,'110kV':25,'220kV':35,'330kV':40,'500kV':50,'750kV':60,'±800kV':65,'1000kV':80};
function estTowerHeight(vl){
  if(!vl) return 25;
  for(var k in TOWER_HEIGHTS){if(vl.indexOf(k)>=0) return TOWER_HEIGHTS[k];}
  return 25;
}
window.onVoltageChangeForPl = function(){
  var vl=document.getElementById('plVoltage').value;
  var h=estTowerHeight(vl);
  if(!document.getElementById('plTh1').value) document.getElementById('plTh1').value=h;
  if(!document.getElementById('plTh2').value) document.getElementById('plTh2').value=h;
  onPlFieldChange();
};
window.onPlFieldChange = function(){
  var alt1=parseFloat(document.getElementById('plAlt1').value)||0;
  var th1=parseFloat(document.getElementById('plTh1').value)||0;
  var alt2=parseFloat(document.getElementById('plAlt2').value)||0;
  var th2=parseFloat(document.getElementById('plTh2').value)||0;
  var hint=document.getElementById('plAltHint');
  if(th1||th2){hint.style.display='';hint.textContent='导线海拔: 端点1='+(alt1+th1).toFixed(1)+'m  端点2='+(alt2+th2).toFixed(1)+'m';}
  else{hint.style.display='none';}
};
window.onPlEditVoltageChange = function(){
  var vl=document.getElementById('plEditInpVl').value;
  var h=estTowerHeight(vl);
  var th1=document.getElementById('plEditInpTh1');
  var th2=document.getElementById('plEditInpTh2');
  if(!th1.value) th1.value=h;
  if(!th2.value) th2.value=h;
};

// Global unhandled promise rejection handler
window.addEventListener('unhandledrejection',function(e){
  console.warn('Fetch error:',e.reason);
  e.preventDefault();
});

// ═══════════ Map ═══════════
var map = L.map('map', {attributionControl: false, zoomControl: false, preferCanvas: true}).setView([35, 105], 4.5);
L.control.zoom({position:'bottomright'}).addTo(map);
var T = window.__TILE_URLS || {};
var baseLayers={
  '标准地图': L.tileLayer(T.standard || 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap'}),
  '卫星影像': L.tileLayer(T.satellite || 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:18,attribution:'Esri,Maxar,Earthstar Geographics'}),
  '地形图': L.tileLayer(T.terrain || 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',{maxZoom:17,attribution:'&copy; OpenTopoMap'})
};
baseLayers['标准地图'].addTo(map);

// Layer switcher
var layerControl=L.control.layers(baseLayers,null,{position:'bottomleft',collapsed:true}).addTo(map);

var droneMarkers = {};
var trajPolylines = {};
var plPolylines = [];
var plLabels = [];
var stationMarkers = {};
var activeTrajDrone = null;
var bufZoneLayers = [];
var bufZonesVisible = false;
var nationalMode = true;

var statusColors = {active:'#16a34a',warning:'#ca8a04',severe:'#ea580c',critical:'#dc262e',gone:'#bbb'};
var statusZh = {active:'正常',warning:'警告',severe:'严重',critical:'危险',gone:'离线'};
function markerColor(s){return statusColors[s]||'#16a34a'}
function markerRadius(s){if(s==='critical')return 10;if(s==='severe')return 8;if(s==='warning')return 6;return 5}

var cachedDashboard = null;
var cachedDrones = [];
var currentUser = {username:'', role:'user', station:'', tenant_id:null, scope:'station', assigned_station:''};
var currentStationDevice = null;

// ═══════════ Mode switching ═══════════
window.enterStationView = function(station){
  if(!station) return;
  nationalMode = false;
  document.body.className = 'sta-mode';
  var name=station.name||station.device_name||'站点';
  var loc=station.location||station.device_location||'';
  document.getElementById('staViewTitle').textContent=name+(loc?' — '+loc:'');
  currentStationDevice = station.device_name || station.name || null;
  var backBtn=document.querySelector('.back-btn');
  if(backBtn && currentUser.role==='user' && currentUser.scope==='station'){
    backBtn.style.display='none';
  }
  var persBtn=document.getElementById('personnelBtn');
  if(persBtn) persBtn.style.display='inline-block';
  var label=document.getElementById('personnelStationLabel');
  if(label) label.textContent=name;
  loadPowerLines();
  updateAll();
  var lat = station.lat!=null ? station.lat : (station.position?station.position.lat:0);
  var lon = station.lon!=null ? station.lon : (station.position?station.position.lon:0);
  if(lat && lon){
    map.flyTo([lat, lon], 14, {duration:1});
  }
};

// ═══════════ Sidebar toggle ═══════════
window.toggleLeft = function(){
  var lb=document.getElementById('leftBar'), btn=document.getElementById('toggleLeftBtn');
  lb.classList.toggle('collapsed'); btn.classList.toggle('shifted');
  btn.textContent=lb.classList.contains('collapsed')?'▶':'◀';
  setTimeout(function(){map.invalidateSize()},350);
};
window.toggleRight = function(){
  var rb=document.getElementById('rightBar'), btn=document.getElementById('toggleRightBtn');
  rb.classList.toggle('open');
  btn.textContent=rb.classList.contains('open')?'▶':'◀';
  setTimeout(function(){map.invalidateSize()},350);
};
window.toggleSection = function(id,head){
  var body=document.getElementById(id), chev=head.querySelector('.chevron');
  body.classList.toggle('collapsed'); if(chev) chev.classList.toggle('open');
};

// ═══════════ Chart.js — alert charts ═══════════
var natChart=null, staChart=null;

function buildAlertChart(canvasId, chartRef, hourly, compact){
  var result = ChartUtils.buildAlertChart(canvasId, chartRef, hourly, compact);
  if(canvasId==='natAlertChart') natChart=result;
  else if(canvasId==='staAlertChart') staChart=result;
  return result;
}

// ═══════════ Model distribution ═══════════
function buildModelBars(models){
  var div=document.getElementById('modelBars');
  if(!div) return;
  if(!models||!models.length){div.innerHTML='<div class="empty-state">暂无数据</div>';return;}
  var max=models[0].count;
  div.innerHTML=models.map(function(m){
    var pct=Math.max(4,Math.round(m.count/max*100));
    return '<div class="model-bar"><span class="m-name" title="'+UI.escapeHtml(m.model)+'">'+UI.escapeHtml(m.model)+'</span><div class="m-track"><div class="m-fill" style="width:'+pct+'%"></div></div><span class="m-cnt">'+m.count+'</span></div>';
  }).join('');
}

// ═══════════ Station info grid ═══════════
function buildStationGrid(gridId, stations){
  var g=document.getElementById(gridId);
  if(!g) return;
  var list = Array.isArray(stations) ? stations : [stations];
  g.innerHTML=list.map(function(s){
    var pos=s.position||{};
    var rows=[['设备',UI.escapeHtml(s.device_name||'--')],['位置',UI.escapeHtml(s.location||'--')],['坐标',pos.lat!=null?pos.lat.toFixed(4)+', '+pos.lon.toFixed(4):'--'],['MQTT',s.mqtt_online?'在线':'离线']];
    var header='<div class="station-item full" style="font-weight:600;font-size:11px;color:var(--blue);padding-top:4px;border-top:1px solid var(--border)">'+UI.escapeHtml(s.name||s.device_name||'--')+'</div>';
    return header+rows.map(function(r){return '<div class="station-item"><div class="sk">'+r[0]+'</div><div class="sv">'+r[1]+'</div></div>'}).join('');
  }).join('');
}

// ═══════════ Station markers — national overview ═══════════
function updateStationMarkers(stations){
  if(!stations||!stations.length) return;
  Object.values(stationMarkers).forEach(function(m){map.removeLayer(m)});
  stationMarkers = {};
  stations.forEach(function(st, i){
    var lat = st.lat!=null ? st.lat : (st.position?st.position.lat:0);
    var lon = st.lon!=null ? st.lon : (st.position?st.position.lon:0);
    if(!lat && !lon) return;
    var icon=L.divIcon({className:'station-icon',iconSize:[20,20],iconAnchor:[10,10]});
    var name = st.name||st.device_name||'站点';
    var m = L.marker([lat,lon],{icon:icon}).addTo(map);
    m.bindTooltip(name,{permanent:true,direction:'top',offset:[0,-14]});
    m.on('click', (function(s){return function(){enterStationView(s)}})(st));
    stationMarkers[i] = m;
  });
}

// ═══════════ National: station cards ═══════════
function renderNationalStationCards(stations){
  var div=document.getElementById('natStationList');
  if(!div) return;
  if(!stations||!stations.length){
    div.innerHTML='<div class="empty-state">暂无站点</div>';
    document.getElementById('natStationCount').textContent='0';
    return;
  }
  document.getElementById('natStationCount').textContent=stations.length;
  div.innerHTML=stations.map(function(st,i){
    var lat=st.lat!=null?st.lat:(st.position?st.position.lat:0);
    var lon=st.lon!=null?st.lon:(st.position?st.position.lon:0);
    var name=st.name||st.device_name||'站点';
    var loc=st.location||'';
    var mqttLabel=st.mqtt_online?'MQTT 在线':'MQTT 离线';
    return '<div class="station-card" data-enter-station="'+i+'">'
      +'<div class="s-name">'+UI.escapeHtml(name)+'</div>'
      +'<div class="s-loc">'+UI.escapeHtml(loc)+'</div>'
      +'<div class="s-row">'
      +'<span>坐标 '+(lat?lat.toFixed(2)+','+lon.toFixed(2):'--')+'</span>'
      +'<span>'+mqttLabel+'</span>'
      +'</div></div>';
  }).join('');
}

// ═══════════ National: alert drone list ═══════════
function renderNationalAlerts(drones){
  var div=document.getElementById('natAlertList');
  if(!div) return;
  var alerts = (drones||[]).filter(function(dr){
    var s=dr.status||'active';
    return s==='warning'||s==='severe'||s==='critical';
  }).sort(function(a,b){
    var order={critical:0,severe:1,warning:2};
    return (order[a.status]||9)-(order[b.status]||9);
  });
  var key=alerts.map(function(d){return d.id+'|'+d.status+'|'+(d.min_distance||0).toFixed(0)+'|'+d.last_seen+'|'+d.model}).join(',');
  if(renderNationalAlerts._key===key) return;
  renderNationalAlerts._key=key;
  if(!alerts.length){div.innerHTML='<div class="empty-state">暂无预警</div>';return;}
  div.innerHTML=alerts.map(function(dr){
    var s=dr.status||'active';
    var model=dr.model||'';
    var time=(dr.last_seen||'').substring(11,19);
    var stationName=(cachedDashboard&&cachedDashboard.station)?cachedDashboard.station.device_name:'站点';
    return '<div class="alert-row" data-enter-station-from-alert="">'
      +'<span class="al-dot '+s+'"></span>'
      +'<div class="al-info"><div class="al-id">'+UI.escapeHtml(dr.id)+'</div><div class="al-sub">'+UI.escapeHtml(stationName)+' · <span style="color:'+(s==='critical'?'var(--red)':s==='severe'?'var(--orange)':'var(--yellow)')+';font-weight:500">'+statusZh[s]+'</span> · '+time+'</div></div>'
      +'<span class="al-model">'+UI.escapeHtml(model)+'</span></div>';
  }).join('');
}

// ═══════════ Station: drone list ═══════════
function renderDroneList(){
  var searchTerm=(document.getElementById('droneSearch').value||'').toLowerCase();
  var drones=cachedDrones;
  if(currentStationDevice){
    drones=drones.filter(function(dr){return (dr.device_name||dr.device||'')===currentStationDevice});
  }
  if(searchTerm){
    drones=drones.filter(function(dr){
      var id=(dr.id||'').toLowerCase();
      var model=(dr.model||'').toLowerCase();
      return id.includes(searchTerm)||model.includes(searchTerm);
    });
  }
  var key=drones.map(function(d){return d.id+'|'+d.status+'|'+(d.min_distance||0).toFixed(0)+'|'+d.last_seen+'|'+d.model+'|'+(d.last_lat||0).toFixed(4)+'|'+(d.last_lon||0).toFixed(4)}).join(',')+'||'+searchTerm;
  if(renderDroneList._key===key) return;
  renderDroneList._key=key;
  var listDiv=document.getElementById('droneList');
  if(!drones.length){
    listDiv.innerHTML='<div class="empty-state">'+(searchTerm?'无匹配结果':'等待信号...')+'</div>';
    return;
  }
  listDiv.innerHTML=drones.map(function(dr){
    var s=dr.status||'active';
    var dist=dr.min_distance!=null?dr.min_distance.toFixed(0)+'m':'-';
    var time=(dr.last_seen||'').substring(11,19);
    var model=dr.model||'';
    var idShort=dr.id.length>14?dr.id.substring(0,14)+'...':dr.id;
    return '<div class="drone-row" data-fly-to="'+dr.last_lat+','+dr.last_lon+'">'
      +'<span class="d-icon '+s+'"></span>'
      +'<div class="d-info"><div class="d-id">'+UI.escapeHtml(idShort)+'</div><div class="d-model">'+UI.escapeHtml(model)+'</div><div class="d-sub">'+time+' · '+statusZh[s]+'</div></div>'
      +'<div class="d-dist">'+dist+'</div></div>';
  }).join('');
}

// Delegate for drone list fly-to clicks
UI.delegate(document.getElementById('droneList'), 'click', '[data-fly-to]', function(){
  var parts = (this.dataset.flyTo||'').split(',');
  var lat = parseFloat(parts[0]), lon = parseFloat(parts[1]);
  if(!isNaN(lat) && !isNaN(lon)) map.flyTo([lat, lon], Math.max(map.getZoom(), 15), {duration:.5});
});

// ═══════════ Station alert list ═══════════
function renderStationAlerts(drones){
  var div=document.getElementById('staAlertList');
  if(!div) return;
  var alerts=(drones||[]).filter(function(dr){
    var s=dr.status||'active';
    return s==='warning'||s==='severe'||s==='critical';
  }).sort(function(a,b){
    var order={critical:0,severe:1,warning:2};
    return (order[a.status]||9)-(order[b.status]||9);
  });
  document.getElementById('staAlertCount').textContent=alerts.length||'';
  var key=alerts.map(function(d){return d.id+'|'+d.status+'|'+(d.min_distance||0).toFixed(0)+'|'+d.last_seen}).join(',');
  if(renderStationAlerts._key===key) return;
  renderStationAlerts._key=key;
  if(!alerts.length){div.innerHTML='<div class="empty-state">暂无预警</div>';return;}
  div.innerHTML=alerts.map(function(dr){
    var s=dr.status||'active';
    var dist=dr.min_distance!=null?dr.min_distance.toFixed(0)+'m':'-';
    var time=(dr.last_seen||'').substring(11,19);
    return '<div class="alert-row" data-fly-to="'+dr.last_lat+','+dr.last_lon+'">'
      +'<span class="al-dot '+s+'"></span>'
      +'<div class="al-info"><div class="al-id">'+UI.escapeHtml(dr.id)+'</div><div class="al-sub">'+UI.escapeHtml(dr.line_name||dr.nearest_line||'--')+' · '+time+'</div></div>'
      +'<span class="al-dist">'+dist+'</span></div>';
  }).join('');
}

// Delegate for station alert list fly-to clicks
UI.delegate(document.getElementById('staAlertList'), 'click', '[data-fly-to]', function(){
  var parts = (this.dataset.flyTo||'').split(',');
  var lat = parseFloat(parts[0]), lon = parseFloat(parts[1]);
  if(!isNaN(lat) && !isNaN(lon)) map.flyTo([lat, lon], Math.max(map.getZoom(), 15), {duration:.5});
});

// Delegate for national alert list: enter station view
UI.delegate(document.getElementById('natAlertList'), 'click', '[data-enter-station-from-alert]', function(){
  if(cachedDashboard&&cachedDashboard.stations&&cachedDashboard.stations.length){
    enterStationView(cachedDashboard.stations[0]);
  }
});

// ═══════════ Power lines ═══════════
var PL_COLOR='#e74c3c', PL_WIDTH=3;

window.loadPowerLines = function(){
  Api.get('/api/powerlines').then(function(lines){
    plPolylines.forEach(function(p){map.removeLayer(p)});
    plLabels.forEach(function(l){map.removeLayer(l)});
    plPolylines=[];plLabels=[];
    document.getElementById('plCountLabel').textContent=lines.length;

    var html='';
    if(!lines.length){html='<div class="empty-state">无电力线</div>'}
    else{
      lines.forEach(function(l){
        var latlngs=[[l.lat1,l.lon1],[l.lat2,l.lon2]];
        var poly=L.polyline(latlngs,{color:PL_COLOR,weight:PL_WIDTH,dashArray:'10,8',opacity:.85}).addTo(map);
        var label=L.tooltip({permanent:true,direction:'center'})
          .setLatLng([(l.lat1+l.lat2)/2,(l.lon1+l.lon2)/2])
          .setContent('<span style="font-size:10px;background:rgba(255,255,255,.92);padding:2px 6px;border-radius:4px;color:'+PL_COLOR+';font-weight:600">'+UI.escapeHtml(l.name)+(l.voltage_level?' '+UI.escapeHtml(l.voltage_level):'')+'</span>')
          .addTo(map);
        plPolylines.push(poly);plLabels.push(label);
        var vlBadge=l.voltage_level?' <span style="font-size:9px;color:'+PL_COLOR+'">'+UI.escapeHtml(l.voltage_level)+'</span>':'';
        html+='<div class="pl-mini" data-fly-bounds="'+l.lat1+','+l.lon1+','+l.lat2+','+l.lon2+'"><span class="pl-swatch" style="background:'+PL_COLOR+'"></span>'+UI.escapeHtml(l.name)+vlBadge+'</div>';
      });
    }
    document.getElementById('plItems').innerHTML=html;
    if(bufZonesVisible) buildBufferZones();
  }).catch(catchErr('加载电力线失败'));
};

// Delegate for power line mini fly-to-bounds
UI.delegate(document.getElementById('plItems'), 'click', '[data-fly-bounds]', function(){
  var p = (this.dataset.flyBounds||'').split(',');
  var lat1=parseFloat(p[0]), lon1=parseFloat(p[1]), lat2=parseFloat(p[2]), lon2=parseFloat(p[3]);
  if(!isNaN(lat1)&&!isNaN(lon1)&&!isNaN(lat2)&&!isNaN(lon2)){
    map.flyToBounds([[lat1,lon1],[lat2,lon2]],{padding:[80,80]});
  }
});

// ═══════════ Trajectory ═══════════
window.toggleTrajectory = function(droneId){
  if(activeTrajDrone===droneId){removeTrajectory(droneId);activeTrajDrone=null;}
  else{if(activeTrajDrone) removeTrajectory(activeTrajDrone);showTrajectory(droneId);}
};

function showTrajectory(droneId){
  if(!droneId) return;
  Api.get('/api/trajectories/'+encodeURIComponent(droneId)+'/points')
    .then(function(pts){
      if(!pts||pts.length<2) return;
      var latlngs=pts.map(function(p){return[p.lat,p.lon]});
      var line=L.polyline(latlngs,{color:'#2563eb',weight:3,opacity:.75,smoothFactor:1}).addTo(map);
      var sm=L.circleMarker(latlngs[0],{radius:5,color:'#2563eb',fillColor:'#fff',fillOpacity:1,weight:2.5}).addTo(map);
      var em=L.circleMarker(latlngs[latlngs.length-1],{radius:6,color:'#2563eb',fillColor:'#2563eb',fillOpacity:1,weight:2.5}).addTo(map);
      trajPolylines[droneId]={line:line,start:sm,end:em};
      activeTrajDrone=droneId;
      map.fitBounds(line.getBounds(),{padding:[80,80],maxZoom:16});
    }).catch(function(){}); // 轨迹可视化失败时静默忽略
}

function removeTrajectory(id){
  var l=trajPolylines[id];
  if(l){map.removeLayer(l.line);map.removeLayer(l.start);map.removeLayer(l.end);delete trajPolylines[id]}
}

window.flyToDrone = function(lat,lon){map.flyTo([lat,lon],Math.max(map.getZoom(),15),{duration:.5})};

// ═══════════ Popup content (station mode) ═══════════
function popupContent(dr){
  var s=dr.status||'active';
  var dist=dr.min_distance!=null?dr.min_distance.toFixed(0)+' m':'--';
  var alt=dr.last_alt!=null?dr.last_alt.toFixed(0)+' m':'--';
  var model=dr.model||'';
  var isActive=activeTrajDrone===dr.id;
  return '<b>'+UI.escapeHtml(dr.id)+'</b>'
    +(model?'<div class="popup-row"><span>型号:</span><span style="color:var(--blue)">'+UI.escapeHtml(model)+'</span></div>':'')
    +'<div class="popup-row"><span>状态:</span><span style="color:'+markerColor(s)+';font-weight:600">'+statusZh[s]+'</span></div>'
    +'<div class="popup-row"><span>经度:</span><span>'+(dr.last_lon||0).toFixed(5)+'</span></div>'
    +'<div class="popup-row"><span>纬度:</span><span>'+(dr.last_lat||0).toFixed(5)+'</span></div>'
    +'<div class="popup-row"><span>高度:</span><span>'+alt+'</span></div>'
    +'<div class="popup-row"><span>距离:</span><span>'+dist+'</span></div>'
    +'<div class="popup-row"><span>最近电力线:</span><span>'+UI.escapeHtml(dr.line_name||dr.nearest_line||'--')+'</span></div>'
    +'<button class="popup-btn'+(isActive?' traj-active':'')+'" data-toggle-traj="'+UI.escapeAttr(dr.id)+'">'+(isActive?'隐藏轨迹':'显示轨迹')+'</button>';
}

// ═══════════ Animate values (delegated to UI module) ═══════════
var animateEl = UI.animateEl;

// ═══════════ Update comms in bottom bar ═══════════
function updateComms(bh, prefix){
  var dot=document.getElementById((prefix||'comm')+'4gDot');
  var lbl=document.getElementById((prefix||'comm')+'Label');
  if(!dot) return;
  if(bh){
    dot.className='comm-dot'+(bh.mqtt_online?' online':'');
    lbl.textContent=bh.mqtt_online?'MQTT 在线':'MQTT 离线';
  }
}

// ═══════════ Power Line Modal ═══════════
window.openPlModal = function(){
  document.getElementById('plModal').classList.add('show');
  refreshPlModalList();
};
window.closePlModal = function(){document.getElementById('plModal').classList.remove('show')};

function refreshPlModalList(){
  Api.get('/api/powerlines').then(function(lines){
    var div=document.getElementById('plModalList');
    if(!lines.length){div.innerHTML='<div style="color:var(--muted);padding:8px;text-align:center;font-size:11px">暂无电力线</div>';return}
    div.innerHTML=lines.map(function(l){
      var lid=l.id; var vl=l.voltage_level||'';
      return '<div class="pl-entry" id="plEntry'+lid+'">'
        +'<span style="flex:1;min-width:0">'
        +'<b id="plEditName'+lid+'">'+UI.escapeHtml(l.name)+'</b>'
        +(vl?' <span id="plEditVl'+lid+'" style="color:var(--blue);font-size:10px">'+UI.escapeHtml(vl)+'</span>':'')
        +' <span style="color:var(--muted);font-size:10px">('+l.lat1.toFixed(4)+','+l.lon1.toFixed(4)+') → ('+l.lat2.toFixed(4)+','+l.lon2.toFixed(4)+') 导线:'+l.alt1.toFixed(0)+'m'+(l.tower_height1?' (塔'+l.tower_height1.toFixed(0)+'m)':'')+'</span>'
        +'</span>'
        +'<span class="pl-del" data-edit-pl-modal="'+lid+'" style="color:var(--blue);margin-right:4px;cursor:pointer">✎</span>'
        +'<span class="pl-del" data-del-pl-modal="'+lid+'" style="cursor:pointer">×</span></div>';
    }).join('');
  }).catch(catchErr('加载电力线失败'));
}

var _editingPlId=-1;
window.editPowerLine = function(lineId){
  if(_editingPlId>=0) return;
  _editingPlId=lineId;
  Api.get('/api/powerlines').then(function(lines){
    var l=lines.find(function(x){return x.id===lineId}); if(!l) return;
    var entry=document.getElementById('plEntry'+lineId);
    var vlHeightMap={'10kV':'5.5','35kV':'6.5','66kV':'7.0','110kV':'7.0','220kV':'8.5','330kV':'9.5','500kV':'14.0','750kV':'19.5','±800kV':'21.0','1000kV':'27.0'};
    var vlOptions=['','10kV','35kV','66kV','110kV','220kV','330kV','500kV','750kV','±800kV','1000kV']
      .map(function(v){return '<option value="'+v+'"'+(v===(l.voltage_level||'')?' selected':'')+'>'+(v||'无')+(v?' — '+(vlHeightMap[v]||'')+'m':'')+'</option>'
      }).join('');
    entry.innerHTML='<span style="flex:1;display:flex;gap:4px;flex-wrap:wrap;align-items:center">'
      +'<input id="plEditInpName" value="'+UI.escapeAttr(l.name)+'" style="width:100px;padding:3px 6px;font-size:11px;background:var(--bg);border:1px solid var(--border);border-radius:4px">'
      +'<select id="plEditInpVl" data-pl-edit-vl style="padding:3px 4px;font-size:10px;background:var(--bg);border:1px solid var(--border);border-radius:4px">'+vlOptions+'</select>'
      +'<input id="plEditInpAlt1" type="number" step="0.1" value="'+l.alt1+'" style="width:55px;padding:3px 4px;font-size:10px;background:var(--bg);border:1px solid var(--border);border-radius:4px" title="端点1地面海拔">'
      +'<input id="plEditInpTh1" type="number" step="0.1" value="'+(l.tower_height1||'')+'" style="width:50px;padding:3px 4px;font-size:10px;background:var(--bg);border:1px solid var(--border);border-radius:4px" title="端点1塔高(m)">'
      +'<input id="plEditInpAlt2" type="number" step="0.1" value="'+l.alt2+'" style="width:55px;padding:3px 4px;font-size:10px;background:var(--bg);border:1px solid var(--border);border-radius:4px" title="端点2地面海拔">'
      +'<input id="plEditInpTh2" type="number" step="0.1" value="'+(l.tower_height2||'')+'" style="width:50px;padding:3px 4px;font-size:10px;background:var(--bg);border:1px solid var(--border);border-radius:4px" title="端点2塔高(m)">'
      +'</span>'
      +'<span class="pl-del" data-save-pl="'+lineId+'" style="color:var(--green);margin-right:4px;cursor:pointer">✓</span>'
      +'<span class="pl-del" data-cancel-edit-pl="" style="cursor:pointer">✗</span>';
  }).catch(catchErr('加载电力线失败'));
};

// Delegate for plEditInpVl onchange
UI.delegate(document.getElementById('plModalList'), 'change', '[data-pl-edit-vl]', function(){
  var vl=this.value;
  var h=estTowerHeight(vl);
  var th1=document.getElementById('plEditInpTh1');
  var th2=document.getElementById('plEditInpTh2');
  if(!th1.value) th1.value=h;
  if(!th2.value) th2.value=h;
});

window.cancelEditPl = function(){
  _editingPlId=-1;
  refreshPlModalList();
};
window.savePowerLine = function(lineId){
  var th1=document.getElementById('plEditInpTh1').value;
  var th2=document.getElementById('plEditInpTh2').value;
  var data={
    name:document.getElementById('plEditInpName').value.trim(),
    voltage_level:document.getElementById('plEditInpVl').value,
    alt1:parseFloat(document.getElementById('plEditInpAlt1').value)||0,
    alt2:parseFloat(document.getElementById('plEditInpAlt2').value)||0,
    tower_height1: th1!==''?parseFloat(th1):null,
    tower_height2: th2!==''?parseFloat(th2):null
  };
  if(!data.name){UI.Message.warning('电力线名称不能为空');return}
  Api.put('/api/powerlines/'+lineId, data).then(function(){
    _editingPlId=-1;
    refreshPlModalList();
    loadPowerLines();
  }).catch(catchErr('保存电力线失败'));
};

window.addPowerLine = function(){
  var data={
    name:document.getElementById('plName').value.trim(),
    voltage_level:document.getElementById('plVoltage').value,
    lat1:parseFloat(document.getElementById('plLat1').value),
    lon1:parseFloat(document.getElementById('plLon1').value),
    alt1:parseFloat(document.getElementById('plAlt1').value)||0,
    lat2:parseFloat(document.getElementById('plLat2').value),
    lon2:parseFloat(document.getElementById('plLon2').value),
    alt2:parseFloat(document.getElementById('plAlt2').value)||0,
    tower_height1: parseFloat(document.getElementById('plTh1').value)||null,
    tower_height2: parseFloat(document.getElementById('plTh2').value)||null
  };
  if(!data.name){UI.Message.warning('电力线名称不能为空');return}
  if(isNaN(data.lat1)||isNaN(data.lon1)||isNaN(data.lat2)||isNaN(data.lon2)){UI.Message.warning('请填写有效的经纬度坐标');return}
  Api.post('/api/powerlines', data).then(function(){
    document.getElementById('plName').value='';document.getElementById('plVoltage').value='';
    document.getElementById('plLat1').value='';document.getElementById('plLon1').value='';document.getElementById('plAlt1').value='';
    document.getElementById('plLat2').value='';document.getElementById('plLon2').value='';document.getElementById('plAlt2').value='';
    document.getElementById('plTh1').value='';document.getElementById('plTh2').value='';
    document.getElementById('plAltHint').style.display='none';
    refreshPlModalList();
    loadPowerLines();
  }).catch(catchErr('添加电力线失败'));
};

window.delPowerLine = function(lineId){
  UI.Message.confirm('确定删除此电力线？').then(function(ok){
    if(!ok) return;
    Api.del('/api/powerlines/'+lineId).then(function(){
      refreshPlModalList();
      loadPowerLines();
    }).catch(catchErr('删除电力线失败'));
  });
};

// ═══════════ Station Management Modal ═══════════
var _stations=[];
var _editingStName=null;

// ── Region cascade helpers (shared with station modal) ──
function _findProvince(name) { return regionData.find(function(p){ return p[0]===name; }); }
function _findCity(prov, name) { return prov[1].find(function(c){ return c[0]===name; }); }
function _popProvinceSelect(selId, placeholder) {
  var sel = document.getElementById(selId);
  sel.innerHTML = '<option value="">'+ (placeholder||'选择省') +'</option>';
  regionData.forEach(function(p){ sel.innerHTML += '<option value="'+p[0]+'">'+p[0]+'</option>'; });
}
function _onProvinceChange(provSelId, citySelId, countySelId, placeholder) {
  var prov = document.getElementById(provSelId).value;
  var citySel = document.getElementById(citySelId);
  var countySel = document.getElementById(countySelId);
  citySel.innerHTML = '<option value="">'+ (placeholder||'选择市') +'</option>';
  countySel.innerHTML = '<option value="">选择区/县</option>';
  if (!prov) return;
  var p = _findProvince(prov);
  if (!p) return;
  p[1].forEach(function(c){ citySel.innerHTML += '<option value="'+c[0]+'">'+c[0]+'</option>'; });
}
function _onCityChange(provSelId, citySelId, countySelId) {
  var prov = document.getElementById(provSelId).value;
  var city = document.getElementById(citySelId).value;
  var countySel = document.getElementById(countySelId);
  countySel.innerHTML = '<option value="">选择区/县</option>';
  if (!prov || !city) return;
  var p = _findProvince(prov);
  if (!p) return;
  var c = _findCity(p, city);
  if (!c) return;
  c[1].forEach(function(x){ countySel.innerHTML += '<option value="'+x+'">'+x+'</option>'; });
}
function _setRegionValues(province, city, county) {
  document.getElementById('stProvince').value = province || '';
  if (province) {
    _onProvinceChange('stProvince', 'stCity', 'stCounty', '选择市');
    document.getElementById('stCity').value = city || '';
    if (city) {
      _onCityChange('stProvince', 'stCity', 'stCounty');
      document.getElementById('stCounty').value = county || '';
    }
  }
}

window.onStFilterProvChange = function(){
  _onProvinceChange('stFilterProv', 'stFilterCity', 'stFilterCounty', '全部市');
  filterStModalList();
};
window.onStFilterCityChange = function(){
  _onCityChange('stFilterProv', 'stFilterCity', 'stFilterCounty');
  filterStModalList();
};

window.openStModal = function(){
  _popProvinceSelect('stProvince', '选择省');
  _popProvinceSelect('stFilterProv', '全部省');
  document.getElementById('stFilterCity').innerHTML = '<option value="">全部市</option>';
  document.getElementById('stFilterCounty').innerHTML = '<option value="">全部区/县</option>';
  document.getElementById('stModal').classList.add('show');
  _populateDeviceSelect();
  refreshStModalList();
};
window.closeStModal = function(){document.getElementById('stModal').classList.remove('show');_resetStForm()};

function _populateDeviceSelect(){
  Api.get('/api/devices').then(function(devices){
    var sel = document.getElementById('stDeviceMap');
    sel.innerHTML = '<option value="">选择设备…</option>';
    (devices||[]).forEach(function(d){
      if(!d.revoked) sel.innerHTML += '<option value="'+UI.escapeAttr(d.device_name)+'">'+UI.escapeHtml(d.device_name)+(d.station?' ('+UI.escapeHtml(d.station)+')':'')+'</option>';
    });
  }).catch(function(){});
}

window._resetStForm = function(){
  _editingStName=null;
  document.getElementById('stName').value='';document.getElementById('stName').readOnly=false;
  document.getElementById('stDeviceMap').value='';
  document.getElementById('stLocation').value='';
  _popProvinceSelect('stProvince', '选择省');
  document.getElementById('stCity').innerHTML='<option value="">选择市</option>';
  document.getElementById('stCounty').innerHTML='<option value="">选择区/县</option>';
  document.getElementById('stLat').value='';document.getElementById('stLon').value='';document.getElementById('stAlt').value='';
  document.getElementById('stFormTitle').textContent='新增站点';
  document.getElementById('stSubmitBtn').textContent='添加';
  var cancelBtn=document.getElementById('stCancelEditBtn');
  if(cancelBtn) cancelBtn.style.display='none';
};

function _stLocationLabel(s){
  var parts=[];
  if(s.province) parts.push(s.province);
  if(s.city) parts.push(s.city);
  if(s.county) parts.push(s.county);
  return parts.length?parts.join(' '):(s.location||'');
}

window.filterStModalList = function(){
  var prov=document.getElementById('stFilterProv').value;
  var city=document.getElementById('stFilterCity').value;
  var county=document.getElementById('stFilterCounty').value;
  var name=(document.getElementById('stFilterName').value||'').trim().toLowerCase();
  var filtered=_stations.filter(function(s){
    if(prov && (s.province||'')!==prov) return false;
    if(city && (s.city||'')!==city) return false;
    if(county && (s.county||'')!==county) return false;
    if(name && s.name.toLowerCase().indexOf(name)<0 && (s.location||'').toLowerCase().indexOf(name)<0) return false;
    return true;
  });
  renderStModalList(filtered);
};

function refreshStModalList(){
  Api.get('/api/stations').then(function(stations){
    _stations=stations||[];
    renderStModalList(_stations);
  }).catch(catchErr('加载站点列表失败'));
}

function renderStModalList(list){
  var div=document.getElementById('stModalList');
  if(!list.length){div.innerHTML='<div style="color:var(--muted);padding:8px;text-align:center;font-size:11px">暂无站点</div>';return}
  div.innerHTML=list.map(function(s,i){
    var loc=_stLocationLabel(s);
    return '<div class="pl-entry"><span><b>'+UI.escapeHtml(s.name)+'</b> <span style="color:var(--muted);font-size:10px">'+(loc||'')+' ('+(s.lat||0).toFixed(2)+','+(s.lon||0).toFixed(2)+')</span></span><span style="display:flex;gap:4px"><span class="pl-del" style="background:var(--blue);margin-right:2px;cursor:pointer" data-edit-st-modal="'+UI.escapeAttr(s.name)+'" title="编辑">✎</span><span class="pl-del" data-del-st-modal="'+UI.escapeAttr(s.name)+'" style="cursor:pointer">×</span></span></div>';
  }).join('');
}

window.editStation = function(name){
  var s=_stations.find(function(x){return x.name===name});
  if(!s) return;
  _editingStName=s.name;
  document.getElementById('stName').value=s.name;
  document.getElementById('stName').readOnly=true;
  document.getElementById('stDeviceMap').value=s.device_name||'';
  document.getElementById('stLocation').value=s.location||'';
  _popProvinceSelect('stProvince', '选择省');
  _setRegionValues(s.province||'', s.city||'', s.county||'');
  document.getElementById('stLat').value=s.lat||0;
  document.getElementById('stLon').value=s.lon||0;
  document.getElementById('stAlt').value=s.alt||0;
  document.getElementById('stFormTitle').textContent='编辑站点';
  document.getElementById('stSubmitBtn').textContent='保存';
  var cancelBtn=document.getElementById('stCancelEditBtn');
  if(cancelBtn) cancelBtn.style.display='inline-block';
};

window.addStation = function(){
  var data={
    name:document.getElementById('stName').value.trim(),
    device_name:document.getElementById('stDeviceMap').value.trim(),
    location:document.getElementById('stLocation').value.trim(),
    province:document.getElementById('stProvince').value.trim(),
    city:document.getElementById('stCity').value.trim(),
    county:document.getElementById('stCounty').value.trim(),
    lat:parseFloat(document.getElementById('stLat').value),
    lon:parseFloat(document.getElementById('stLon').value),
    alt:parseFloat(document.getElementById('stAlt').value)||0
  };
  if(!data.name){UI.Message.warning('请输入站点名称');return}
  if(isNaN(data.lat)||isNaN(data.lon)){UI.Message.warning('请输入有效坐标');return}
  var method=_editingStName?'PUT':'POST';
  Api[method.toLowerCase()]('/api/stations', data).then(function(res){
    if(res.error){UI.toast(res.error,'error');return}
    document.getElementById('stName').readOnly=false;
    _resetStForm();
    document.getElementById('stFilterProv').value='';document.getElementById('stFilterCity').value='';document.getElementById('stFilterName').value='';
    refreshStModalList();
    updateAll._lastStats=0;
  }).catch(catchErr((_editingStName?'编辑':'添加')+'站点失败'));
};

window.delStation = function(name){
  var s=_stations.find(function(x){return x.name===name});
  if(!s) return;
  UI.Message.confirm('确定要删除站点 '+s.name+' 吗？').then(function(ok){
    if(!ok) return;
    Api.del('/api/stations', {name:s.name}).then(function(){
      document.getElementById('stFilterProv').value='';document.getElementById('stFilterCity').value='';document.getElementById('stFilterName').value='';
      refreshStModalList();
      updateAll._lastStats=0;
    }).catch(catchErr('删除站点失败'));
  });
};

// ═══════════ User Management Modal ═══════════
var _users=[];
var _editingUsername=null;

function _populateUsrStationSelect(){
  Api.get('/api/stations').then(function(stations){
    var sel = document.getElementById('usrStation');
    sel.innerHTML = '<option value="">全部站点</option>';
    (stations||[]).forEach(function(s){
      sel.innerHTML += '<option value="'+UI.escapeAttr(s.name)+'">'+UI.escapeHtml(s.name)+(s.location?' ('+UI.escapeHtml(s.location)+')':'')+'</option>';
    });
  }).catch(function(){});
}

window.openUsrModal = function(){
  document.getElementById('usrModal').classList.add('show');
  _populateUsrStationSelect();
  refreshUsrModalList();
};
window.closeUsrModal = function(){document.getElementById('usrModal').classList.remove('show');_resetUsrForm()};

window._resetUsrForm = function(){
  _editingUsername=null;
  document.getElementById('usrName').value='';
  document.getElementById('usrName').readOnly=false;
  document.getElementById('usrPass').value='';
  document.getElementById('usrRole').value='user';
  var usrSel = document.getElementById('usrStation');
  if(usrSel) usrSel.value='';
  document.getElementById('usrFormTitle').textContent='新增用户';
  document.getElementById('usrSubmitBtn').textContent='添加';
  var cancelBtn=document.getElementById('usrCancelEditBtn');
  if(cancelBtn) cancelBtn.style.display='none';
};

function refreshUsrModalList(){
  Api.get('/api/users').then(function(users){
    _users=users||[];
    var div=document.getElementById('usrModalList');
    if(!_users.length){div.innerHTML='<div style="color:var(--muted);padding:8px;text-align:center;font-size:11px">暂无用户</div>';return}
    div.innerHTML=_users.map(function(u,i){
      var roleLabel={admin:'管理员',tenant_admin:'租户管理员',user:'操作员'}[u.role]||'操作员';
      var stationLabel=u.assigned_station||u.station||'全部站点';
      var scopeLabel=u.scope==='tenant'?'(全局)':'';
      return '<div class="pl-entry"><span><b>'+UI.escapeHtml(u.username)+'</b> <span style="color:var(--muted);font-size:10px">'+roleLabel+' · '+UI.escapeHtml(stationLabel)+' '+scopeLabel+'</span></span><span style="display:flex;gap:4px"><span class="pl-del" style="background:var(--blue);cursor:pointer" data-edit-user-modal="'+i+'" title="编辑">✎</span>'+(currentUser.role==='admin'?'<span class="pl-del" style="background:var(--blue);margin-right:2px;cursor:pointer" data-reset-pwd-modal="'+UI.escapeAttr(u.username)+'" title="重置密码">🔑</span>':'')+'<span class="pl-del" data-del-user-modal="'+UI.escapeAttr(u.username)+'" style="cursor:pointer">×</span></span></div>';
    }).join('');
  });
}

window.editUser = function(idx){
  var u=_users[idx];
  if(!u) return;
  _editingUsername=u.username;
  document.getElementById('usrName').value=u.username;
  document.getElementById('usrName').readOnly=true;
  document.getElementById('usrPass').value='';
  document.getElementById('usrPass').placeholder='留空则不改密码';
  document.getElementById('usrRole').value=u.role||'user';
  var usrSel = document.getElementById('usrStation');
  if(usrSel) usrSel.value = u.assigned_station||u.station||'';
  document.getElementById('usrFormTitle').textContent='编辑用户: '+u.username;
  document.getElementById('usrSubmitBtn').textContent='保存';
  var cancelBtn=document.getElementById('usrCancelEditBtn');
  if(cancelBtn) cancelBtn.style.display='inline-block';
};

window.addUser = function(){
  var usrSel = document.getElementById('usrStation');
  var data={
    username:document.getElementById('usrName').value,
    password:document.getElementById('usrPass').value,
    role:document.getElementById('usrRole').value,
    station: usrSel ? usrSel.value : ''
  };
  if(!data.username){UI.Message.warning('用户名不能为空');return}
  if(!_editingUsername&&!data.password){UI.Message.warning('密码不能为空');return}
  var method=_editingUsername?'PUT':'POST';
  Api[method.toLowerCase()]('/api/users', data).then(function(res){
    if(res.error){UI.toast(res.error,'error');return}
    _resetUsrForm();
    refreshUsrModalList();
  }).catch(catchErr((_editingUsername?'编辑':'添加')+'用户失败'));
};

window.delUser = function(username){
  UI.Message.confirm('确定要删除用户 '+username+' 吗？').then(function(ok){
    if(!ok) return;
    Api.del('/api/users', {username:username}).then(function(res){
      if(res.error){UI.toast(res.error,'error');return}
      refreshUsrModalList();
    }).catch(catchErr('删除用户失败'));
  });
};

// ═══════════ License Management ═══════════
window.openLicModal = function(){
  document.getElementById('licModal').classList.add('show');
  refreshLicModalList();
};
window.closeLicModal = function(){document.getElementById('licModal').classList.remove('show')};

function refreshLicModalList(){
  Api.get('/api/licenses').then(function(tenants){
    var div=document.getElementById('licModalList');
    if(!tenants||!tenants.length){div.innerHTML='<div style="color:var(--muted);padding:8px;text-align:center;font-size:11px">暂无密钥</div>';return}
    div.innerHTML=tenants.map(function(t){
      var status=t.is_active?'<span style="color:#22c55e">有效</span>':'<span style="color:#ef4444">已停用</span>';
      var actionBtn=t.is_active
        ?'<span class="pl-del" data-del-lic="'+UI.escapeAttr(String(t.id))+'" title="停用" style="cursor:pointer">×</span>'
        :'<span style="font-size:11px;color:var(--blue);cursor:pointer" data-reactivate-lic="'+UI.escapeAttr(String(t.id))+'">重新激活</span>';
      return '<div class="pl-entry"><span><b>'+UI.escapeHtml(t.name)+'</b> <code style="font-size:10px;color:var(--accent)">'+UI.escapeHtml(t.license_key)+'</code><br><span style="font-size:10px;color:var(--muted)">用户数:'+t.user_count+'/'+t.max_users+' '+status+' 联系人:'+UI.escapeHtml(t.contact||'-')+'</span></span>'+actionBtn+'</div>';
    }).join('');
  });
}

window.addLicense = function(){
  var data={
    name: document.getElementById('licName').value.trim(),
    max_users: parseInt(document.getElementById('licMaxUsers').value)||3,
    contact: document.getElementById('licContact').value.trim()
  };
  if(!data.name){UI.Message.warning('客户名称不能为空');return}
  Api.post('/api/licenses', data).then(function(res){
    if(res.error){UI.toast(res.error,'error');return}
    document.getElementById('licName').value='';
    document.getElementById('licContact').value='';
    UI.Message.success('密钥已生成: '+res.license_key);
    refreshLicModalList();
  }).catch(catchErr('创建密钥失败'));
};

window.delLicense = function(id){
  UI.Message.confirm('确定要停用该密钥吗？所有关联用户将无法操作。').then(function(ok){
    if(!ok) return;
    Api.del('/api/licenses', {id:id}).then(function(res){
      if(res.error){UI.toast(res.error,'error');return}
      refreshLicModalList();
    }).catch(catchErr('停用密钥失败'));
  });
};

window.reactivateLicense = function(id){
  UI.Message.confirm('确定要重新激活该密钥吗？').then(function(ok){
    if(!ok) return;
    Api.put('/api/licenses', {id:id, is_active:true}).then(function(res){
      if(res.error){UI.toast(res.error,'error');return}
      refreshLicModalList();
    }).catch(catchErr('激活密钥失败'));
  });
};

// ═══════════ Personnel Modal (告警联系人) ═══════════
window.openPersonnelModal = function(){
  document.getElementById('personnelModal').classList.add('show');
  Api.get('/api/stations').then(function(stations){
    var sel = document.getElementById('personnelStationSelect');
    sel.innerHTML = '<option value="">选择关联站点</option>' + stations.map(function(s){
      return '<option value="'+UI.escapeAttr(s.name)+'">'+UI.escapeHtml(s.name)+
             (s.location?' ('+UI.escapeHtml(s.location)+')':'')+'</option>';
    }).join('');
    var stName = '';
    if (currentStationDevice) {
      var sts = cachedDashboard?cachedDashboard.stations:[];
      for (var i=0;i<sts.length;i++) {
        if (sts[i].device_name===currentStationDevice || sts[i].name===currentStationDevice) {
          stName = sts[i].name; break;
        }
      }
    }
    if (!stName) stName = currentUser.assigned_station || currentUser.station || '';
    if (stName) sel.value = stName;
    refreshPersonnelList();
  }).catch(catchErr('加载站点列表失败'));
};

window.closePersonnelModal = function(){document.getElementById('personnelModal').classList.remove('show')};

function refreshPersonnelList(){
  var sel = document.getElementById('personnelStationSelect');
  var stName = sel ? sel.value : '';
  if (!stName) {
    document.getElementById('personnelList').innerHTML='<div style="color:var(--muted);padding:8px;text-align:center;font-size:11px">请选择站点</div>';
    return;
  }
  Api.get('/api/personnel?station='+encodeURIComponent(stName)).then(function(list){
    var div=document.getElementById('personnelList');
    if(!list||!list.length){div.innerHTML='<div style="color:var(--muted);padding:8px;text-align:center;font-size:11px">暂无联系人</div>';return}
    div.innerHTML=list.map(function(p){
      return '<div class="pl-entry"><span><b>'+UI.escapeHtml(p.name)+'</b> <span style="color:var(--accent);font-size:10px">'+UI.escapeHtml(p.phone)+'</span></span><span class="pl-del" data-del-personnel="'+p.id+'" style="cursor:pointer">×</span></div>';
    }).join('');
  });
}

window.addPersonnel = function(){
  var sel = document.getElementById('personnelStationSelect');
  var stName = sel ? sel.value : '';
  if (!stName) { UI.Message.warning('请选择关联站点'); return; }
  var name = document.getElementById('persName').value.trim();
  var phone = document.getElementById('persPhone').value.trim();
  if (!name || !phone) { UI.Message.warning('请填写姓名和联系电话'); return; }
  if (!/^1\d{10}$/.test(phone)) { UI.Message.warning('请输入合法的11位手机号码'); return; }

  Api.post('/api/personnel', {station_name:stName, name:name, phone:phone}).then(function(res){
    if(res.error){UI.toast(res.error,'error');return}
    document.getElementById('persName').value='';
    document.getElementById('persPhone').value='';
    refreshPersonnelList();
  }).catch(catchErr('添加联系人失败'));
};

window.delPersonnel = function(id){
  UI.Message.confirm('确定要删除该联系人吗？').then(function(ok){
    if(!ok) return;
    Api.del('/api/personnel', {id:id}).then(function(res){
      if(res.error){UI.toast(res.error,'error');return}
      refreshPersonnelList();
    }).catch(catchErr('删除联系人失败'));
  });
};

// ═══════════ Settings Modal ═══════════
window.openCfgModal = function(){
  document.getElementById('cfgModal').classList.add('show');
  Api.get('/api/settings').then(function(s){
    document.getElementById('cfgThreshWarn').value=s.threshold_warning||200;
    document.getElementById('cfgThreshSev').value=s.threshold_severe||100;
    document.getElementById('cfgThreshCrit').value=s.threshold_critical||50;
    document.getElementById('cfgFlapEn').checked=s.anti_flapping_enabled==='true';
    document.getElementById('cfgFlapIn').value=s.debounce_in||3;
    document.getElementById('cfgFlapOut').value=s.debounce_out||10;
    document.getElementById('cfgSmsEn').checked=s.sms_enabled==='true';
    document.getElementById('cfgSmsPhones').value=(s.sms_alert_phones||'').split(',').join('\n');
    document.getElementById('cfgArchiveEn').checked=s.raw_archive_enabled!=='false';
    document.getElementById('cfgRetention').value=s.raw_archive_retention_days||30;
  });
};
window.closeCfgModal = function(){document.getElementById('cfgModal').classList.remove('show')};

window.saveSettings = function(){
  var phones=document.getElementById('cfgSmsPhones').value.split('\n').map(function(s){return s.trim()}).filter(Boolean).join(',');
  var data={
    threshold_warning: String(parseFloat(document.getElementById('cfgThreshWarn').value)||200),
    threshold_severe: String(parseFloat(document.getElementById('cfgThreshSev').value)||100),
    threshold_critical: String(parseFloat(document.getElementById('cfgThreshCrit').value)||50),
    anti_flapping_enabled: document.getElementById('cfgFlapEn').checked?'true':'false',
    debounce_in: String(parseFloat(document.getElementById('cfgFlapIn').value)||3),
    debounce_out: String(parseFloat(document.getElementById('cfgFlapOut').value)||10),
    sms_enabled: document.getElementById('cfgSmsEn').checked?'true':'false',
    sms_alert_phones: phones,
    raw_archive_enabled: document.getElementById('cfgArchiveEn').checked?'true':'false',
    raw_archive_retention_days: String(parseInt(document.getElementById('cfgRetention').value)||30)
  };
  Api.put('/api/settings', data).then(function(res){
    if(res.error){UI.toast(res.error,'error');return}
    closeCfgModal();
  }).catch(catchErr('保存设置失败'));
};

// ═══════════ Alert History Modal ═══════════
window.openHistModal = function(){
  document.getElementById('histModal').classList.add('show');
  refreshHistory();
};
window.closeHistModal = function(){document.getElementById('histModal').classList.remove('show')};

window.exportAlertsCsv = function(){
  var params=new URLSearchParams();
  var lv=document.getElementById('histLevel').value; if(lv) params.set('level',lv);
  var dr=document.getElementById('histDrone').value.trim(); if(dr) params.set('drone_id',dr);
  window.open('/api/alerts/export?'+params.toString(),'_blank');
};

window.exportDronesCsv = function(){window.open('/api/drones/export','_blank')};

window.ackAlert = function(alertId, el){
  Api.post('/api/alerts/'+alertId+'/acknowledge', {note:''}).then(function(res){
    if(res.error){UI.toast(res.error,'error');return}
    el.textContent='已确认';el.style.color='var(--green)';el.style.cursor='default';
    el.onclick=null;
  }).catch(catchErr('确认告警失败'));
};

window.refreshHistory = function(){
  var level=document.getElementById('histLevel').value;
  var drone=document.getElementById('histDrone').value.trim();
  var fromDate=document.getElementById('histFrom').value;
  var toDate=document.getElementById('histTo').value;
  var params=new URLSearchParams();
  if(level) params.set('level',level);
  if(drone) params.set('drone_id',drone);
  if(fromDate) params.set('since',fromDate+'T00:00:00');
  if(toDate) params.set('to',toDate+'T23:59:59');
  params.set('limit','150');
  Api.get('/api/alerts/history?'+params.toString()).then(function(rows){
    var div=document.getElementById('histList');
    if(!rows||!rows.length){div.innerHTML='<div class="empty-state">暂无告警记录</div>';return}
    div.innerHTML=rows.map(function(r){
      var lvlColor=r.level==='critical'?'var(--red)':r.level==='severe'?'var(--orange)':'var(--yellow)';
      var ackHtml=r.acknowledged
        ?'<span style="font-size:10px;color:var(--green);min-width:64px;text-align:center" title="'+UI.escapeAttr(r.ack_by)+' '+r.ack_time.substring(0,16)+'">已确认</span>'
        :'<span style="font-size:10px;color:var(--muted);min-width:64px;text-align:center;cursor:pointer" data-ack-alert="'+r.id+'">确认</span>';
      return '<div style="display:flex;align-items:center;gap:8px;padding:5px 10px;border-bottom:1px solid #f3f4f6;font-size:11px">'
        +'<span style="width:7px;height:7px;border-radius:50%;background:'+lvlColor+';flex-shrink:0"></span>'
        +'<span style="font-weight:600;min-width:44px;color:'+lvlColor+'">'+(r.level==='critical'?'危险':r.level==='severe'?'严重':'警告')+'</span>'
        +'<span style="font-family:monospace;font-size:10px;min-width:90px">'+UI.escapeHtml(r.drone_id.substring(0,12))+'</span>'
        +'<span style="color:var(--muted);min-width:70px">'+UI.escapeHtml(r.line_name||'')+'</span>'
        +'<span style="font-weight:600;min-width:44px;text-align:right">'+(r.distance!=null?r.distance.toFixed(0)+'m':'')+'</span>'
        +'<span style="color:var(--muted);text-align:right;flex:1">'+r.timestamp.substring(0,16)+'</span>'
        +ackHtml
        +'</div>';
    }).join('');
  }).catch(catchErr('加载告警历史失败'));
};

// ═══════════ Alert Sound (delegated to UI module) ═══════════
var playAlertBeep = UI.beep;

window.importPowerLinesCsv = function(){
  var csvText=document.getElementById('plCsv').value.trim();
  if(!csvText){UI.Message.warning('请粘贴 CSV 内容');return}
  Api.post('/api/powerlines/import', {csv:csvText}).then(function(res){
    if(res.error){UI.toast(res.error,'error');return}
    UI.toast('成功导入 '+res.imported+' 条电力线', 'ok');
    document.getElementById('plCsv').value='';
    refreshPlModalList();
    loadPowerLines();
  }).catch(catchErr('导入电力线失败'));
};

// ═══════════ Audit log viewer ═══════════
window.openAudModal = function(){
  document.getElementById('audModal').style.display='flex';
  refreshAudit();
};
window.closeAudModal = function(){
  document.getElementById('audModal').style.display='none';
};
function refreshAudit(){
  var list=document.getElementById('audList');
  list.innerHTML='<div class="empty-state">加载中...</div>';
  Api.get('/api/audit?limit=100').then(function(rows){
    if(!rows.length){list.innerHTML='<div class="empty-state">暂无操作记录</div>';return}
    var html='<table style="width:100%;font-size:11px;border-collapse:collapse">';
    html+='<thead><tr style="border-bottom:1px solid var(--border);color:var(--muted);text-align:left">';
    html+='<th style="padding:6px 4px">时间</th><th style="padding:6px 4px">操作</th><th style="padding:6px 4px">对象</th><th style="padding:6px 4px">操作者</th></tr></thead><tbody>';
    rows.forEach(function(r){
      html+='<tr style="border-bottom:1px solid var(--border)">';
      html+='<td style="padding:6px 4px">'+UI.escapeHtml(r.timestamp)+'</td>';
      html+='<td style="padding:6px 4px">'+UI.escapeHtml(r.operation)+'</td>';
      html+='<td style="padding:6px 4px">'+UI.escapeHtml(r.table_name||'')+(r.record_id?' #'+r.record_id:'')+'</td>';
      html+='<td style="padding:6px 4px">'+UI.escapeHtml(r.username)+'</td>';
      html+='</tr>';
      if(r.detail){
        html+='<tr style="border-bottom:1px solid var(--border);background:var(--surface2)"><td colspan="4" style="padding:4px 8px;font-size:10px;color:var(--muted)">'+UI.escapeHtml(r.detail)+'</td></tr>';
      }
    });
    html+='</tbody></table>';
    list.innerHTML=html;
  }).catch(catchErr('加载审计日志失败'));
}

// ═══════════ Change Password ═══════════
window.openPwdModal = function(){document.getElementById('pwdModal').style.display='flex'};
window.closePwdModal = function(){document.getElementById('pwdModal').style.display='none';document.getElementById('oldPassword').value='';document.getElementById('newPassword').value=''};
window.changePassword = function(){
  var oldPw=document.getElementById('oldPassword').value;
  var newPw=document.getElementById('newPassword').value.trim();
  if(!oldPw||!newPw){showToast('请填写原密码和新密码','warn');return}
  if(newPw.length<6){showToast('新密码至少6位','warn');return}
  Api.put('/api/password', {old_password:oldPw,new_password:newPw}).then(function(res){
    if(res.error){showToast(res.error,'error');return}
    showToast('密码修改成功','ok');
    closePwdModal();
  }).catch(catchErr('修改密码失败'));
};

// ═══════════ Admin Reset User Password ═══════════
window.resetUserPassword = function(username){
  var newPw=prompt('为 '+username+' 设置新密码 (至少6位):');
  if(!newPw||newPw.length<6){showToast('密码至少6位','warn');return}
  Api.post('/api/users/'+encodeURIComponent(username)+'/reset-password', {new_password:newPw}).then(function(res){
    if(res.error){showToast(res.error,'error');return}
    showToast('密码已重置','ok');
  }).catch(catchErr('重置密码失败'));
};

// ═══════════ Tenant Info ═══════════
function refreshTenantInfo(){
  var sec=document.getElementById('tenantInfoSection');
  if(currentUser.role!=='tenant_admin'&&currentUser.role!=='user'){sec.style.display='none';return}
  if(!currentUser.tenant_id){sec.style.display='none';return}
  Api.get('/api/tenant/info').then(function(t){
    if(!t){sec.style.display='none';return}
    sec.style.display='block';
    var html='<div style="margin-bottom:4px"><b>'+UI.escapeHtml(t.name)+'</b></div>';
    html+='<div>用户: '+t.current_users+'/'+t.max_users+'</div>';
    if(t.license_key) html+='<div style="font-size:10px;color:var(--blue);margin-top:2px">密钥: '+UI.escapeHtml(t.license_key)+'</div>';
    if(t.stations&&t.stations.length){
      html+='<div style="margin-top:4px">站点: '+t.stations.map(function(s){return UI.escapeHtml(s.name)}).join(', ')+'</div>';
    }
    document.getElementById('tenantInfoContent').innerHTML=html;
  }).catch(function(){sec.style.display='none'});
}

// ═══════════ RBAC: hide admin-only buttons for non-admin ═══════════
function applyRBACVisibility(){
  var isAdmin=currentUser.role==='admin';
  var isTenantAdmin=currentUser.role==='tenant_admin';
  document.querySelectorAll('#addStationBtn,#stMgrBtn,#stMgrBtn2,#usrMgrBtn').forEach(function(b){
    b.style.display=(isAdmin||isTenantAdmin)?'inline-block':'none';
  });
  document.getElementById('cfgMgrBtn').style.display=isAdmin?'inline-block':'none';
  document.getElementById('licMgrBtn').style.display=isAdmin?'inline-block':'none';
  var audBtn=document.querySelector('[onclick="openAudModal()"]');
  if(audBtn) audBtn.style.display=isAdmin?'inline-block':'none';
  refreshTenantInfo();
}

// ═══════════ Station lock for station_user ═══════════
window.returnToNational = function(){
  if(currentUser.role==='user' && currentUser.scope==='station'){
    if(cachedDashboard) enterStationView(cachedDashboard.station);
    return;
  }
  if(currentUser.role!=='admin' && currentUser.role!=='tenant_admin'){
    if(cachedDashboard) enterStationView(cachedDashboard.station);
    return;
  }
  nationalMode = true;
  currentStationDevice = null;
  document.body.className = 'nat-mode';
  Object.keys(droneMarkers).forEach(function(k){map.removeLayer(droneMarkers[k]);});
  droneMarkers = {};
  Object.keys(trajPolylines).forEach(function(k){removeTrajectory(k);});
  plPolylines.forEach(function(p){map.removeLayer(p)});
  plLabels.forEach(function(l){map.removeLayer(l)});
  plPolylines = []; plLabels = [];
  clearBufferZones(); bufZonesVisible = false;
  var btn=document.getElementById('bufToggleBtn');
  btn.textContent='显示阈值圈'; btn.style.background='';
  activeTrajDrone = null;
  map.flyTo([35, 105], 4.5, {duration:.8});
  if(cachedDashboard) updateStationMarkers(cachedDashboard.stations);
};

// ═══════════ Buffer zone toggle ═══════════
window.toggleBufferZones = function(){
  bufZonesVisible=!bufZonesVisible;
  var btn=document.getElementById('bufToggleBtn');
  if(bufZonesVisible){
    btn.textContent='隐藏阈值圈';
    btn.style.background='var(--blue-bg)';
    buildBufferZones();
  }else{
    btn.textContent='显示阈值圈';
    btn.style.background='';
    clearBufferZones();
  }
};

function clearBufferZones(){
  bufZoneLayers.forEach(function(l){map.removeLayer(l)});
  bufZoneLayers=[];
}

function buildBufferZones(){
  clearBufferZones();
  Api.get('/api/powerlines').then(function(lines){
    lines.forEach(function(l){
      var latlngs=[[l.lat1,l.lon1],[l.lat2,l.lon2]];
      var crit=L.polyline(latlngs,{color:'rgba(220,38,38,0.10)', weight:8, opacity:1, smoothFactor:1, interactive:false}).addTo(map);
      bufZoneLayers.push(crit);
      var sev=L.polyline(latlngs,{color:'rgba(234,88,12,0.06)', weight:18, opacity:1, smoothFactor:1, interactive:false}).addTo(map);
      bufZoneLayers.push(sev);
      var warn=L.polyline(latlngs,{color:'rgba(202,138,4,0.04)', weight:34, opacity:1, smoothFactor:1, interactive:false}).addTo(map);
      bufZoneLayers.push(warn);
    });
  }).catch(catchErr('加载电力线失败'));
}

// Event delegation: click outside modal to close, modal action buttons
document.addEventListener('click', function(e) {
  var modalCloseMap = {
    'plModal': closePlModal, 'stModal': closeStModal, 'usrModal': closeUsrModal,
    'cfgModal': closeCfgModal, 'histModal': closeHistModal, 'audModal': closeAudModal,
    'personnelModal': closePersonnelModal, 'pwdModal': closePwdModal, 'licModal': closeLicModal
  };
  if (e.target.id && modalCloseMap[e.target.id]) {
    modalCloseMap[e.target.id]();
    return;
  }
  // Handle data-action buttons via delegation (trajectory toggle in popups)
  var actionBtn = e.target.closest('[data-toggle-traj]');
  if (actionBtn) {
    var droneId = actionBtn.dataset.toggleTraj;
    if (droneId) toggleTrajectory(droneId);
  }
});

// ═══════════ Main update loop (with AbortController) ═══════════
var prevAlertLevels={};
var _updateAllCtrl = null;
var _pollFallbackCtrl = null;

window.updateAll = function(){
  if(_updateAllCtrl) _updateAllCtrl.abort();
  _updateAllCtrl = new AbortController();
  fetch('/api/status', {signal: _updateAllCtrl.signal}).then(function(r){return r.json()}).then(function(d){
    if(d.current_user){
      currentUser=d.current_user;
      var roleLabels={admin:'管理员',tenant_admin:'租户管理员',user:'操作员'};
      document.getElementById('userBadge').textContent=currentUser.username+' ('+(roleLabels[currentUser.role]||'操作员')+')';
    }

    cachedDrones=d.drones;

    var warn=0,sev=0,crit=0;
    d.drones.forEach(function(dr){var s=dr.status;if(s==='warning')warn++;if(s==='severe')sev++;if(s==='critical')crit++;});

    animateEl(document.getElementById('qsDrones'),d.drones.length);
    animateEl(document.getElementById('qsWarn'),warn);
    animateEl(document.getElementById('qsSev'),sev);
    animateEl(document.getElementById('qsCrit'),crit);

    if(nationalMode){
      if(cachedDashboard){
        animateEl(document.getElementById('btNatStations'),(cachedDashboard.stations||[]).length);
      }
      animateEl(document.getElementById('btNatDrones'),d.drones.length);
      animateEl(document.getElementById('btNatAlerts'),warn+sev+crit);
      document.getElementById('footerNatTime').textContent='更新 '+(d.server_time||d.now);
      updateComms(d.backhaul, 'commNat');
      renderNationalAlerts(d.drones);

      d.drones.forEach(function(dr){
        var prev=prevAlertLevels[dr.id];
        var cur=dr.status||'active';
        if(cur!==prev&&(cur==='critical'||cur==='severe')){
          playAlertBeep(cur);
          if(window.Notification&&Notification.permission==='granted'){
            new Notification('['+(cur==='critical'?'危险':'严重')+'] '+dr.id,{body:'距离 '+(dr.nearest_line||dr.line_name||'?')+' '+(dr.min_distance||0).toFixed(0)+'m',tag:dr.id});
          }
        }
        prevAlertLevels[dr.id]=cur;
      });
    }else{
      var stationDrones = d.drones;
      if(currentStationDevice){
        stationDrones = d.drones.filter(function(dr){return (dr.device_name||dr.device||'')===currentStationDevice});
      }
      warn=0;sev=0;crit=0;
      stationDrones.forEach(function(dr){var s=dr.status;if(s==='warning')warn++;if(s==='severe')sev++;if(s==='critical')crit++;});
      animateEl(document.getElementById('btTotal'),stationDrones.length);
      animateEl(document.getElementById('btWarn'),warn);
      animateEl(document.getElementById('btSev'),sev);
      animateEl(document.getElementById('btCrit'),crit);
      document.getElementById('droneCountPill').textContent=stationDrones.length;
      document.getElementById('footerTime').textContent='更新 '+(d.server_time||d.now);
      updateComms(d.backhaul, 'comm');

      stationDrones.forEach(function(dr){
        var prev=prevAlertLevels[dr.id];
        var cur=dr.status||'active';
        if(cur!==prev&&(cur==='critical'||cur==='severe')){
          playAlertBeep(cur);
          if(window.Notification&&Notification.permission==='granted'){
            new Notification('['+(cur==='critical'?'危险':'严重')+'] '+dr.id,{body:'距离 '+(dr.nearest_line||dr.line_name||'?')+' '+(dr.min_distance||0).toFixed(0)+'m',tag:dr.id});
          }
        }
        prevAlertLevels[dr.id]=cur;
      });

      var seen={};
      stationDrones.forEach(function(dr){
        if(dr.last_lat==null||dr.last_lon==null)return;
        var id=dr.id||'?';seen[id]=true;
        var lat=dr.last_lat,lon=dr.last_lon;
        var s=dr.status||'active';
        var color=markerColor(s),radius=markerRadius(s);
        if(droneMarkers[id]){
          droneMarkers[id].setLatLng([lat,lon]);
          droneMarkers[id].setStyle({color:color,fillColor:color});
          droneMarkers[id].setRadius(radius);
          droneMarkers[id].unbindPopup();
          droneMarkers[id].bindPopup(popupContent(dr));
        }else{
          var m=L.circleMarker([lat,lon],{radius:radius,color:color,fillColor:color,fillOpacity:.55,weight:2.5}).addTo(map);
          m.bindPopup(popupContent(dr));droneMarkers[id]=m;
        }
      });
      Object.keys(droneMarkers).forEach(function(k){if(!seen[k]){map.removeLayer(droneMarkers[k]);delete droneMarkers[k]}});
      Object.keys(trajPolylines).forEach(function(k){if(!seen[k]){removeTrajectory(k);if(activeTrajDrone===k) activeTrajDrone=null}});

      if(activeTrajDrone&&seen[activeTrajDrone]){
        var trajId=activeTrajDrone;
        removeTrajectory(trajId);
        activeTrajDrone=null;
        showTrajectory(trajId);
      }

      renderDroneList();
      renderStationAlerts(stationDrones);
    }
  }).catch(function(e){
    if(e.name!=='AbortError') console.warn('updateAll error:', e);
  });

  if(!updateAll._lastStats||Date.now()-updateAll._lastStats>30000){
    updateAll._lastStats=Date.now();
    fetch('/api/stats/dashboard').then(function(r){return r.json()}).then(function(s){
      if(s.error) return;
      cachedDashboard=s;

      buildStationGrid('natStationGrid', s.station_list||[s.station]);
      buildStationGrid('staStationGrid', s.station);

      renderNationalStationCards(s.stations);
      updateStationMarkers(s.stations);

      applyRBACVisibility();

      if(!updateAll._userInit && currentUser.username){
        updateAll._userInit=true;
        if(currentUser.role==='user' && currentUser.scope==='station'){
          var stName = currentUser.assigned_station || currentUser.station;
          var userStation = (s.stations||[]).find(function(st){return st.name===stName}) || s.station;
          enterStationView(userStation);
          var natBtns=document.querySelectorAll('.nat-only');
          natBtns.forEach(function(b){b.style.display='none'});
        }
      }

      if(nationalMode){
        natChart = buildAlertChart('natAlertChart', natChart, s.hourly_alerts, false);
      }else{
        staChart = buildAlertChart('staAlertChart', staChart, s.hourly_alerts, false);
        if(s.model_dist) buildModelBars(s.model_dist);
      }
    });
  }
};
updateAll._userInit=false;
updateAll._lastStats=0;

// ═══════════ Notification permission ═══════════
document.addEventListener('click',function f(){
  if(window.Notification&&Notification.permission==='default') Notification.requestPermission();
},{once:true});

// ═══════════ WebSocket real-time push (with polling fallback) ═══════════
var socket=null;
var wsEnabled=false;

function initSocket(){
  socket=io({transports:['websocket','polling'],reconnectionDelay:3000,reconnectionDelayMax:10000});
  socket.on('connect',function(){
    wsEnabled=true;
    console.log('WS connected');
  });
  socket.on('disconnect',function(){
    wsEnabled=false;
    console.log('WS disconnected, fallback to polling');
  });
  socket.on('drone_update',function(d){
    if(!d||!d.drone_id) return;
    var found=false;
    if(!cachedDrones) cachedDrones=[];
    for(var i=0;i<cachedDrones.length;i++){
      if(cachedDrones[i].id===d.drone_id){
        cachedDrones[i].last_lat=d.lat;
        cachedDrones[i].last_lon=d.lon;
        cachedDrones[i].last_alt=d.alt;
        cachedDrones[i].min_distance=d.distance;
        cachedDrones[i].nearest_line=d.nearest_line;
        cachedDrones[i].status=d.status;
        found=true; break;
      }
    }
    if(!found){
      cachedDrones.push({id:d.drone_id,last_lat:d.lat,last_lon:d.lon,last_alt:d.alt,min_distance:d.distance,nearest_line:d.nearest_line,status:d.status});
    }
    var key='_ws_upd_'+d.drone_id;
    if(!updateAll[key]||Date.now()-updateAll[key]>1000){
      updateAll[key]=Date.now();
      updateMarkersFromCache();
    }
  });
  socket.on('alert_update',function(a){
    if(!a) return;
    if(a.level==='critical'||a.level==='severe'){
      playAlertBeep(a.level);
      if(window.Notification&&Notification.permission==='granted'){
        new Notification('['+(a.level==='critical'?'危险':'严重')+'] '+a.drone_id,{body:'距离 '+a.line_name+' '+a.distance.toFixed(0)+'m',tag:a.drone_id});
      }
    }
  });
}

function updateMarkersFromCache(){
  var d=cachedDrones||[];
  if(currentStationDevice){
    d=d.filter(function(dr){return (dr.device_name||dr.device||'')===currentStationDevice});
  }
  var seen={};
  d.forEach(function(dr){
    if(dr.last_lat==null||dr.last_lon==null)return;
    var id=dr.id||'?';seen[id]=true;
    var lat=dr.last_lat,lon=dr.last_lon;
    var s=dr.status||'active';
    var color=markerColor(s),radius=markerRadius(s);
    if(droneMarkers[id]){
      droneMarkers[id].setLatLng([lat,lon]);
      droneMarkers[id].setStyle({color:color,fillColor:color});
      droneMarkers[id].setRadius(radius);
      droneMarkers[id].unbindPopup();
      droneMarkers[id].bindPopup(popupContent(dr));
    }else{
      var m=L.circleMarker([lat,lon],{radius:radius,color:color,fillColor:color,fillOpacity:.55,weight:2.5}).addTo(map);
      m.bindPopup(popupContent(dr));droneMarkers[id]=m;
    }
  });
  Object.keys(droneMarkers).forEach(function(k){if(!seen[k]){map.removeLayer(droneMarkers[k]);delete droneMarkers[k]}});
}

function pollFallback(){
  if(wsEnabled){
    if(_pollFallbackCtrl) _pollFallbackCtrl.abort();
    _pollFallbackCtrl = new AbortController();
    fetch('/api/status', {signal: _pollFallbackCtrl.signal}).then(function(r){return r.json()}).then(function(d){
      var drones = cachedDrones || [];
      if(currentStationDevice){
        drones = drones.filter(function(dr){return (dr.device_name||dr.device||'')===currentStationDevice});
      }
      var warn=0,sev=0,crit=0;
      drones.forEach(function(dr){var s=dr.status;if(s==='warning')warn++;if(s==='severe')sev++;if(s==='critical')crit++;});
      if(nationalMode){
        animateEl(document.getElementById('btNatDrones'),drones.length);
        animateEl(document.getElementById('btNatAlerts'),warn+sev+crit);
        if(cachedDashboard){
          animateEl(document.getElementById('btNatStations'),(cachedDashboard.stations||[]).length);
        }
        document.getElementById('footerNatTime').textContent='更新 '+(d.server_time||d.now)+' [WS]';
        updateComms(d.backhaul,'commNat');
      }else{
        animateEl(document.getElementById('btTotal'),drones.length);
        animateEl(document.getElementById('btWarn'),warn);
        animateEl(document.getElementById('btSev'),sev);
        animateEl(document.getElementById('btCrit'),crit);
        document.getElementById('droneCountPill').textContent=drones.length;
        document.getElementById('footerTime').textContent='更新 '+(d.server_time||d.now)+' [WS]';
        updateComms(d.backhaul,'comm');
      }
      animateEl(document.getElementById('qsDrones'),drones.length);
      animateEl(document.getElementById('qsWarn'),warn);
      animateEl(document.getElementById('qsSev'),sev);
      animateEl(document.getElementById('qsCrit'),crit);
      if(nationalMode){
        renderNationalAlerts(drones);
      }else{
        renderStationAlerts(drones);
      }
    }).catch(function(e){
      if(e.name!=='AbortError') console.warn('pollFallback error:', e);
    });
  }else{
    updateAll();
  }
}

function schedulePoll(){
  pollFallback();
  setTimeout(schedulePoll, wsEnabled ? 5000 : 2000);
}

// ═══════════ Event Delegation for data-* buttons ═══════════
(function setupDelegation() {
  // Region cascade: stProvince → stCity
  document.addEventListener('change', function(e){
    var t=e.target;
    if(t.id==='stProvince'){ _onProvinceChange('stProvince','stCity','stCounty','选择市'); }
    else if(t.id==='stCity'){ _onCityChange('stProvince','stCity','stCounty'); }
    else if(t.id==='stFilterCounty'){ filterStModalList(); }
  });
  // Station cards: enter station view
  UI.delegate(document.getElementById('natStationList'), 'click', '[data-enter-station]', function() {
    var idx = parseInt(this.dataset.enterStation);
    if (cachedDashboard && cachedDashboard.stations && cachedDashboard.stations[idx]) {
      enterStationView(cachedDashboard.stations[idx]);
    }
  });
  // Power line modal
  UI.delegate(document.getElementById('plModalList'), 'click', '[data-edit-pl-modal]', function() {
    editPowerLine(parseInt(this.dataset.editPlModal));
  });
  UI.delegate(document.getElementById('plModalList'), 'click', '[data-del-pl-modal]', function() {
    delPowerLine(parseInt(this.dataset.delPlModal));
  });
  UI.delegate(document.getElementById('plModalList'), 'click', '[data-save-pl]', function() {
    savePowerLine(parseInt(this.dataset.savePl));
  });
  UI.delegate(document.getElementById('plModalList'), 'click', '[data-cancel-edit-pl]', function() {
    cancelEditPl();
  });
  // Station modal
  UI.delegate(document.getElementById('stModalList'), 'click', '[data-edit-st-modal]', function() {
    editStation(this.dataset.editStModal);
  });
  UI.delegate(document.getElementById('stModalList'), 'click', '[data-del-st-modal]', function() {
    delStation(this.dataset.delStModal);
  });
  // User modal
  UI.delegate(document.getElementById('usrModalList'), 'click', '[data-edit-user-modal]', function() {
    editUser(parseInt(this.dataset.editUserModal));
  });
  UI.delegate(document.getElementById('usrModalList'), 'click', '[data-reset-pwd-modal]', function() {
    resetUserPassword(this.dataset.resetPwdModal);
  });
  UI.delegate(document.getElementById('usrModalList'), 'click', '[data-del-user-modal]', function() {
    delUser(this.dataset.delUserModal);
  });
  // License modal
  UI.delegate(document.getElementById('licModalList'), 'click', '[data-del-lic]', function() {
    delLicense(parseInt(this.dataset.delLic));
  });
  UI.delegate(document.getElementById('licModalList'), 'click', '[data-reactivate-lic]', function() {
    reactivateLicense(parseInt(this.dataset.reactivateLic));
  });
  // Personnel modal
  UI.delegate(document.getElementById('personnelList'), 'click', '[data-del-personnel]', function() {
    delPersonnel(parseInt(this.dataset.delPersonnel));
  });
  // Alert history: acknowledge
  UI.delegate(document.getElementById('histList'), 'click', '[data-ack-alert]', function() {
    ackAlert(parseInt(this.dataset.ackAlert), this);
  });
})();

// ═══════════ Init ═══════════
initSocket();
updateAll();
schedulePoll();
setInterval(function(){if(!nationalMode) loadPowerLines();},60000);
