"""
Flask Web GUI - 无人机 RID 接收与电力线防碰撞监控系统

纯 Python 依赖 (flask), 不依赖 tkinter。
浏览器访问 http://localhost:5000 即可使用。
"""

import json
import os
import sys
import threading
import time
import asyncio
from pathlib import Path
from datetime import datetime

from flask import Flask, render_template_string, jsonify, request

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from logging_config import get_logger

logger = get_logger(__name__)

from core.config import load_config as load_yaml_config


def _get_base_path():
    """获取资源根目录（兼容 PyInstaller 打包和源码运行）"""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return SCRIPT_DIR.parent


PROJECT_ROOT = _get_base_path()

from storage.database import Database
from core.powerline import PowerLineManager
from core.alert import AlertSystem
from core.trajectory import TrajectoryRecorder
from core.pipeline import RIDPipeline
from core.beidou import create_beidou
from core.backhaul import BackhaulManager
from core.parser import configure_protocol

app = Flask(__name__)

# ── 全局状态 ──
controller = None

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Drone RID Monitor</title>
<style>
:root{
  --bg:#f6f8fa;--surface:#fff;--border:#d0d7de;
  --text:#1f2328;--muted:#656d76;
  --blue:#0969da;--green:#1a7f37;--yellow:#9a6700;--orange:#bc4c00;--red:#cf222e;
  --radius:6px;--shadow:0 1px 3px rgba(0,0,0,.06);
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);min-height:100vh;font-size:13px;line-height:1.5}

