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
    <div class="status"><span class="dot" id="dot"></span><span id="statusText">已停止</span></div>
    <select id="modeSelect"><option value="ble">BLE</option><option value="wifi">WiFi</option></select>
    <button class="btn btn-primary" onclick="startScan()" id="btnStart">开始</button>
    <button class="btn btn-danger" onclick="stopScan()" id="btnStop" disabled>停止</button>
    <button class="btn btn-ghost" onclick="openPowerLines()">电力线</button>
    <button class="btn btn-ghost" onclick="showTraj()">轨迹</button>
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
        <thead><tr><th>ID</th><th>类型</th><th>纬度</th><th>经度</th><th>高度</th><th>距离</th><th>最近电力线</th><th>状态</th><th>更新</th></tr></thead>
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
          <td class="mono">${dr.id||'?'}</td><td>多旋翼</td>
          <td class="mono">${(dr.last_lat||0).toFixed(5)}</td>
          <td class="mono">${(dr.last_lon||0).toFixed(5)}</td>
          <td>${(dr.last_alt||0).toFixed(0)}m</td>
          <td class="mono">${dist}</td>
          <td>${dr.line_name||'-'}</td>
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

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/status')
def api_status():
    global controller
    drones = controller.db.get_active_drones() if controller else []
    alert_drones = dict(controller.alert_system._drone_level) if controller else {}
    for d in drones:
        d['line_name'] = ''
    
    # Add power line names to drones
    if controller:
        for d in drones:
            line_id = d.get('nearest_line_id')
            if line_id:
                for l in controller.pl_manager.lines:
                    if l.line_id == line_id:
                        d['line_name'] = l.name
                        break

    logs = controller._log_buffer[-50:] if controller and hasattr(controller, '_log_buffer') else []

    return jsonify({
        'running': controller.running if controller else False,
        'mode': controller.mode if controller else '',
        'drone_count': len(drones),
        'alert_count': len(alert_drones),
        'pl_count': len(controller.pl_manager.lines) if controller else 0,
        'drones': drones,
        'logs': logs,
        'now': datetime.now().strftime('%H:%M:%S'),
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

        self.alert_system = AlertSystem(
            db=self.db,
            thresholds=thresholds,
        )

        traj_cfg = self._config.get('trajectory', {})
        self.trajectory_recorder = TrajectoryRecorder(
            db=self.db,
            min_interval=traj_cfg.get('min_interval', 2.0),
            max_points_per_drone=traj_cfg.get('max_points_per_drone', 1000),
        )

        self.pipeline = RIDPipeline(
            db=self.db,
            pl_manager=self.pl_manager,
            alert_system=self.alert_system,
            trajectory_recorder=self.trajectory_recorder,
            thresholds=thresholds,
        )

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

        if self.mode == 'wifi':
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
    controller.switch_mode('ble')

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
