"""GET /, GET /map — 监测看板"""

from flask import Blueprint, render_template_string

bp = Blueprint("dashboard", __name__)

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Drone RID 中心监测</title>
<style>
:root{
  --bg:#0d1117;--surface:#161b22;--border:#30363d;
  --text:#c9d1d9;--muted:#8b949e;
  --blue:#58a6ff;--green:#3fb950;--yellow:#d2991d;--orange:#db6d28;--red:#f85149;
  --radius:8px;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);min-height:100vh;font-size:13px;line-height:1.5}

.topbar{
  background:var(--surface);border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  padding:0 20px;height:48px;
}
.topbar .brand{font-size:15px;font-weight:600}
.topbar .brand span{color:var(--muted);font-weight:400;font-size:12px;margin-left:8px}
.topbar .actions{display:flex;align-items:center;gap:10px}
.status-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
.status-dot.online{background:var(--green)}
.status-dot.offline{background:var(--red)}

.stats{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;padding:14px 20px}
.stat-card{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:12px 16px;
}
.stat-card .label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.stat-card .value{font-size:24px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.stat-card .sub{font-size:10px;color:var(--muted);margin-top:2px}
.val-critical{color:var(--red)}
.val-severe{color:var(--orange)}
.val-warning{color:var(--yellow)}
.val-info{color:var(--blue)}
.val-ok{color:var(--green)}

.content{padding:0 20px 16px;display:grid;grid-template-columns:1fr 1fr;gap:12px;height:calc(100vh - 230px)}
.panel{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  display:flex;flex-direction:column;overflow:hidden;
}
.panel-header{
  padding:10px 14px;font-size:12px;font-weight:600;color:var(--muted);
  border-bottom:1px solid var(--border);display:flex;justify-content:space-between
}
.panel-body{flex:1;overflow-y:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
thead{position:sticky;top:0;z-index:1}
th{background:var(--surface);color:var(--muted);font-size:10px;font-weight:500;text-align:left;padding:7px 10px;border-bottom:1px solid var(--border);text-transform:uppercase;letter-spacing:.5px}
td{padding:6px 10px;border-bottom:1px solid rgba(48,54,61,.5);white-space:nowrap}
tr:hover td{background:rgba(255,255,255,.02)}
tr:last-child td{border-bottom:none}
.tag{font-size:10px;font-weight:600;padding:1px 6px;border-radius:3px}
.tag-active{color:var(--green);background:rgba(63,185,80,.15)}
.tag-warning{color:var(--yellow);background:rgba(210,153,29,.15)}
.tag-severe{color:var(--orange);background:rgba(219,109,40,.15)}
.tag-critical{color:var(--red);background:rgba(248,81,73,.15)}
.tag-offline{color:var(--muted);background:rgba(139,148,158,.1)}
.tag-online{color:var(--green);background:rgba(63,185,80,.15)}
.mono{font-family:"SF Mono",Consolas,monospace;font-variant-numeric:tabular-nums}

.content.single{grid-template-columns:1fr}

.empty{color:var(--muted);text-align:center;padding:40px 0;font-size:13px}

.footer{
  position:fixed;bottom:0;left:0;right:0;height:28px;
  background:var(--surface);border-top:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  padding:0 20px;font-size:10px;color:var(--muted);
}
</style>
</head>
<body>

<div class="topbar">
  <div class="brand">Drone RID 中心监测 <span>v1.0 — 电力线防碰撞多杆塔聚合</span></div>
  <div class="actions">
    <a href="/map" style="color:var(--blue);font-size:12px;text-decoration:none">地图视图</a>
  </div>
</div>

<div class="stats">
  <div class="stat-card"><div class="label">杆塔设备</div><div class="value val-info" id="sDevTotal">0</div><div class="sub">在线 <span id="sDevOnline">0</span></div></div>
  <div class="stat-card"><div class="label">活跃无人机</div><div class="value val-ok" id="sDrones">0</div><div class="sub">所有杆塔</div></div>
  <div class="stat-card"><div class="label">危险</div><div class="value val-critical" id="sCrit">0</div><div class="sub">&le;50m</div></div>
  <div class="stat-card"><div class="label">严重</div><div class="value val-severe" id="sSev">0</div><div class="sub">&le;100m</div></div>
  <div class="stat-card"><div class="label">警告</div><div class="value val-warning" id="sWarn">0</div><div class="sub">&le;200m</div></div>
  <div class="stat-card"><div class="label">告警总计</div><div class="value val-info" id="sTotalAlerts">0</div><div class="sub">近50条</div></div>
</div>

<div class="content">
  <div class="panel">
    <div class="panel-header"><span>杆塔设备</span><span id="devCount">--</span></div>
    <div class="panel-body">
      <table><thead><tr><th>设备名</th><th>位置</th><th>坐标</th><th>最后心跳</th><th>状态</th></tr></thead>
      <tbody id="devTable"></tbody></table>
      <div class="empty" id="devEmpty" style="display:none">等待设备上线...</div>
    </div>
  </div>
  <div class="panel">
    <div class="panel-header"><span>告警日志</span><span id="alertCount">--</span></div>
    <div class="panel-body" id="alertPanel"><div class="empty">暂无告警</div></div>
  </div>
</div>

<div class="panel" style="position:fixed;left:20px;right:20px;bottom:36px;height:200px;border:1px solid var(--border)">
  <div class="panel-header"><span>无人机列表 (所有杆塔)</span><span id="droneCount">--</span></div>
  <div class="panel-body">
    <table><thead><tr><th>无人机ID</th><th>来源设备</th><th>纬度</th><th>经度</th><th>高度</th><th>距离</th><th>最近电力线</th><th>状态</th><th>更新</th></tr></thead>
    <tbody id="droneTable"></tbody></table>
    <div class="empty" id="droneEmpty" style="display:none">等待无人机数据...</div>
  </div>
</div>

<div class="footer">
  <span id="footerLeft">中心服务器 v2.0</span>
  <span id="footerTime">--</span>
</div>

<script>
function update(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    let devs=d.devices;
    document.getElementById('sDevTotal').textContent=devs.total;
    document.getElementById('sDevOnline').textContent=devs.online;
    document.getElementById('sDrones').textContent=d.drones.total;
    document.getElementById('sCrit').textContent=d.drones.critical;
    document.getElementById('sSev').textContent=d.drones.severe;
    document.getElementById('sWarn').textContent=d.drones.warning;
    document.getElementById('sTotalAlerts').textContent=d.alerts.length;
    document.getElementById('devCount').textContent=devs.total+' 台';
    document.getElementById('alertCount').textContent=d.alerts.length+' 条';
    document.getElementById('droneCount').textContent=d.drones.total+' 架';
    document.getElementById('footerTime').textContent=d.server_time;

    let dt=document.getElementById('devTable');
    let de=document.getElementById('devEmpty');
    if(devs.list.length===0){dt.innerHTML='';de.style.display='block';}
    else{de.style.display='none';
      dt.innerHTML=devs.list.map(d=>{
        let s=d.status==='online'?'tag-online':'tag-offline';
        let t=d.last_seen?d.last_seen.substring(11,19):'--';
        return `<tr><td><b>${d.name}</b></td><td>${d.location||'--'}</td><td class="mono">${d.lat.toFixed(4)},${d.lon.toFixed(4)}</td><td class="mono">${t}</td><td><span class="tag ${s}">${d.status==='online'?'在线':'离线'}</span></td></tr>`;
      }).join('');
    }

    let drt=document.getElementById('droneTable');
    let dr_empty=document.getElementById('droneEmpty');
    if(d.drones.list.length===0){drt.innerHTML='';dr_empty.style.display='block';}
    else{dr_empty.style.display='none';
      let sc={'active':'tag-active','warning':'tag-warning','severe':'tag-severe','critical':'tag-critical'};
      let st={'active':'正常','warning':'警告','severe':'严重','critical':'危险'};
      drt.innerHTML=d.drones.list.map(dr=>{
        let s=dr.status||'active';let dist=dr.min_distance!=null?dr.min_distance.toFixed(0)+'m':'-';
        let t=dr.last_seen?dr.last_seen.substring(11,19):'--';
        return `<tr><td class="mono">${dr.id}</td><td>${dr.device_name}</td>
          <td class="mono">${(dr.last_lat||0).toFixed(5)}</td><td class="mono">${(dr.last_lon||0).toFixed(5)}</td>
          <td>${(dr.last_alt||0).toFixed(0)}m</td><td class="mono">${dist}</td>
          <td>${dr.nearest_line||'-'}</td><td><span class="tag ${sc[s]||'tag-active'}">${st[s]||s}</span></td>
          <td class="mono">${t}</td></tr>`;
      }).join('');
    }

    let ap=document.getElementById('alertPanel');
    if(d.alerts.length===0){ap.innerHTML='<div class="empty">暂无告警</div>';}
    else{
      let bg={'critical':'rgba(248,81,73,.08)','severe':'rgba(219,109,40,.06)','warning':'rgba(210,153,29,.06)'};
      ap.innerHTML=d.alerts.map(a=>{
        return `<div style="padding:7px 12px;font-size:12px;border-bottom:1px solid rgba(48,54,61,.4);background:${bg[a.level]||'transparent'};border-left:3px solid var(--${a.level==='critical'?'red':a.level==='severe'?'orange':'yellow'})">
          <span style="color:var(--muted);font-size:10px">${a.time}</span>
          <b style="color:var(--blue)">${a.device}</b>
          无人机 <b>${a.drone}</b> 接近 <b>${a.line}</b> 距离 <b style="color:var(--${a.level==='critical'?'red':a.level==='severe'?'orange':'yellow'})">${a.distance.toFixed(0)}m</b>
          <span class="tag tag-${a.level}">${a.level}</span>
        </div>`;
      }).join('');
    }
  });
}
update();setInterval(update,2000);
</script>
</body>
</html>
"""

MAP_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>中心监测地图</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;font-size:13px}
#map{width:100%;height:100%;background:#1a1a2e}
#panel{
  position:fixed;top:12px;right:12px;z-index:1000;width:320px;max-height:calc(100vh - 24px);
  background:rgba(22,27,34,.94);backdrop-filter:blur(8px);border:1px solid #30363d;
  border-radius:8px;display:flex;flex-direction:column;overflow:hidden;
}
#panel .head{padding:12px 14px;border-bottom:1px solid #30363d;font-weight:600;font-size:13px;color:#c9d1d9}
#panel .body{flex:1;overflow-y:auto;padding:8px 0}
.device-row{padding:8px 14px;border-bottom:1px solid rgba(48,54,61,.4);cursor:pointer}
.device-row:hover{background:rgba(255,255,255,.03)}
.device-row .name{font-weight:600;color:#c9d1d9;font-size:12px}
.device-row .meta{font-size:10px;color:#8b949e;margin-top:2px}
.device-row .dot{width:6px;height:6px;border-radius:50%;display:inline-block;margin-right:4px}
.device-row .dot.online{background:#3fb950}
.device-row .dot.offline{background:#f85149}
#topControls{position:fixed;top:12px;left:12px;z-index:1000}
.btn-map{
  padding:6px 14px;border-radius:6px;font-size:12px;cursor:pointer;
  border:1px solid #30363d;background:rgba(22,27,34,.94);color:#c9d1d9;
}
.leaflet-popup-content{font-size:12px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#000}
</style>
</head>
<body>
<div id="map"></div>
<div id="topControls"><button class="btn-map" onclick="window.location='/'">返回</button></div>
<div id="panel">
  <div class="head">设备列表</div>
  <div class="body" id="devList"><div style="color:#8b949e;text-align:center;padding:20px">等待数据...</div></div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
var map=L.map('map').setView([30,120],10);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19}).addTo(map);

var markers={};
var droneMarkers={};

function update(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    var html='';
    d.devices.list.forEach(function(dev){
      html+='<div class="device-row" onclick="map.flyTo(['+dev.lat+','+dev.lon+'],14)">'
        +'<span class="dot '+(dev.status==='online'?'online':'offline')+'"></span>'
        +'<span class="name">'+dev.name+'</span>'
        +'<div class="meta">'+dev.location+' &middot; '+(dev.last_seen||'').substring(11,19)+'</div>'
        +'</div>';
    });
    if(!html) html='<div style="color:#8b949e;text-align:center;padding:20px">暂无设备</div>';
    document.getElementById('devList').innerHTML=html;

    var seen={};
    d.devices.list.forEach(function(dev){
      if(!dev.lat||!dev.lon) return;
      seen[dev.name]=true;
      var color=dev.status==='online'?'#3fb950':'#f85149';
      if(markers[dev.name]){
        markers[dev.name].setLatLng([dev.lat,dev.lon]);
      }else{
        markers[dev.name]=L.circleMarker([dev.lat,dev.lon],{
          radius:8,color:color,fillColor:color,fillOpacity:.8,weight:2.5
        }).addTo(map).bindPopup('<b>'+dev.name+'</b><br>'+dev.location+'<br>状态: '+(dev.status==='online'?'在线':'离线'));
      }
    });
    Object.keys(markers).forEach(function(k){if(!seen[k]){map.removeLayer(markers[k]);delete markers[k];}});

    var seenDrones={};
    d.drones.list.forEach(function(dr){
      if(!dr.last_lat||!dr.last_lon) return;
      var id=dr.id+'@'+dr.device_name;
      seenDrones[id]=true;
      var sc={'active':'#3fb950','warning':'#d2991d','severe':'#db6d28','critical':'#f85149'};
      var color=sc[dr.status]||'#3fb950';
      if(droneMarkers[id]){
        droneMarkers[id].setLatLng([dr.last_lat,dr.last_lon]);
        droneMarkers[id].setStyle({color:color,fillColor:color});
      }else{
        droneMarkers[id]=L.circleMarker([dr.last_lat,dr.last_lon],{
          radius:5,color:color,fillColor:color,fillOpacity:.6,weight:2
        }).addTo(map).bindPopup('<b>'+dr.id+'</b><br>设备: '+dr.device_name+'<br>高度: '+(dr.last_alt||0).toFixed(0)+'m<br>状态: '+dr.status);
      }
    });
    Object.keys(droneMarkers).forEach(function(k){if(!seenDrones[k]){map.removeLayer(droneMarkers[k]);delete droneMarkers[k];}});
  });
}
update();setInterval(update,3000);
</script>
</body>
</html>
"""


@bp.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@bp.route("/map")
def map_view():
    return render_template_string(MAP_HTML)