/* ── Top Bar ── */
.topbar{
  background:var(--surface);border-bottom:1px solid var(--border);box-shadow:var(--shadow);
  display:flex;align-items:center;justify-content:space-between;
  padding:0 20px;height:48px;
}
.topbar .brand{font-size:15px;font-weight:600;color:var(--text)}
.topbar .brand span{color:var(--muted);font-weight:400;font-size:12px;margin-left:8px}
.topbar .actions{display:flex;align-items:center;gap:10px}
.topbar .status{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)}
.topbar .status .dot{width:7px;height:7px;border-radius:50%;background:var(--red)}
.topbar .status .dot.live{background:var(--green)}
.comms-status{display:flex;align-items:center;gap:2px}
.comm-dot{width:6px;height:6px;border-radius:50%;background:var(--red)}
.comm-dot.online{background:var(--green)}
.comm-dot.degraded{background:#d4a72c}
select{
  background:var(--bg);color:var(--text);border:1px solid var(--border);
  padding:5px 10px;border-radius:var(--radius);font-size:12px;cursor:pointer;outline:none;
}
select:focus{border-color:var(--blue)}
.btn{padding:5px 14px;border-radius:var(--radius);font-size:12px;font-weight:500;cursor:pointer;border:1px solid transparent;transition:all .15s}
.btn-primary{background:var(--green);color:#fff;border-color:var(--green)}
.btn-primary:hover{background:#116329}
.btn-primary:disabled{opacity:.5;cursor:default}
.btn-danger{background:var(--red);color:#fff}
.btn-danger:hover{background:#a40e26}
.btn-danger:disabled{opacity:.5;cursor:default}
.btn-ghost{background:transparent;color:var(--text);border-color:var(--border)}
.btn-ghost:hover{background:#f3f4f6}

/* ── Stats Row ── */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;padding:16px 20px}
.card{
  background:var(--surface);border:1px solid var(--border);border-radius:8px;
  padding:14px 16px;box-shadow:var(--shadow);
}
.card .label{font-size:11px;color:var(--muted);margin-bottom:4px}
.card .value{font-size:28px;font-weight:600;font-variant-numeric:tabular-nums}
.card.critical{border-left:3px solid var(--red)}
.card.severe{border-left:3px solid var(--orange)}
.card.warning{border-left:3px solid var(--yellow)}
.card.info{border-left:3px solid var(--blue)}
.val-critical{color:var(--red)}
.val-severe{color:var(--orange)}
.val-warning{color:var(--yellow)}

/* ── Content ── */
.content{padding:0 20px 16px;display:grid;grid-template-columns:1fr 320px;gap:12px;height:calc(100vh - 205px)}

/* ── Table Panel ── */
.panel{
  background:var(--surface);border:1px solid var(--border);border-radius:8px;
  display:flex;flex-direction:column;overflow:hidden;box-shadow:var(--shadow);
}
.panel-header{
  padding:10px 14px;font-size:12px;font-weight:600;color:var(--muted);
  border-bottom:1px solid var(--border);
}
.panel-body{flex:1;overflow-y:auto}
table{width:100%;border-collapse:collapse}
thead{position:sticky;top:0;z-index:1}
th{
  background:var(--surface);color:var(--muted);font-size:11px;font-weight:500;
  text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);
}
td{padding:7px 10px;font-size:12px;border-bottom:1px solid #f0f0f0;white-space:nowrap}
tr:hover td{background:#f6f8fa}
tr:last-child td{border-bottom:none}
.tag{font-size:10px;font-weight:600;padding:2px 6px;border-radius:3px}
.tag-active{color:var(--green);background:#dafbe1}
.tag-warning{color:var(--yellow);background:#fff8c5}
.tag-severe{color:var(--orange);background:#fff1e5}
.tag-critical{color:var(--red);background:#ffebe9}
.tag-gone{color:var(--muted);background:#f6f8fa}
.mono{font-family:"SF Mono",Consolas,monospace;font-variant-numeric:tabular-nums}

/* ── Side Log ── */
.log-item{padding:8px 14px;font-size:12px;border-bottom:1px solid #f0f0f0;line-height:1.5}
.log-item .ts{color:var(--muted);font-size:11px;margin-right:6px}
.log-critical{border-left:3px solid var(--red);background:#ffebe9}
.log-severe{border-left:3px solid var(--orange);background:#fff1e5}
.log-warning{border-left:3px solid var(--yellow);background:#fff8c5}
.log-info{border-left:3px solid var(--border)}

.empty{color:var(--muted);text-align:center;padding:40px 0;font-size:13px}

/* ── Footer ── */
.footer{
  position:fixed;bottom:0;left:0;right:0;height:28px;
  background:var(--surface);border-top:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  padding:0 20px;font-size:11px;color:var(--muted);
}
.footer .hint{padding:2px 8px;border-radius:3px;font-size:11px}
.footer .hint.live{background:#dafbe1;color:var(--green)}
.footer .hint.stop{background:#ffebe9;color:var(--red)}

/* ── Modal ── */
.modal{display:none;position:fixed;inset:0;background:rgba(140,140,140,.4);z-index:100;justify-content:center;align-items:center;backdrop-filter:blur(2px)}
.modal.active{display:flex}
.modal-box{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:24px;width:620px;max-height:80vh;overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,.12)}
.modal-box h3{font-size:14px;font-weight:600;margin:16px 0 8px;color:var(--text)}
.modal-box h3:first-child{margin-top:0}
.modal-box label{font-size:11px;color:var(--muted);display:block;margin-top:8px}
.modal-box input{
  width:100%;padding:7px 10px;margin-top:3px;background:var(--bg);border:1px solid var(--border);
  color:var(--text);border-radius:var(--radius);font-size:12px;outline:none;
}
.modal-box input:focus{border-color:var(--blue)}
.modal-box .row{display:flex;gap:8px}
.modal-box .row input{flex:1}
.pl-item{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;background:var(--bg);border-radius:4px;margin-bottom:4px;font-size:12px}
.pl-item .del{color:var(--red);cursor:pointer;font-weight:700;padding:2px 6px;border-radius:3px}
.pl-item .del:hover{background:#ffebe9}
.modal-actions{display:flex;gap:8px;margin-top:12px}
</style>
</head>
<body>

<div class="topbar">
  <div class="brand">Drone RID Monitor <span>v2.4</span></div>
  <div class="actions">
    <!-- 通信通道状态 -->
    <div class="comms-status" title="数据回传通道">
      <span class="comm-dot" id="comm4gDot"></span><span style="font-size:10px;color:var(--muted);margin-right:4px">4G/有线</span>
      <span class="comm-dot" id="commBdDot" style="margin-left:8px"></span><span style="font-size:10px;color:var(--muted);margin-right:4px">北斗</span>
      <span style="font-size:10px;color:var(--muted)" id="commLabel">--</span>
      <span id="commQueue" style="display:none;font-size:10px;color:var(--yellow);margin-left:4px"></span>
    </div>
    <div class="status"><span class="dot" id="dot"></span><span id="statusText">已停止</span></div>
    <select id="modeSelect"><option value="simulated">模拟</option><option value="ble">BLE</option><option value="wifi">WiFi</option></select>
    <button class="btn btn-primary" onclick="startScan()" id="btnStart">开始</button>
    <button class="btn btn-danger" onclick="stopScan()" id="btnStop" disabled>停止</button>
    <button class="btn btn-ghost" onclick="openPowerLines()">电力线</button>
    <button class="btn btn-ghost" onclick="showTraj()">轨迹</button>
    <button class="btn btn-ghost" onclick="window.location='/map'">地图视图</button>
  </div>
</div>

<div class="stats">
  <div class="card info"><div class="label">活跃无人机</div><div class="value" id="statDrones">0</div></div>
  <div class="card warning"><div class="label">警告 / 200m</div><div class="value val-warning" id="statWarn">0</div></div>
  <div class="card severe"><div class="label">严重 / 100m</div><div class="value val-severe" id="statSev">0</div></div>
  <div class="card critical"><div class="label">危险 / 50m</div><div class="value val-critical" id="statCrit">0</div></div>
</div>

<div class="content">
  <div class="panel">
    <div class="panel-header">无人机列表</div>
    <div class="panel-body">
      <table>
        <thead><tr><th>ID</th><th>SN / ID</th><th>飞行器类型</th><th>推测型号</th><th>纬度</th><th>经度</th><th>高度</th><th>距离</th><th>最近电力线</th><th>起飞位</th><th>状态</th><th>更新</th></tr></thead>
        <tbody id="droneTable"></tbody>
      </table>
      <div class="empty" id="emptyMsg" style="display:none">等待 RID 广播信号...</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">告警日志</div>
    <div class="panel-body" id="logPanel">
      <div class="empty">暂无告警</div>
    </div>
  </div>
</div>

<div class="footer">
  <span id="footerLeft">--</span>
  <span id="footerHint" class="hint stop">已停止</span>
</div>

<div class="modal" id="plModal">
  <div class="modal-box">
    <h3>电力线管理</h3>
    <div id="plList" style="max-height:280px;overflow-y:auto;margin-bottom:8px"></div>
    <h3>新增线段</h3>
    <label>名称</label><input id="plName" placeholder="例如: 高压线A-北段">
    <label>端点 1 (纬度, 经度, 海拔m)</label>
    <div class="row"><input id="plLat1" placeholder="纬度" step="any"><input id="plLon1" placeholder="经度" step="any"><input id="plAlt1" placeholder="海拔" step="any"></div>
    <label>端点 2 (纬度, 经度, 海拔m)</label>
    <div class="row"><input id="plLat2" placeholder="纬度" step="any"><input id="plLon2" placeholder="经度" step="any"><input id="plAlt2" placeholder="海拔" step="any"></div>
    <div class="modal-actions">
      <button class="btn btn-primary" onclick="addPowerLine()">添加</button>
      <button class="btn btn-ghost" onclick="closeModal('plModal')">关闭</button>
    </div>
  </div>
</div>

<script>
let pollTimer;

function updateUI(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    let running=d.running;
    let dot=document.getElementById('dot');
    dot.className='dot'+(running?' live':'');
    document.getElementById('statusText').textContent=running?'运行中':'已停止';
    document.getElementById('btnStart').disabled=running;
    document.getElementById('btnStop').disabled=!running;

    // footer hint
    let hint=document.getElementById('footerHint');
    hint.textContent=running?'正在监听 '+(d.mode||'').toUpperCase():'已停止';
    hint.className='hint '+(running?'live':'stop');

    // Comms channel status
    let bh=d.backhaul;
    let c4g=document.getElementById('comm4gDot');
    let cbd=document.getElementById('commBdDot');
    let clbl=document.getElementById('commLabel');
    let cq=document.getElementById('commQueue');
    if(bh){
      c4g.className='comm-dot '+(bh.primary_online?'online':'');
      cbd.className='comm-dot '+(bh.beidou_online?'online':'');
      if(bh.channel==='4g_wired'){clbl.textContent='4G/有线';
      }else if(bh.channel==='beidou_emergency'){clbl.textContent='北斗应急';
      }else{clbl.textContent='通信中断';}
      if(bh.queue_size>0){cq.style.display='';cq.textContent='积压'+bh.queue_size;
      }else{cq.style.display='none';}
    }

    // Stats
    let warn=0,sev=0,crit=0;
    d.drones.forEach(dr=>{let s=dr.status;if(s==='warning')warn++;if(s==='severe')sev++;if(s==='critical')crit++;});
    document.getElementById('statDrones').textContent=d.drones.length;
    document.getElementById('statWarn').textContent=warn;
    document.getElementById('statSev').textContent=sev;
    document.getElementById('statCrit').textContent=crit;

    // Table
    let table=document.getElementById('droneTable');
    let empty=document.getElementById('emptyMsg');
    if(d.drones.length===0){
      table.innerHTML='';
      empty.style.display='block';
    }else{
      empty.style.display='none';
      let tagClass={'active':'tag-active','warning':'tag-warning','severe':'tag-severe','critical':'tag-critical','gone':'tag-gone'};
      let tagText={'active':'正常','warning':'[W] 警告','severe':'[S] 严重','critical':'[X] 危险','gone':'离线'};
      table.innerHTML=d.drones.map(dr=>{
        let s=dr.status||'active';
        let dist=dr.min_distance!=null?dr.min_distance.toFixed(0)+'m':'-';
        let time=(dr.last_seen||'').substring(11,19);
        return `<tr>
          <td class="mono" title="${dr.id||''}">${((dr.id||'').length>12?(dr.id||'').substring(0,12)+'...':(dr.id||'?'))}</td><td>${dr.category_name||'未知'}</td><td style="color:var(--blue)">${dr.product_model||'-'}</td>
          <td class="mono">${(dr.last_lat||0).toFixed(5)}</td>
          <td class="mono">${(dr.last_lon||0).toFixed(5)}</td>
          <td>${(dr.last_alt||0).toFixed(0)}m</td>
          <td class="mono">${dist}</td>
          <td>${dr.line_name||'-'}</td>
          <td class="mono" style="font-size:10px">${dr.takeoff_lat!=null?dr.takeoff_lat.toFixed(4)+','+dr.takeoff_lon.toFixed(4):'-'}</td>
          <td><span class="tag ${tagClass[s]||'tag-active'}">${tagText[s]||s}</span></td>
          <td class="mono">${time}</td>
        </tr>`;
      }).join('');
    }

    // Log
    let log=document.getElementById('logPanel');
    if(d.logs.length===0){
      log.innerHTML='<div class="empty">暂无告警</div>';
    }else{
      log.innerHTML=d.logs.map(l=>{
        return `<div class="log-item log-${l.level}"><span class="ts">${l.time}</span>${l.msg}</div>`;
      }).join('');
      log.scrollTop=log.scrollHeight;
    }

    document.getElementById('footerLeft').textContent='更新 '+d.now;
  });
}

function startScan(){
  fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:document.getElementById('modeSelect').value})}).then(r=>r.json());
}
function stopScan(){fetch('/api/stop',{method:'POST'}).then(r=>r.json());}
function openPowerLines(){
  fetch('/api/powerlines').then(r=>r.json()).then(d=>{
    document.getElementById('plList').innerHTML=d.length===0
      ?'<div style="color:var(--muted);padding:8px">暂无电力线</div>'
      :d.map((l,i)=>`<div class="pl-item"><span>${l.name}<span style="color:var(--muted)">  (${l.lat1.toFixed(4)},${l.lon1.toFixed(4)},${l.alt1}m) &rarr; (${l.lat2.toFixed(4)},${l.lon2.toFixed(4)},${l.alt2}m)</span></span><span class="del" onclick="delPowerLine(${i})">x</span></div>`).join('');
  });
  document.getElementById('plModal').classList.add('active');
}
function addPowerLine(){
  let data={
    name:document.getElementById('plName').value,
    lat1:parseFloat(document.getElementById('plLat1').value),
    lon1:parseFloat(document.getElementById('plLon1').value),
    alt1:parseFloat(document.getElementById('plAlt1').value),
    lat2:parseFloat(document.getElementById('plLat2').value),
    lon2:parseFloat(document.getElementById('plLon2').value),
    alt2:parseFloat(document.getElementById('plAlt2').value)};
  fetch('/api/powerlines',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
  .then(r=>r.json()).then(()=>openPowerLines());
}
function delPowerLine(idx){
  fetch('/api/powerlines/'+idx,{method:'DELETE'}).then(r=>r.json()).then(()=>openPowerLines());
}
function closeModal(id){document.getElementById(id).classList.remove('active');}
function showTraj(){
  fetch('/api/trajectories').then(r=>r.json()).then(d=>{
    let keys=Object.keys(d);
    let html='<h3>轨迹记录</h3><div style="max-height:400px;overflow-y:auto">';
    if(keys.length===0){html+='<div style="color:var(--muted);padding:8px">暂无轨迹数据</div>';}
    for(let did of keys){
      html+=`<div class="pl-item"><span><b>${did}</b><span style="color:var(--muted)">  ${d[did].count} 点 &middot; 最近 ${d[did].min_dist.toFixed(1)}m &middot; ${d[did].first} &rarr; ${d[did].last}</span></span></div>`;
    }
    html+='</div><div class="modal-actions"><button class="btn btn-ghost" onclick="closeModal(\'trajModal\')">关闭</button></div>';
    let m=document.createElement('div');m.className='modal active';m.id='trajModal';
    m.innerHTML='<div class="modal-box">'+html+'</div>';
    document.body.appendChild(m);
  });
}

updateUI();
pollTimer=setInterval(updateUI,1500);
</script>
</body>
</html>
"""

MAP_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Drone RID Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;font-size:13px}
#map{width:100%;height:100%;background:#e8e8e8}

/* ── Sidebar ── */
#sidebar{
  position:fixed;top:0;left:0;width:340px;height:100%;
  background:rgba(255,255,255,0.94);backdrop-filter:blur(8px);
  box-shadow:2px 0 12px rgba(0,0,0,.12);
  z-index:1000;display:flex;flex-direction:column;
  transition:transform .25s ease;
}
#sidebar.collapsed{transform:translateX(-340px)}
#sidebar .head{
  padding:14px 16px;border-bottom:1px solid #e0e0e0;
  display:flex;align-items:center;justify-content:space-between;
}
#sidebar .head .brand{font-size:14px;font-weight:600}
#sidebar .head .brand span{color:#888;font-weight:400;font-size:11px;margin-left:6px}
#sidebar .head .dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:6px}
#sidebar .head .dot.live{background:#1a7f37}
#sidebar .head .dot.stop{background:#cf222e}

/* Stats mini */
.stats-mini{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:6px;padding:10px 14px}
.stats-mini .sm{border-radius:6px;padding:8px;text-align:center;font-variant-numeric:tabular-nums}
.sm .val{font-size:20px;font-weight:700}
.sm .lbl{font-size:10px;color:#888}
.sm-info{background:#ecf6ff}.sm-info .val{color:#0969da}
.sm-warn{background:#fff8c5}.sm-warn .val{color:#9a6700}
.sm-sev{background:#fff1e5}.sm-sev .val{color:#bc4c00}
.sm-crit{background:#ffebe9}.sm-crit .val{color:#cf222e}

/* Drone list */
#droneList{flex:1;overflow-y:auto;padding:0}
#droneList .drone-row{
  display:flex;align-items:center;gap:8px;padding:10px 14px;
  border-bottom:1px solid #f0f0f0;cursor:pointer;transition:background .1s;
}
#droneList .drone-row:hover{background:#f6f8fa}
.drone-row .icon{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.drone-row .icon.active{background:#1a7f37}
.drone-row .icon.warning{background:#d4a72c}
.drone-row .icon.severe{background:#bc4c00}
.drone-row .icon.critical{background:#cf222e}
.drone-row .icon.gone{background:#aaa}
.drone-row .info{flex:1;min-width:0}
.drone-row .info .did{font-weight:500;font-size:11px;font-family:"SF Mono",Consolas,monospace}
.drone-row .info .sub{font-size:10px;color:#888}
.drone-row .dist{font-weight:600;font-size:12px;font-variant-numeric:tabular-nums}

/* Power line list */
#plListInSidebar{padding:8px 14px;border-top:1px solid #e0e0e0;max-height:200px;overflow-y:auto}
#plListInSidebar .pl-label{font-size:10px;color:#888;margin-bottom:6px;font-weight:600}
.pl-mini{
  display:flex;align-items:center;gap:6px;font-size:11px;
  padding:4px 0;cursor:pointer;
}
.pl-mini .swatch{width:14px;height:3px;border-radius:1px;flex-shrink:0}
.pl-mini:hover{color:var(--blue,#0969da)}

/* Sidebar footer */
#sidebar .foot{
  padding:8px 14px;border-top:1px solid #e0e0e0;
  display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#888;
}
.btn-sm{
  padding:3px 10px;border-radius:4px;font-size:11px;cursor:pointer;border:1px solid #d0d7de;
  background:#f6f8fa;color:#333;transition:all .1s;
}
.btn-sm:hover{background:#e8eaed}
.btn-sm.active{background:#0969da;color:#fff;border-color:#0969da}

/* Collapse toggle */
#collapseBtn{
  position:fixed;top:12px;left:348px;z-index:1001;
  width:28px;height:28px;border-radius:50%;background:rgba(255,255,255,0.94);
  border:1px solid #d0d7de;cursor:pointer;font-size:14px;display:flex;
  align-items:center;justify-content:center;box-shadow:0 1px 4px rgba(0,0,0,.1);
  transition:left .25s ease;
}
#collapseBtn.shifted{left:8px}

/* Top-right controls */
#topControls{
  position:fixed;top:12px;right:12px;z-index:1000;
  display:flex;gap:6px;
}
#topControls .btn-map{
  padding:6px 14px;border-radius:6px;font-size:12px;font-weight:500;
  cursor:pointer;border:1px solid #d0d7de;background:rgba(255,255,255,0.94);
  box-shadow:0 1px 4px rgba(0,0,0,.08);transition:all .1s;
}
#topControls .btn-map:hover{background:#f6f8fa}

/* Legend */
#legend{
  position:fixed;bottom:24px;right:12px;z-index:1000;
  background:rgba(255,255,255,0.92);border-radius:6px;
  padding:10px 12px;box-shadow:0 1px 4px rgba(0,0,0,.08);
  font-size:11px;
}
#legend .leg-item{display:flex;align-items:center;gap:6px;margin:4px 0}
#legend .leg-dot{width:9px;height:9px;border-radius:50%}

/* Leaflet overrides */
.leaflet-popup-content{font-size:12px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
.leaflet-popup-content b{font-size:12px}
.popup-row{display:flex;justify-content:space-between;gap:12px;margin:2px 0}
.popup-btn{display:inline-block;margin-top:6px;padding:4px 12px;border-radius:4px;
  font-size:11px;cursor:pointer;border:1px solid #0969da;color:#0969da;background:#fff}
.popup-btn:hover{background:#ecf6ff}
.popup-btn.traj-active{background:#0969da;color:#fff}
</style>
</head>
<body>
<div id="map"></div>

<div id="sidebar">
  <div class="head">
    <div class="brand">
      <span class="dot stop" id="statusDot"></span>Drone RID Map <span>v2.5</span>
    </div>
    <span style="font-size:11px;color:#888" id="sidebarTime">--</span>
  </div>
  <div class="stats-mini">
    <div class="sm sm-info"><div class="val" id="sTotal">0</div><div class="lbl">活跃</div></div>
    <div class="sm sm-warn"><div class="val" id="sWarn">0</div><div class="lbl">警告≤200m</div></div>
    <div class="sm sm-sev"><div class="val" id="sSev">0</div><div class="lbl">严重≤100m</div></div>
    <div class="sm sm-crit"><div class="val" id="sCrit">0</div><div class="lbl">危险≤50m</div></div>
  </div>
  <div id="droneList"><div style="color:#aaa;text-align:center;padding:24px">等待无人机数据...</div></div>
  <div id="plListInSidebar">
    <div class="pl-label">电力线</div>
    <div id="plItems" style="color:#aaa;font-size:11px">加载中...</div>
  </div>
  <div class="foot">
    <span id="footerStatus">已停止</span>
    <span id="footerCount">--</span>
  </div>
</div>

<button id="collapseBtn" onclick="toggleSidebar()" title="收起/展开">◀</button>

<div id="topControls">
  <button class="btn-map" onclick="window.location='/'">返回列表</button>
</div>

<div id="legend">
  <div class="leg-item"><span class="leg-dot" style="background:#1a7f37"></span> 正常</div>
  <div class="leg-item"><span class="leg-dot" style="background:#d4a72c"></span> 警告 (&lt;200m)</div>
  <div class="leg-item"><span class="leg-dot" style="background:#bc4c00"></span> 严重 (&lt;100m)</div>
  <div class="leg-item"><span class="leg-dot" style="background:#cf222e"></span> 危险 (&lt;50m)</div>
  <div class="leg-item"><span class="leg-dot" style="background:#aaa"></span> 离线</div>
  <div style="margin-top:4px;border-top:1px solid #e0e0e0;padding-top:4px">
    <span style="display:inline-block;width:18px;border-top:2px dashed #c40;margin-right:4px;vertical-align:middle"></span> 电力线
  </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// ── Map init ──
var map = L.map('map', {attributionControl: true}).setView([31, 110], 5);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>'
}).addTo(map);

// ── Layer groups ──
var droneMarkers = {};        // drone_id -> L.circleMarker
var trajPolylines = {};       // drone_id -> L.polyline
var plPolylines = [];         // power line L.polylines
var plLabels = [];            // power line L.tooltips
var activeTrajDrone = null;
var firstDroneSeen = false;

// ── Sidebar toggle ──
function toggleSidebar(){
  var sb = document.getElementById('sidebar');
  var btn = document.getElementById('collapseBtn');
  sb.classList.toggle('collapsed');
  btn.classList.toggle('shifted');
  btn.textContent = sb.classList.contains('collapsed') ? '▶' : '◀';
  setTimeout(function(){ map.invalidateSize(); }, 300);
}

// ── Status colors ──
var statusColors = {
  'active':'#1a7f37','warning':'#d4a72c','severe':'#bc4c00','critical':'#cf222e','gone':'#aaa'
};
var statusZh = {'active':'正常','warning':'警告','severe':'严重','critical':'危险','gone':'离线'};

function markerColor(status){ return statusColors[status] || '#1a7f37'; }
function markerRadius(status){
  if(status==='critical') return 9;
  if(status==='severe') return 7;
  if(status==='warning') return 6;
  return 5;
}

// ── Load power lines ──
function loadPowerLines(){
  fetch('/api/powerlines').then(function(r){return r.json()}).then(function(lines){
    // Clear old
    plPolylines.forEach(function(p){ map.removeLayer(p); });
    plLabels.forEach(function(l){ map.removeLayer(l); });
    plPolylines = [];
    plLabels = [];

    var html = '';
    if(lines.length === 0){
      html = '<span style="color:#aaa">无电力线</span>';
    }else{
      lines.forEach(function(l, i){
        var latlngs = [[l.lat1, l.lon1], [l.lat2, l.lon2]];
        var poly = L.polyline(latlngs, {
          color: '#cc4400', weight: 3, dashArray: '8, 6', opacity: 0.8
        }).addTo(map);
        var label = L.tooltip({permanent: true, direction: 'center', className: 'pl-tooltip'})
          .setLatLng([(l.lat1+l.lat2)/2, (l.lon1+l.lon2)/2])
          .setContent('<span style="font-size:10px;background:rgba(255,255,255,.85);padding:1px 4px;border-radius:3px;color:#c40;font-weight:600">'+l.name+'</span>')
          .addTo(map);
        plPolylines.push(poly);
        plLabels.push(label);

        html += '<div class="pl-mini" onclick="map.flyToBounds([['+l.lat1+','+l.lon1+'],['+l.lat2+','+l.lon2+']],{padding:[50,50]})">'
          +'<span class="swatch" style="background:#c40"></span>'+l.name+'</div>';
      });
    }
    document.getElementById('plItems').innerHTML = html;
  });
}

// ── Show trajectory for a drone ──
function showTrajectory(droneId){
  // Toggle off if already active
  if(activeTrajDrone === droneId){
    removeTrajectory(droneId);
    activeTrajDrone = null;
    return;
  }
  // Remove previous
  if(activeTrajDrone && activeTrajDrone !== droneId){
    removeTrajectory(activeTrajDrone);
  }

  fetch('/api/trajectories/' + encodeURIComponent(droneId) + '/points')
    .then(function(r){return r.json()})
    .then(function(pts){
      if(!pts || pts.length < 2){
        alert('该无人机轨迹数据不足');
        return;
      }
      var latlngs = pts.map(function(p){ return [p.lat, p.lon]; });
      var line = L.polyline(latlngs, {
        color: '#2196F3', weight: 3, opacity: 0.7, smoothFactor: 1
      }).addTo(map);
      // Add direction markers (small dots)
      var startMarker = L.circleMarker(latlngs[0], {
        radius: 4, color: '#2196F3', fillColor: '#fff', fillOpacity: 1, weight: 2
      }).addTo(map);
      var endMarker = L.circleMarker(latlngs[latlngs.length-1], {
        radius: 5, color: '#2196F3', fillColor: '#2196F3', fillOpacity: 1, weight: 2
      }).addTo(map);

      trajPolylines[droneId] = {line: line, start: startMarker, end: endMarker};
      activeTrajDrone = droneId;

      // Fit bounds to show the full trajectory
      map.fitBounds(line.getBounds(), {padding: [50, 50], maxZoom: 16});
    });
}

function removeTrajectory(droneId){
  var layers = trajPolylines[droneId];
  if(layers){
    map.removeLayer(layers.line);
    map.removeLayer(layers.start);
    map.removeLayer(layers.end);
    delete trajPolylines[droneId];
  }
  activeTrajDrone = null;
}

// ── Fly to drone ──
function flyToDrone(lat, lon){
  map.flyTo([lat, lon], Math.max(map.getZoom(), 14), {duration: 0.6});
}

// ── Main update loop ──
function updateMap(){
  fetch('/api/status').then(function(r){return r.json()}).then(function(d){
    var running = d.running;
    var dot = document.getElementById('statusDot');
    dot.className = 'dot ' + (running ? 'live' : 'stop');
    document.getElementById('footerStatus').textContent = running ? '运行中 ('+d.mode.toUpperCase()+')' : '已停止';

    // Stats
    var warn=0, sev=0, crit=0;
    d.drones.forEach(function(dr){
      var s=dr.status; if(s==='warning')warn++; if(s==='severe')sev++; if(s==='critical')crit++;
    });
    document.getElementById('sTotal').textContent = d.drones.length;
    document.getElementById('sWarn').textContent = warn;
    document.getElementById('sSev').textContent = sev;
    document.getElementById('sCrit').textContent = crit;
    document.getElementById('footerCount').textContent = '更新 '+d.now;

    // Track current drone IDs
    var seen = {};

    // Update/create drone markers
    d.drones.forEach(function(dr){
      if(dr.last_lat == null || dr.last_lon == null) return;
      var id = dr.id || '?';
      seen[id] = true;
      var lat = dr.last_lat, lon = dr.last_lon;
      var status = dr.status || 'active';
      var color = markerColor(status);
      var radius = markerRadius(status);

      if(droneMarkers[id]){
        // Update position
        droneMarkers[id].setLatLng([lat, lon]);
        droneMarkers[id].setStyle({color: color, fillColor: color});
        droneMarkers[id].setRadius(radius);
      }else{
        var marker = L.circleMarker([lat, lon], {
          radius: radius, color: color, fillColor: color,
          fillOpacity: 0.6, weight: 2.5
        }).addTo(map);
        marker.bindPopup(popupContent(dr));
        droneMarkers[id] = marker;
      }

      // Auto-center on first drone
      if(!firstDroneSeen && d.drones.length > 0){
        firstDroneSeen = true;
        map.setView([lat, lon], 14);
      }
    });

    // Remove gone drones
    Object.keys(droneMarkers).forEach(function(k){
      if(!seen[k]){
        map.removeLayer(droneMarkers[k]);
        delete droneMarkers[k];
      }
    });

    // Remove trajectory for gone drones
    Object.keys(trajPolylines).forEach(function(k){
      if(!seen[k]) removeTrajectory(k);
    });

    // Refresh trajectory for active drone if showing
    if(activeTrajDrone && seen[activeTrajDrone]){
      removeTrajectory(activeTrajDrone);
      showTrajectory(activeTrajDrone);
    }

    // Update sidebar drone list
    var listDiv = document.getElementById('droneList');
    if(d.drones.length === 0){
      listDiv.innerHTML = '<div style="color:#aaa;text-align:center;padding:24px">等待无人机数据...</div>';
    }else{
      listDiv.innerHTML = d.drones.map(function(dr){
        var s = dr.status || 'active';
        var dist = dr.min_distance != null ? dr.min_distance.toFixed(0)+'m' : '-';
        var time = (dr.last_seen||'').substring(11,19);
        return '<div class="drone-row" onclick="flyToDrone('+dr.last_lat+','+dr.last_lon+')">'
          +'<span class="icon '+s+'"></span>'
          +'<div class="info"><div class="did">'+dr.id+'</div><div class="sub">'+time+' &middot; '+statusZh[s]+'</div></div>'
          +'<div class="dist">'+dist+'</div>'
          +'</div>';
      }).join('');
    }

    document.getElementById('sidebarTime').textContent = d.now;
  });
}

function popupContent(dr){
  var s = dr.status || 'active';
  var dist = dr.min_distance != null ? dr.min_distance.toFixed(0)+' m' : '--';
  var alt = dr.last_alt != null ? dr.last_alt.toFixed(0)+' m' : '--';
  var lineName = dr.line_name || '--';
  return '<b>'+dr.id+'</b>'
    +'<div class="popup-row"><span>状态:</span><span style="color:'+markerColor(s)+';font-weight:600">'+statusZh[s]+'</span></div>'
    +'<div class="popup-row"><span>经度:</span><span>'+(dr.last_lon||0).toFixed(5)+'</span></div>'
    +'<div class="popup-row"><span>纬度:</span><span>'+(dr.last_lat||0).toFixed(5)+'</span></div>'
    +'<div class="popup-row"><span>高度:</span><span>'+alt+'</span></div>'
    +'<div class="popup-row"><span>距离:</span><span>'+dist+'</span></div>'
    +'<div class="popup-row"><span>最近电力线:</span><span>'+lineName+'</span></div>'
    +'<button class="popup-btn'+(activeTrajDrone===dr.id?' traj-active':'')+'" onclick="showTrajectory(\''+dr.id+'\')">'
    +(activeTrajDrone===dr.id?'隐藏轨迹':'显示轨迹')+'</button>';
}

// ── Start ──
loadPowerLines();
updateMap();
setInterval(updateMap, 1500);
setInterval(loadPowerLines, 30000);  // refresh power lines every 30s
</script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/map')
def map_view():
    return render_template_string(MAP_TEMPLATE)

@app.route('/api/status')
def api_status():
    global controller
    drones = controller.db.get_active_drones() if controller else []
    alert_drones = dict(controller.alert_system._drone_level) if controller else {}
    for d in drones:
        d['line_name'] = ''
    
    # Add power line names, model names to drones
    if controller:
        for d in drones:
            from core.parser.types import UA_TYPE_NAMES, lookup_model_by_sn
            d['category_name'] = UA_TYPE_NAMES.get(d.get('ua_type', 0), '未知')
            d['product_model'] = lookup_model_by_sn(d['id']) or ''
            line_id = d.get('nearest_line_id')
            if line_id:
                for l in controller.pl_manager.lines:
                    if l.line_id == line_id:
                        d['line_name'] = l.name
                        break

    logs = controller._log_buffer[-50:] if controller and hasattr(controller, '_log_buffer') else []

    bhaul = controller.backhaul if controller else None
    return jsonify({
        'running': controller.running if controller else False,
        'mode': controller.mode if controller else '',
        'drone_count': len(drones),
        'alert_count': len(alert_drones),
        'pl_count': len(controller.pl_manager.lines) if controller else 0,
        'drones': drones,
        'logs': logs,
        'now': datetime.now().strftime('%H:%M:%S'),
        'backhaul': {
            'channel': bhaul.channel_status if bhaul else 'offline',
            'primary_online': bhaul.primary_online if bhaul else False,
            'beidou_online': bhaul.beidou_online if bhaul else False,
            'active_channel': bhaul.active_channel if bhaul else 'none',
            'queue_size': bhaul.queue_size if bhaul else 0,
            'stats': bhaul.stats if bhaul else {},
        } if bhaul else None,
    })

@app.route('/api/start', methods=['POST'])
def api_start():
    global controller
    data = request.json or {}
    mode = data.get('mode', 'ble')
    if controller is None:
        controller = WebController()
    controller.switch_mode(mode, wifi_interface=data.get('interface'))
    return jsonify({'status': 'ok', 'mode': mode})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    global controller
    if controller:
        controller.stop()
    return jsonify({'status': 'ok'})

@app.route('/api/powerlines', methods=['GET', 'POST', 'DELETE'])
def api_powerlines():
    global controller
    if not controller:
        return jsonify([])
    if request.method == 'GET':
        return jsonify([{
            'name': l.name, 'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
            'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2
        } for l in controller.pl_manager.lines])
    elif request.method == 'POST':
        data = request.json
        from core.powerline import PowerLineSegment
        seg = PowerLineSegment(
            name=data['name'], lat1=data['lat1'], lon1=data['lon1'], alt1=data['alt1'],
            lat2=data['lat2'], lon2=data['lon2'], alt2=data['alt2'],
            line_id=len(controller.pl_manager.lines) + 1
        )
        controller.pl_manager.lines.append(seg)
        controller.db.load_power_lines([{
            'name': l.name, 'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
            'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2, 'id': l.line_id
        } for l in controller.pl_manager.lines])
        controller._save_power_lines_to_yaml()
        return jsonify({'status': 'ok'})

@app.route('/api/powerlines/<int:idx>', methods=['DELETE'])
def api_delete_powerline(idx):
    global controller
    if controller and idx < len(controller.pl_manager.lines):
        del controller.pl_manager.lines[idx]
        controller.db.load_power_lines([{
            'name': l.name, 'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
            'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2, 'id': i+1
        } for i, l in enumerate(controller.pl_manager.lines)])
        controller._save_power_lines_to_yaml()
    return jsonify({'status': 'ok'})

@app.route('/api/trajectories')
def api_trajectories():
    global controller
    if not controller:
        return jsonify({})
    result = {}
    drones = controller.db.get_active_drones()
    for d in drones:
        did = d['id']
        points = controller.db.get_trajectory(did, 500)
        if points:
            result[did] = {
                'count': len(points),
                'min_dist': min(p['distance_to_line'] for p in points),
                'first': points[-1]['timestamp'][:19] if points else '',
                'last': points[0]['timestamp'][:19] if points else '',
            }
    return jsonify(result)


@app.route('/api/trajectories/<drone_id>/points')
def api_trajectory_points(drone_id):
    """返回指定无人机轨迹点坐标（用于地图绘制）"""
    global controller
    if not controller:
        return jsonify([])
    points = controller.db.get_trajectory(drone_id, 500)
    return jsonify([{
        'lat': p['lat'],
        'lon': p['lon'],
        'alt': p['alt'],
        'distance': p['distance_to_line'],
        'time': p['timestamp'][:19] if p['timestamp'] else '',
    } for p in reversed(points)])


@app.route('/api/backhaul')
def api_backhaul():
    """数据回传通道状态"""
    global controller
    if not controller or not controller.backhaul:
        return jsonify({'channel': 'offline', 'primary_online': False,
                        'beidou_online': False, 'active_channel': 'none'})
    bh = controller.backhaul
    return jsonify({
        'channel': bh.channel_status,
        'primary_online': bh.primary_online,
        'beidou_online': bh.beidou_online,
        'active_channel': bh.active_channel,
        'queue_size': bh.queue_size,
        'stats': bh.stats,
    })


@app.route('/api/archive/<drone_id>')
def api_archive_drone(drone_id):
    """原始报文存档查询 + 哈希链验证"""
    global controller
    if not controller or not controller.raw_archive:
        return jsonify({'error': 'raw archive not enabled'}), 404
    raw = controller.raw_archive
    chain_ok, count, break_id = raw.verify_chain(drone_id)
    messages = controller.db.get_raw_messages(drone_id, limit=100)
    return jsonify({
        'drone_id': drone_id,
        'chain_intact': chain_ok,
        'chain_length': count,
        'break_at_id': break_id,
        'messages': messages,
    })


@app.route('/api/archive/verify')
def api_archive_verify_all():
    """全量哈希链验证"""
    global controller
    if not controller or not controller.raw_archive:
        return jsonify({'error': 'raw archive not enabled'}), 404
    results = controller.raw_archive.verify_all()
    return jsonify({
        'total': len(results),
        'results': {did: {'intact': ok, 'count': cnt}
                     for did, (ok, cnt) in results.items()},
    })


class WebController:
    """Web 模式控制器 — 无 tkinter 依赖。DB 初始化只做一次。"""

    def __init__(self):
        self.mode = None
        self.running = False
        self._log_buffer = []
        self._receiver = None
        self._loop = None
        self._thread = None
        self._wifi_interface = None

        # Init DB (once)
        config_path = str(PROJECT_ROOT / 'config' / 'config.yaml')
        self._config = load_yaml_config(config_path)
        configure_protocol(self._config)

        db_path = PROJECT_ROOT / 'data' / 'drone_rid.db'
        os.makedirs(db_path.parent, exist_ok=True)
        self.db = Database(str(db_path))

        self.pl_manager = PowerLineManager()
        self._pl_file = PROJECT_ROOT / 'config' / 'power_lines.yaml'
        self.pl_manager.load_from_yaml(str(self._pl_file))
        self.db.load_power_lines([{
            'name': l.name, 'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
            'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2, 'id': l.line_id
        } for l in self.pl_manager.lines])
        
        thresholds = self._config.get('thresholds', {'warning': 200, 'severe': 100, 'critical': 50})
        self.thresholds = thresholds

        # 告警防抖
        af_cfg = self._config.get('anti_flapping', {})
        anti_flapping = None
        if af_cfg.get('enabled', False):
            from core.anti_flapping import AntiFlappingEngine
            anti_flapping = AntiFlappingEngine(
                debounce_in=af_cfg.get('debounce_in', 3),
                debounce_out=af_cfg.get('debounce_out', 10),
            )

        self.alert_system = AlertSystem(
            db=self.db,
            thresholds=thresholds,
            anti_flapping=anti_flapping,
        )

        traj_cfg = self._config.get('trajectory', {})
        self.trajectory_recorder = TrajectoryRecorder(
            db=self.db,
            min_interval=traj_cfg.get('min_interval', 2.0),
            max_points_per_drone=traj_cfg.get('max_points_per_drone', 1000),
        )

        # 原始报文存档
        self.raw_archive = None
        if self._config.get('raw_archive', {}).get('enabled', True):
            from core.raw_archive import RawArchiveManager
            arc_cfg = self._config.get('raw_archive', {})
            self.raw_archive = RawArchiveManager(
                db=self.db,
                retention_days=arc_cfg.get('retention_days', 30),
                cleanup_interval=arc_cfg.get('cleanup_interval', 86400),
            )
            self.raw_archive.start()

        # 飞手推送
        from core.pilot_notify import create_pilot_notifier
        self.pilot_notifier = create_pilot_notifier(self._config)

        self.pipeline = RIDPipeline(
            db=self.db,
            pl_manager=self.pl_manager,
            alert_system=self.alert_system,
            trajectory_recorder=self.trajectory_recorder,
            thresholds=thresholds,
            raw_archive=self.raw_archive,
            pilot_notifier=self.pilot_notifier,
        )

        # 北斗 + 数据回传
        self._beidou = create_beidou(self._config)
        device_name = self._config.get('backhaul', {}).get('device_name', 'NW-F1')
        self.backhaul = BackhaulManager(self._config, self._beidou, device_name=device_name)
        self.pipeline.backhaul = self.backhaul
        self.backhaul.start()

    def _save_power_lines_to_yaml(self):
        """将当前电力线数据写回 YAML 文件"""
        import yaml
        pl_data = {
            "power_lines": [
                {
                    "name": l.name,
                    "lat1": l.lat1, "lon1": l.lon1, "alt1": l.alt1,
                    "lat2": l.lat2, "lon2": l.lon2, "alt2": l.alt2,
                }
                for l in self.pl_manager.lines
            ]
        }
        with open(str(self._pl_file), 'w', encoding='utf-8') as f:
            yaml.dump(pl_data, f, allow_unicode=True, default_flow_style=False)

    def switch_mode(self, mode, wifi_interface=None):
        """切换接收模式 (不重建 DB)"""
        self.stop()
        self.mode = mode
        self._wifi_interface = wifi_interface
        self.start()

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self._loop and self._receiver:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._receiver.stop(), self._loop
                )
                future.result(timeout=3)
            except Exception:
                pass
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8)
        self._thread = None
        self._loop = None

    def shutdown(self):
        """完全关闭，释放数据库"""
        self.stop()
        try:
            self.backhaul.stop()
        except Exception:
            pass
        try:
            if self.raw_archive:
                self.raw_archive.stop()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass

    def _log(self, msg, level='info'):
        self._log_buffer.append({'time': datetime.now().strftime('%H:%M:%S'), 'msg': msg, 'level': level})
        if len(self._log_buffer) > 200:
            self._log_buffer = self._log_buffer[-100:]

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        from receiver.ble import BLE_RIDReceiver

        if self.mode == 'simulated':
            from receiver.simulated import create_simulated_receiver
            self._receiver = create_simulated_receiver(
                callback=lambda p: self._on_rid(p),
                pl_manager=self.pl_manager,
                drone_count=6,
                update_interval=1.0,
            )
        elif self.mode == 'wifi':
            from receiver.wifi import create_wifi_receiver
            self._receiver = create_wifi_receiver(
                callback=lambda p: self._on_rid(p),
                interface=self._wifi_interface,
            )
        else:
            self._receiver = BLE_RIDReceiver(
                callback=lambda p: self._on_rid(p),
                scan_duration=self._config.get('ble', {}).get('scan_duration', 5.0),
            )

        async def runner():
            mode_names = {'ble': 'BLE 蓝牙', 'wifi': 'WiFi'}
            self._log(f"系统启动 ({mode_names.get(self.mode, self.mode)}模式)", "info")
            await self._receiver.start()

        try:
            self._loop.run_until_complete(runner())
        except (RuntimeError, asyncio.CancelledError):
            pass
        except Exception as e:
            self._log(f"错误: {e}", "crit")
        finally:
            try:
                tasks = asyncio.all_tasks(self._loop)
                for t in tasks:
                    t.cancel()
                self._loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            except Exception:
                pass
            self._loop.close()
            self._loop = None
    
    def _on_rid(self, parsed):
        if not self.running:
            return

        result = self.pipeline.process(parsed)
        if result is None:
            return

        if result.alert_level:
            level_tag = {'critical': '[危险]', 'severe': '[严重]', 'warning': '[警告]'}.get(result.alert_level, '!')
            self._log(f"{level_tag} [{result.alert_level}] {result.drone_id} "
                      f"距离 {result.nearest_line.name} {result.distance:.0f}m",
                      result.alert_level)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    
    global controller
    controller = WebController()
    controller.switch_mode('simulated')

    logger.info("Drone RID Receiver Web GUI 启动")
    logger.info("浏览器打开: http://%s:%s", args.host, args.port)
    logger.info("按 Ctrl+C 停止")
    
    try:
        app.run(host=args.host, port=args.port, debug=False)
    except KeyboardInterrupt:
        pass
    finally:
        if controller:
            controller.shutdown()


if __name__ == '__main__':
    main()
