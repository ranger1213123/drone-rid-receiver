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
sys.path.insert(0, str(SCRIPT_DIR))


def _get_base_path():
    """获取资源根目录（兼容 PyInstaller 打包和源码运行）"""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return SCRIPT_DIR.parent


PROJECT_ROOT = _get_base_path()

from db import Database
from powerline import PowerLineManager
from alert import AlertSystem, MockSMSBackend
from trajectory import TrajectoryRecorder
from rid_parser import UA_TYPE_NAMES

app = Flask(__name__)

# ── 全局状态 ──
controller = None

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>无人机 RID 监控系统</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Microsoft YaHei',sans-serif;background:#1e1e2e;color:#cdd6f4;min-height:100vh}
.header{background:#313244;padding:12px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:2px solid #45475a}
.header h1{font-size:18px;color:#89b4fa}
.controls{display:flex;gap:8px;align-items:center}
.controls button{padding:8px 16px;border:none;border-radius:4px;cursor:pointer;font-size:13px;font-weight:bold}
.btn-start{background:#a6e3a1;color:#1e1e2e}
.btn-stop{background:#f38ba8;color:#1e1e2e}
.btn-secondary{background:#45475a;color:#cdd6f4}
select{padding:7px 12px;background:#45475a;color:#cdd6f4;border:1px solid #585b70;border-radius:4px}
.status-dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
.status-running{background:#a6e3a1}
.status-stopped{background:#f38ba8}
.main{padding:16px;display:grid;grid-template-columns:220px 1fr;gap:16px;height:calc(100vh - 200px)}
.sidebar{background:#313244;border-radius:8px;padding:14px}
.sidebar h3{font-size:13px;color:#a6adc8;margin-bottom:10px;border-bottom:1px solid #45475a;padding-bottom:6px}
.sidebar .info{padding:6px 0;font-size:13px}
.sidebar .info span{color:#89b4fa;font-weight:bold}
.sidebar button{margin-top:8px;width:100%}
.content{display:flex;flex-direction:column;gap:12px;overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:12px}
th{background:#313244;padding:8px 6px;text-align:left;position:sticky;top:0;z-index:1;border-bottom:2px solid #45475a}
td{padding:6px;border-bottom:1px solid #313244}
tr:hover{background:#313244}
.critical{color:#f38ba8;font-weight:bold}
.severe{color:#fab387;font-weight:bold}
.warning{color:#f9e2af}
.active{color:#a6e3a1}
.gone{color:#6c7086}
.log{background:#11111b;border-radius:8px;padding:10px;height:180px;overflow-y:auto;font-family:Consolas,monospace;font-size:11px}
.log .ts{color:#6c7086}
.log .crit{color:#f38ba8}
.log .sev{color:#fab387}
.log .warn{color:#f9e2af}
.log .info{color:#a6adc8}
.table-wrap{flex:1;overflow-y:auto;border-radius:8px;background:#11111b}
.footer{display:flex;justify-content:space-between;padding:8px 20px;font-size:12px;color:#6c7086;border-top:1px solid #313244}
.modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.6);z-index:100;justify-content:center;align-items:center}
.modal.active{display:flex}
.modal-box{background:#313244;border-radius:12px;padding:20px;width:600px;max-height:80vh;overflow-y:auto}
.modal-box h2{color:#89b4fa;margin-bottom:12px}
.modal-box input{width:100%;padding:8px;margin:4px 0;background:#1e1e2e;border:1px solid #45475a;color:#cdd6f4;border-radius:4px;font-size:13px}
.modal-box .row{display:flex;gap:8px}
.modal-box .row input{flex:1}
.modal-box label{font-size:11px;color:#a6adc8;display:block;margin-top:8px}
.modal-box button{margin-top:12px}
</style>
</head>
<body>
<div class="header">
  <h1>🛸 无人机 RID 接收与电力线防碰撞监控系统 v2.1</h1>
  <div class="controls">
    <div><span class="status-dot" id="dot"></span><span id="statusText">已停止</span></div>
    <select id="modeSelect"><option value="mock">模拟数据</option><option value="ble">BLE 蓝牙</option><option value="wifi">WiFi</option></select>
    <button class="btn-start" onclick="startScan()">▶ 开始</button>
    <button class="btn-stop" onclick="stopScan()">■ 停止</button>
    <button class="btn-secondary" onclick="openPowerLines()">📝 电力线</button>
    <button class="btn-secondary" onclick="showTraj()">📊 轨迹</button>
  </div>
</div>

<div class="main">
  <div class="sidebar">
    <h3>告警阈值</h3>
    <div class="info">⚠ 警告: <span>≤200m</span></div>
    <div class="info">▲ 严重: <span>≤100m</span></div>
    <div class="info">■ 危险: <span>≤50m</span></div>
    <h3 style="margin-top:16px">系统信息</h3>
    <div class="info">电力线: <span id="plCount">0</span> 条</div>
    <div class="info">活跃无人机: <span id="droneCount">0</span></div>
    <div class="info">告警中: <span id="alertCount">0</span></div>
    <button class="btn-secondary" onclick="location.reload()" style="margin-top:16px">刷新页面</button>
  </div>

  <div class="content">
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID</th><th>类型</th><th>纬度</th><th>经度</th><th>高度</th><th>距离</th><th>最近电力线</th><th>状态</th><th>更新时间</th></tr></thead>
        <tbody id="droneTable"></tbody>
      </table>
    </div>
    <div class="log" id="logPanel"></div>
  </div>
</div>

<div class="footer">
  <span id="footerTime">--</span>
  <span>Web GUI  |  浏览器访问 http://localhost:5000</span>
</div>

<!-- 电力线编辑弹窗 -->
<div class="modal" id="plModal">
  <div class="modal-box">
    <h2>电力线管理</h2>
    <div id="plList" style="max-height:300px;overflow-y:auto;margin-bottom:10px"></div>
    <h3 style="color:#a6e3a1;margin-top:12px">新增线段</h3>
    <label>名称</label><input id="plName" placeholder="例如: 高压线A-北段">
    <label>端点1 (纬度, 经度, 海拔m)</label>
    <div class="row"><input id="plLat1" placeholder="纬度"><input id="plLon1" placeholder="经度"><input id="plAlt1" placeholder="海拔"></div>
    <label>端点2 (纬度, 经度, 海拔m)</label>
    <div class="row"><input id="plLat2" placeholder="纬度"><input id="plLon2" placeholder="经度"><input id="plAlt2" placeholder="海拔"></div>
    <button class="btn-start" onclick="addPowerLine()">+ 添加</button>
    <button class="btn-secondary" onclick="closeModal('plModal')" style="margin-left:8px">关闭</button>
  </div>
</div>

<script>
let pollTimer;

function updateUI(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    let running=d.running;
    document.getElementById('dot').className='status-dot '+(running?'status-running':'status-stopped');
    document.getElementById('statusText').textContent=running?'运行中':'已停止';
    document.getElementById('droneCount').textContent=d.drone_count;
    document.getElementById('alertCount').textContent=d.alert_count;
    document.getElementById('plCount').textContent=d.pl_count;

    let table=document.getElementById('droneTable');
    table.innerHTML=d.drones.map(dr=>{
      let cls=dr.status||'active';
      let dist=dr.min_distance!=null?dr.min_distance.toFixed(0)+'m':'-';
      return `<tr><td>${dr.id||'?'}</td><td>多旋翼</td>
        <td>${(dr.last_lat||0).toFixed(5)}</td><td>${(dr.last_lon||0).toFixed(5)}</td>
        <td>${(dr.last_alt||0).toFixed(0)}m</td><td>${dist}</td>
        <td>${dr.line_name||'-'}</td><td class="${cls}">${cls}</td>
        <td>${(dr.last_seen||'').substring(11,19)}</td></tr>`;
    }).join('');

    // Log
    let log=document.getElementById('logPanel');
    log.innerHTML=d.logs.map(l=>`<span class="ts">[${l.time}]</span> <span class="${l.level}">${l.msg}</span>`).join('<br>');
    log.scrollTop=log.scrollHeight;

    document.getElementById('footerTime').textContent='更新: '+d.now;
  });
}

function startScan(){
  fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:document.getElementById('modeSelect').value})})
  .then(r=>r.json()).then(console.log);
}
function stopScan(){fetch('/api/stop',{method:'POST'}).then(r=>r.json()).then(console.log);}
function openPowerLines(){
  fetch('/api/powerlines').then(r=>r.json()).then(d=>renderPowerLines(d));
  document.getElementById('plModal').classList.add('active');
}
function renderPowerLines(lines){
  document.getElementById('plList').innerHTML=lines.map((l,i)=>`<div style="padding:6px;background:#1e1e2e;margin:2px 0;border-radius:4px;display:flex;justify-content:space-between">
    <span>${l.name}: (${l.lat1.toFixed(4)},${l.lon1.toFixed(4)},${l.alt1}m) -> (${l.lat2.toFixed(4)},${l.lon2.toFixed(4)},${l.alt2}m)</span>
    <button class="btn-stop" style="padding:2px 8px;font-size:11px" onclick="delPowerLine(${i})">X</button></div>`).join('');
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
    let html='<h2 style="color:#89b4fa;margin-bottom:10px">轨迹记录</h2><div style="max-height:400px;overflow-y:auto">';
    for(let did in d){
      html+=`<div style="margin:6px 0;padding:8px;background:#1e1e2e;border-radius:4px">
        <b>${did}</b>: ${d[did].count} 点, 最近距离 ${d[did].min_dist.toFixed(1)}m, ${d[did].first} -> ${d[did].last}</div>`;
    }
    html+='</div><button class="btn-secondary" onclick="closeModal(\'trajModal\')" style="margin-top:10px">关闭</button>';
    let m=document.createElement('div');m.className='modal active';m.id='trajModal';
    m.innerHTML=`<div class="modal-box">${html}</div>`;
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
    mode = data.get('mode', 'mock')
    if controller:
        controller.stop()
        controller = None
        time.sleep(0.5)
    controller = WebController(mode)
    controller._wifi_interface = data.get('interface')
    controller.start()
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
        from powerline import PowerLineSegment
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
    """Web 模式控制器 — 无 tkinter 依赖"""

    def __init__(self, mode='mock'):
        self.mode = mode
        self.running = False
        self._log_buffer = []
        self._receiver = None
        self._loop = None

        # Init DB
        config_path = PROJECT_ROOT / 'config' / 'config.yaml'
        import yaml
        self._config = yaml.safe_load(config_path.read_text(encoding='utf-8'))

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
            db=self.db, sms_backend=MockSMSBackend(),
            thresholds=thresholds,
            alert_contacts=self._config.get('alert_contacts', []),
            pilot_phones=self._config.get('pilot_phones', {}) or {},
        )

        traj_cfg = self._config.get('trajectory', {})
        self.trajectory_recorder = TrajectoryRecorder(
            db=self.db,
            min_interval=traj_cfg.get('min_interval', 2.0),
            max_points_per_drone=traj_cfg.get('max_points_per_drone', 1000),
        )
        
        self._thread = None

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

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
    
    def stop(self):
        self.running = False
        if self._loop and self._receiver:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._receiver.stop(), self._loop
                )
            except Exception:
                pass

    def _log(self, msg, level='info'):
        self._log_buffer.append({'time': datetime.now().strftime('%H:%M:%S'), 'msg': msg, 'level': level})
        if len(self._log_buffer) > 200:
            self._log_buffer = self._log_buffer[-100:]

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        from rid_receiver import MockRIDReceiver, BLE_RIDReceiver

        if self.mode == 'ble':
            self._receiver = BLE_RIDReceiver(
                callback=lambda p: self._on_rid(p),
                scan_duration=self._config.get('ble', {}).get('scan_duration', 5.0),
            )
        elif self.mode == 'wifi':
            from wifi_receiver import create_wifi_receiver
            self._receiver = create_wifi_receiver(
                callback=lambda p: self._on_rid(p),
                interface=getattr(self, '_wifi_interface', None),
            )
        else:
            self._receiver = MockRIDReceiver(
                callback=lambda p: self._on_rid(p),
                interval=1.0, num_drones=3,
            )

        async def runner():
            mode_names = {'mock': '模拟', 'ble': 'BLE 蓝牙', 'wifi': 'WiFi'}
            self._log(f"系统启动 ({mode_names.get(self.mode, self.mode)}模式)", "info")
            await self._receiver.start()

        try:
            self._loop.run_until_complete(runner())
        except Exception as e:
            self._log(f"错误: {e}", "crit")
        finally:
            self._loop.close()
            self._loop = None
    
    def _on_rid(self, parsed):
        if not self.running:
            return
        drone_id = parsed.drone_id
        if not drone_id or not parsed.location:
            return
        
        loc = parsed.location
        
        self.db.upsert_drone(drone_id, loc.latitude, loc.longitude,
                             loc.altitude_geodetic, loc.speed_horizontal)
        
        nearest_line, distance = self.pl_manager.find_nearest_line(
            loc.latitude, loc.longitude, loc.altitude_geodetic)
        
        if nearest_line:
            status = 'active'
            if distance <= self.thresholds.get('critical', 50):
                status = 'critical'
            elif distance <= self.thresholds.get('severe', 100):
                status = 'severe'
            elif distance <= self.thresholds.get('warning', 200):
                status = 'warning'
            
            self.db.update_drone_distance(drone_id, distance, nearest_line.line_id, status)
            
            if distance <= self.thresholds.get('warning', 200):
                level = self.alert_system.process(
                    drone_id, distance, nearest_line.name,
                    nearest_line.line_id, loc.altitude_geodetic,
                    loc.latitude, loc.longitude)
                if level:
                    emoji = {'critical': '🔴', 'severe': '🚨', 'warning': '⚠️'}.get(level, '!')
                    self._log(f"{emoji} [{level}] {drone_id} 距离 {nearest_line.name} {distance:.0f}m", level)
                
                self.trajectory_recorder.record(
                    drone_id, loc.latitude, loc.longitude,
                    loc.altitude_geodetic, distance, nearest_line.line_id)
            else:
                self.trajectory_recorder.stop_tracking(drone_id)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    
    global controller
    controller = WebController('mock')
    controller.start()
    
    print(f"\n  Drone RID Receiver Web GUI")
    print(f"  浏览器打开: http://{args.host}:{args.port}")
    print(f"  按 Ctrl+C 停止\n")
    
    try:
        app.run(host=args.host, port=args.port, debug=False)
    except KeyboardInterrupt:
        pass
    finally:
        if controller:
            controller.stop()


if __name__ == '__main__':
    main()
