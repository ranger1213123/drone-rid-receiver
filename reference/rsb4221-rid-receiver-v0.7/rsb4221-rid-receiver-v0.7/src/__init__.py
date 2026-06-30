#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""无人机 RID 接收装置 — 轻量级 Web 服务器 (Python 2.7 兼容版)

运行在 RSB-4221 AM3358 板子上。
不需要 Flask，使用 Python 2.7 标准库 BaseHTTPServer + json。
提供 REST API 和嵌入式 HTML 监控界面。

启动: python2 server_web.py
访问: http://192.168.8.76:5000
"""

import os
import sys
import json
import time
import math
import sqlite3
import threading
import logging
import subprocess
import base64
import re
from collections import defaultdict
try:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
except ImportError:
    from http.server import BaseHTTPRequestHandler, HTTPServer
try:
    from urlparse import urlparse, parse_qs
except ImportError:
    from urllib.parse import urlparse, parse_qs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("rid-web")


# ================================================================
#  配置
# ================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
DATA_DIR = os.path.join(BASE_DIR, "data")

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# ================================================================
#  drone_id 校验
# ================================================================

VALID_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$')

def is_valid_drone_id(drone_id):
    """返回 True 如果 drone_id 是合法的 ASCII 标识符"""
    if not drone_id or len(drone_id) > 32:
        return False
    return bool(VALID_ID_RE.match(drone_id))

# ================================================================
#  RID 解析器 (Python 2 兼容版)
# ================================================================

def parse_message_pack(pack_data):
    """解析 RID 消息包"""
    if not pack_data or len(pack_data) < 2:
        return {"messages": [], "error": "数据太短"}
    
    result = {
        "drone_id": None,
        "location": None,
        "messages": [],
    }
    
    # 简化解析：直接尝试提取 ASCII ID
    if len(pack_data) > 4:
        # 提取可能的 UAS ID (ASCII)
        for start in range(0, len(pack_data) - 2, 25):
            chunk = pack_data[start:start+25]
            if len(chunk) < 2:
                break
            msg_type = chunk[0] & 0x0F
            if msg_type == 0 and len(chunk) >= 22:
                # Basic ID: byte1 = id_type|ua_type, byte2-21 = UAS ID
                uas_id_bytes = chunk[2:22].split(b'\x00')[0]
                try:
                    uas_id = uas_id_bytes.decode('ascii', errors='replace')
                    if uas_id:
                        result["drone_id"] = uas_id
                except:
                    pass
            elif msg_type == 1 and len(chunk) >= 15:
                # Location: lat/lon at offset 6-13 (big-endian int32)
                try:
                    import struct
                    lat_raw = struct.unpack('>i', chunk[6:10])[0]
                    lon_raw = struct.unpack('>i', chunk[10:14])[0]
                    lat = lat_raw / 1e7
                    lon = lon_raw / 1e7
                    
                    alt_geo = None
                    if len(chunk) >= 16:
                        alt_raw = struct.unpack('>h', chunk[14:16])[0]
                        alt_geo = alt_raw * 0.5
                    
                    speed = None
                    if len(chunk) >= 22:
                        spd_raw = struct.unpack('>H', chunk[20:22])[0]
                        if spd_raw != 0xFFFF:
                            speed = spd_raw * 0.01
                    
                    heading = None
                    if len(chunk) >= 24:
                        hdg_raw = struct.unpack('>H', chunk[22:24])[0]
                        if hdg_raw != 0xFFFF:
                            heading = hdg_raw * 0.01
                    
                    result["location"] = {
                        "lat": lat, "lon": lon, "alt": alt_geo,
                        "speed": speed, "heading": heading,
                    }
                except:
                    pass
    
    return result


def mock_drone_data(drone_id, lat_base, lon_base, alt_base, t_offset):
    """生成模拟无人机数据"""
    angle = t_offset + hash(drone_id) % 10
    return {
        "drone_id": drone_id,
        "location": {
            "lat": lat_base + 0.002 * math.sin(angle * 0.1),
            "lon": lon_base + 0.002 * math.cos(angle * 0.1),
            "alt": alt_base + 10 * math.sin(angle * 0.05),
            "speed": 10 + 5 * math.sin(angle * 0.2),
            "heading": (angle * 3) % 360,
        },
        "source": "mock",
        "rssi": -50 - hash(drone_id) % 20,
    }


# ================================================================
#  电力线 + 距离计算
# ================================================================

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def meters_per_degree(lat):
    lat_rad = math.radians(lat)
    m_lat = 111132.954 - 559.822 * math.cos(2 * lat_rad)
    m_lon = (math.pi / 180.0) * 6378137.0 * math.cos(lat_rad)
    return m_lat, m_lon


def point_to_line_distance_3d(lat, lon, alt, line):
    ref_lat, ref_lon = line["lat1"], line["lon1"]
    
    m_lat, m_lon = meters_per_degree((ref_lat + line["lat2"]) / 2)
    
    # 平面坐标
    x1, y1 = 0, 0
    x2 = (line["lon2"] - ref_lon) * m_lon
    y2 = (line["lat2"] - ref_lat) * m_lat
    z1, z2 = line["alt1"], line["alt2"]
    
    dx = (lon - ref_lon) * m_lon
    dy = (lat - ref_lat) * m_lat
    dz = alt
    
    seg_x = x2 - x1
    seg_y = y2 - y1
    seg_z = z2 - z1
    seg_len_sq = seg_x*seg_x + seg_y*seg_y + seg_z*seg_z
    
    if seg_len_sq < 1e-12:
        dist_h = math.sqrt(dx*dx + dy*dy)
        dist_v = abs(dz - z1)
        total = math.sqrt(dist_h*dist_h + dist_v*dist_v)
        return total, dist_h, dist_v, ref_lat, ref_lon, z1, 0
    
    t = ((dx - x1)*seg_x + (dy - y1)*seg_y + (dz - z1)*seg_z) / seg_len_sq
    t = max(0.0, min(1.0, t))
    
    cx, cy, cz = x1 + t*seg_x, y1 + t*seg_y, z1 + t*seg_z
    
    clat = ref_lat + (cy / m_lat) if abs(m_lat) > 1e-12 else ref_lat
    clon = ref_lon + (cx / m_lon) if abs(m_lon) > 1e-12 else ref_lon
    
    dist_h = math.sqrt((dx - cx)**2 + (dy - cy)**2)
    dist_v = abs(dz - cz)
    total = math.sqrt(dist_h*dist_h + dist_v*dist_v)
    
    return total, dist_h, dist_v, clat, clon, cz, t


# ================================================================
#  数据库
# ================================================================

class Database(object):
    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()
    
    def _init_tables(self):
        with self._lock:
            c = self.conn.cursor()
            c.executescript("""
                CREATE TABLE IF NOT EXISTS drones (
                    drone_id TEXT PRIMARY KEY,
                    mac TEXT, name TEXT,
                    lat REAL DEFAULT 0, lon REAL DEFAULT 0, alt REAL DEFAULT 0,
                    speed REAL DEFAULT 0, heading REAL DEFAULT 0,
                    rssi INTEGER DEFAULT 0, min_distance REAL DEFAULT 999999,
                    nearest_line_id INTEGER, status TEXT DEFAULT 'active',
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS power_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    lat1 REAL NOT NULL, lon1 REAL NOT NULL, alt1 REAL DEFAULT 0,
                    lat2 REAL NOT NULL, lon2 REAL NOT NULL, alt2 REAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS trajectories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drone_id TEXT NOT NULL, lat REAL, lon REAL, alt REAL,
                    distance_to_line REAL, line_id INTEGER,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drone_id TEXT, level TEXT, distance REAL,
                    line_id INTEGER, line_name TEXT,
                    sms_pilot INTEGER DEFAULT 0, sms_staff INTEGER DEFAULT 0,
                    message TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_traj_drone ON trajectories(drone_id);
                CREATE INDEX IF NOT EXISTS idx_alert_drone ON alerts(drone_id);
            """)
            self.conn.commit()
    
    def upsert_drone(self, drone_id, lat=0, lon=0, alt=0, speed=0, heading=0,
                     rssi=0, min_distance=999999, status="active", mac="", name=""):
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            # SQLite 3.11.0 不支持 ON CONFLICT，改用 INSERT OR REPLACE
            self.conn.execute("""
                INSERT OR REPLACE INTO drones
                    (drone_id, mac, name, lat, lon, alt, speed,
                     heading, rssi, min_distance, status, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (drone_id, mac, name, lat, lon, alt, speed, heading,
                  rssi, min_distance, status, now))
            self.conn.commit()
    
    def get_active_drones(self):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM drones ORDER BY min_distance ASC"
            ).fetchall()
        return [dict(r) for r in rows]
    
    def add_power_line(self, name, lat1, lon1, alt1, lat2, lon2, alt2):
        with self._lock:
            c = self.conn.execute("""
                INSERT INTO power_lines (name, lat1, lon1, alt1, lat2, lon2, alt2)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (name, lat1, lon1, alt1, lat2, lon2, alt2))
            self.conn.commit()
            return c.lastrowid
    
    def get_power_lines(self):
        with self._lock:
            rows = self.conn.execute("SELECT * FROM power_lines").fetchall()
        return [dict(r) for r in rows]
    
    def delete_power_line(self, line_id):
        with self._lock:
            c = self.conn.execute("DELETE FROM power_lines WHERE id=?", (line_id,))
            self.conn.commit()
            return c.rowcount > 0
    
    def insert_alert(self, drone_id, level, distance, line_id=None, line_name="",
                     sms_pilot=0, sms_staff=0, message=""):
        with self._lock:
            self.conn.execute("""
                INSERT INTO alerts (drone_id, level, distance, line_id, line_name,
                    sms_pilot, sms_staff, message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (drone_id, level, distance, line_id, line_name,
                  sms_pilot, sms_staff, message))
            self.conn.commit()
    
    def get_recent_alerts(self, limit=50):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    
    def insert_trajectory(self, drone_id, lat, lon, alt, distance_to_line=None, line_id=None):
        with self._lock:
            self.conn.execute("""
                INSERT INTO trajectories (drone_id, lat, lon, alt, distance_to_line, line_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (drone_id, lat, lon, alt, distance_to_line, line_id))
            self.conn.commit()
    
    def get_stats(self):
        with self._lock:
            dc = self.conn.execute("SELECT COUNT(*) as c FROM drones WHERE status='active'").fetchone()["c"]
            ac = self.conn.execute("SELECT COUNT(*) as c FROM alerts").fetchone()["c"]
            plc = self.conn.execute("SELECT COUNT(*) as c FROM power_lines").fetchone()["c"]
            tc = self.conn.execute("SELECT COUNT(*) as c FROM trajectories").fetchone()["c"]
        return {"active_drones": dc, "total_alerts": ac, "total_power_lines": plc, "total_trajectory_points": tc}


# ================================================================
#  告警系统
# ================================================================

class AlertSystem(object):
    def __init__(self, config=None):
        self.config = config or {}
        self.thresholds = {
            "warning": self.config.get("warning", 200),
            "severe": self.config.get("severe", 100),
            "critical": self.config.get("critical", 50),
        }
        self.cooldowns = {"warning": 120, "severe": 60, "critical": 30}
        self.last_alerts = {}
        self.current_levels = {}
    
    def evaluate(self, distance_m):
        if distance_m <= self.thresholds["critical"]:
            return "critical"
        elif distance_m <= self.thresholds["severe"]:
            return "severe"
        elif distance_m <= self.thresholds["warning"]:
            return "warning"
        return "safe"
    
    def process(self, drone_id, distance_m, line_name="", lat=0, lon=0, alt=0):
        level = self.evaluate(distance_m)
        now = time.time()
        
        if level == "safe":
            self.current_levels.pop(drone_id, None)
            return None
        
        prev_level = self.current_levels.get(drone_id, "safe")
        level_order = {"safe": 0, "warning": 1, "severe": 2, "critical": 3}
        if level_order[level] < level_order[prev_level]:
            level = prev_level
            if level == "safe":
                return None
        
        self.current_levels[drone_id] = level
        
        last = self.last_alerts.get(drone_id, {}).get(level, 0)
        cd = self.cooldowns.get(level, 30)
        if now - last < cd:
            return None
        
        if drone_id not in self.last_alerts:
            self.last_alerts[drone_id] = {}
        self.last_alerts[drone_id][level] = now
        
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        threshold = self.thresholds[level]
        
        level_labels = {
            "warning": "注意", "severe": "严重警告", "critical": "立即驱离"
        }
        
        msg = "[%s] 无人机 %s 距离 %s %.1fm (阈值: %dm)" % (
            level_labels[level], drone_id, line_name, distance_m, threshold
        )
        
        logger.warning(msg)
        
        return {
            "drone_id": drone_id, "level": level, "distance": distance_m,
            "line_name": line_name, "timestamp": ts, "message": msg,
        }


# ================================================================
#  全局状态
# ================================================================

DB_PATH = os.path.join(DATA_DIR, "rid.db")
db = Database(DB_PATH)
alert_sys = AlertSystem({"warning": 200, "severe": 100, "critical": 50})
scanner_running = False
alert_logs = []
STATUS_LOCK = threading.Lock()


# ================================================================
#  扫描线程
# ================================================================

def scan_thread_func():
    global scanner_running
    
    logger.info("扫描线程启动")
    
    mock_drones = [
        {"id": "DRONE-A001", "mac": "04:D6:AA:11:22:33", "lat": 39.9042, "lon": 116.4074, "alt": 150},
        {"id": "DRONE-B002", "mac": "60:60:1F:44:55:66", "lat": 39.9092, "lon": 116.4124, "alt": 200},
        {"id": "DRONE-C003", "mac": "34:D2:62:77:88:99", "lat": 39.8942, "lon": 116.4024, "alt": 100},
    ]
    
    while scanner_running:
        try:
            t = time.time()
            for d in mock_drones:
                drone_data = mock_drone_data(d["id"], d["lat"], d["lon"], d["alt"], t)
                process_drone_data(drone_data)
            time.sleep(2)
        except:
            time.sleep(2)
    
    logger.info("扫描线程停止")


def process_drone_data(drone_data):
    try:
        drone_id = drone_data.get("drone_id") or ""
        if not is_valid_drone_id(drone_id):
            logger.debug("过滤无效 drone_id: %s" % repr(drone_id))
            return
        loc = drone_data.get("location", None)
        has_location = bool(loc and loc.get("lat") is not None)
        
        lat = loc.get("lat", 0) or 0 if loc else 0
        lon = loc.get("lon", 0) or 0 if loc else 0
        alt = loc.get("alt", 0) or 0 if loc else 0
        speed = loc.get("speed", 0) or 0 if loc else 0
        heading = loc.get("heading", 0) or 0 if loc else 0
        
        if not drone_id:
            return
        
        # 加载电力线并计算距离
        lines = db.get_power_lines()
        min_distance = 999999
        nearest_line = None
        
        if has_location:
            for line in lines:
                total, dh, dv, clat, clon, calt, t = point_to_line_distance_3d(
                    lat, lon, alt, line
                )
                if total < min_distance:
                    min_distance = total
                    nearest_line = line
        
        # 告警 (仅在有位置信息时)
        if has_location and nearest_line and min_distance < 200:
            alert_data = alert_sys.process(
                drone_id, min_distance, nearest_line["name"],
                lat=lat, lon=lon, alt=alt
            )
            if alert_data:
                with STATUS_LOCK:
                    alert_logs.append(alert_data)
                    while len(alert_logs) > 50:
                        alert_logs.pop(0)
                db.insert_alert(
                    drone_id, alert_data["level"], min_distance,
                    line_id=nearest_line["id"],
                    line_name=nearest_line["name"],
                    message=alert_data["message"]
                )
            
            # 记录轨迹
            db.insert_trajectory(drone_id, lat, lon, alt,
                                distance_to_line=min_distance,
                                line_id=nearest_line["id"])
        
        # 状态
        status = "active"
        if has_location:
            if min_distance < 50:
                status = "critical"
            elif min_distance < 100:
                status = "severe"
            elif min_distance < 200:
                status = "warning"
        
        db.upsert_drone(drone_id, lat, lon, alt, speed, heading,
                        rssi=drone_data.get("rssi", 0),
                        min_distance=min_distance, status=status,
                        mac=drone_data.get("mac", ""))
        
    except Exception as e:
        logger.error("处理无人机数据出错: %s" % str(e))


# ================================================================
#  Web 服务器
# ================================================================

HTML_INDEX = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>无人机 RID 接收装置</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'Segoe UI',Arial,sans-serif; background:#0a0e17; color:#e0e0e0; }
.header { background:linear-gradient(90deg,#0d1b2a,#1b2838); padding:16px 24px; border-bottom:1px solid #1e3a5f; display:flex; justify-content:space-between; align-items:center; }
.header h1 { font-size:20px; color:#4fc3f7; }
.stats { display:flex; gap:16px; font-size:13px; }
.stats span { padding:4px 12px; border-radius:4px; background:#1a2332; }
.container { display:flex; gap:16px; padding:16px; height:calc(100vh - 100px); }
.panel { background:#111926; border:1px solid #1e3a5f; border-radius:8px; overflow:hidden; display:flex; flex-direction:column; }
.ptitle { background:#162030; padding:10px 16px; font-size:14px; font-weight:bold; border-bottom:1px solid #1e3a5f; }
.left { flex:1; }
.right { width:400px; }
table { width:100%; border-collapse:collapse; }
th { background:#162030; padding:8px 12px; text-align:left; font-size:12px; color:#8899aa; }
td { padding:8px 12px; font-size:12px; border-bottom:1px solid #162030; }
.critical { color:#f44336; font-weight:bold; }
.severe { color:#ff9800; font-weight:bold; }
.warning { color:#ffeb3b; }
.active { color:#4caf50; }
.log { padding:6px 12px; font-size:12px; border-bottom:1px solid #111; }
.log-lvl { font-weight:bold; }
.controls { padding:12px; display:flex; gap:8px; }
.controls button { padding:8px 16px; border:1px solid #1e3a5f; border-radius:4px; background:#162030; color:#e0e0e0; cursor:pointer; }
button:disabled { opacity:0.4; }
.btn-start { background:#1b5e20 !important; }
.btn-stop { background:#b71c1c !important; }
.footer { text-align:center; padding:8px; font-size:11px; color:#444; }
.scroll { overflow-y:auto; flex:1; }
</style>
</head>
<body>
<div class="header">
<h1>&#x1f6f8; 无人机 RID 监控</h1>
<div class="stats">
<span>&#x1f6f8; <span id="dc">0</span></span>
<span>&#x1f6a8; <span id="ac">0</span></span>
<span>&#x26a1; <span id="pc">0</span></span>
</div>
</div>
<div class="container">
<div class="left panel">
<div class="ptitle">&#x1f6f8; 在线无人机 <span id="dbadge"></span></div>
<div class="scroll">
<table><thead><tr>
<th>#</th><th>ID</th><th>纬度</th><th>经度</th><th>高度(m)</th><th>距离(m)</th><th>状态</th>
</tr></thead>
<tbody id="tbody"></tbody>
</table>
</div>
</div>
<div class="right panel">
<div class="ptitle">&#x1f4cb; 控制</div>
<div class="controls">
<button class="btn-start" onclick="startScan()">&#x25b6; 启动</button>
<button class="btn-stop" disabled onclick="stopScan()">&#x23f9; 停止</button>
<button onclick="refresh()">&#x1f504; 刷新</button>
</div>
<div class="ptitle">&#x1f4cb; 告警日志</div>
<div class="scroll" id="logs"></div>
<div class="ptitle">&#x26a1; 电力线</div>
<div class="scroll" id="plist"></div>
</div>
</div>
<div class="footer">无人机 RID 接收装置 | RSB-4221 AM3358 | <span id="time">-</span></div>
<script>
var iv = null;
function startScan(){fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"mode":"mock"}'}).then(function(){iv=setInterval(refresh,2000);refresh();});}
function stopScan(){fetch('/api/stop',{method:'POST'}).then(function(){if(iv){clearInterval(iv);iv=null;}refresh();});}
function refresh(){fetch('/api/status').then(function(r){return r.json()}).then(function(d){
document.getElementById('dc').textContent=(d.drone_count||0);
document.getElementById('ac').textContent=(d.alert_count||0);
document.getElementById('pc').textContent=(d.pl_count||0);
document.getElementById('dbadge').textContent='('+(d.drone_count||0)+')';
document.getElementById('time').textContent=d.now;
var tb=document.getElementById('tbody');tb.innerHTML='';
(d.drones||[]).forEach(function(dr,i){
var s=dr.status||'active',dist=dr.min_distance>=999990?'-':dr.min_distance.toFixed(1);
var did=dr.drone_id||dr.id||'?';tb.innerHTML+='<tr><td>'+(i+1)+'</td><td>'+did.substring(0,12)+'</td><td>'+(dr.lat||0).toFixed(4)+'</td><td>'+(dr.lon||0).toFixed(4)+'</td><td>'+(dr.alt||0)+'</td><td>'+dist+'</td><td class="'+s+'">'+s+'</td></tr>';
});
if(!(d.drones||[]).length)tb.innerHTML='<tr><td colspan="7" style="text-align:center;color:#666;padding:20px;">暂无数据</td></tr>';
var lg=document.getElementById('logs');lg.innerHTML='';
(d.logs||[]).forEach(function(l){lg.innerHTML+='<div class="log"><span class="log-lvl" style="color:#'+(l.level=='critical'?'f44336':l.level=='severe'?'ff9800':'ffeb3b')+'">['+l.level+']</span> '+l.message+'</div>';});
var pl=document.getElementById('plist');
if(d.power_lines&&d.power_lines.length){pl.innerHTML='';d.power_lines.forEach(function(p){pl.innerHTML+='<div class="log">\\u26a1 '+p.name+'</div>';});}
else pl.innerHTML='<div class="log" style="color:#666;">暂无电力线</div>';
});}
refresh();
</script>
</body>
</html>"""


class RIDHandler(BaseHTTPRequestHandler):
    
    def log_message(self, format, *args):
        logger.debug(format % args)
    
    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, indent=2)
        if isinstance(body, unicode):
            body_bytes = body.encode("utf-8")
        else:
            body_bytes = body
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", len(body_bytes))
        self.end_headers()
        self.wfile.write(body_bytes)
    
    def _send_html(self, html, code=200):
        body = html.encode("utf-8") if isinstance(html, unicode) else html.decode("utf-8").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
    
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        
        if path == "/":
            self._send_html(HTML_INDEX)
        elif path == "/api/status":
            self._handle_status()
        elif path == "/api/drones":
            self._handle_drones()
        elif path == "/api/powerlines":
            self._handle_get_powerlines()
        elif path == "/api/alerts":
            self._handle_alerts()
        elif path in ("/api/start", "/api/stop"):
            self._send_json({"error": "use POST"}, 405)
        else:
            self._send_json({"error": "not found"}, 404)
    
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        
        if path == "/api/start":
            self._handle_start()
        elif path == "/api/stop":
            self._handle_stop()
        elif path == "/api/powerlines":
            self._handle_add_powerline()
        else:
            self._send_json({"error": "not found"}, 404)
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    
    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            return self.rfile.read(length).decode("utf-8", errors="replace")
        return "{}"
    
    # ---- 路由处理 ----
    
    def _handle_status(self):
        global scanner_running
        
        drones = db.get_active_drones()
        drone_list = []
        for d in drones:
            did = d.get("drone_id", "")
            if not is_valid_drone_id(did):
                continue
            drone_list.append({
                "id": did,
                "lat": d.get("lat", 0),
                "lon": d.get("lon", 0),
                "alt": d.get("alt", 0),
                "speed": d.get("speed", 0),
                "heading": d.get("heading", 0),
                "min_distance": d.get("min_distance", 999999),
                "status": d.get("status", "active"),
            })
        
        lines = db.get_power_lines()
        
        # 格式化日志
        logs_formatted = []
        with STATUS_LOCK:
            for log in alert_logs[-20:]:
                logs_formatted.append({
                    "level": log["level"],
                    "message": log.get("message", ""),
                })
        
        stats = db.get_stats()
        
        self._send_json({
            "running": scanner_running,
            "drone_count": len(drone_list),
            "alert_count": stats["total_alerts"],
            "pl_count": len(lines),
            "drones": drone_list,
            "logs": logs_formatted,
            "power_lines": lines,
            "now": time.strftime("%H:%M:%S"),
        })
    
    def _handle_start(self):
        global scanner_running
        if not scanner_running:
            scanner_running = True
            scanner_running = True
            t = threading.Thread(target=scan_thread_func)
            t.daemon = True
            t.start()
        self._send_json({"status": "started"})
    
    def _handle_stop(self):
        global scanner_running
        scanner_running = False
        logger.info("扫描已停止")
        self._send_json({"status": "stopped"})
    
    def _handle_drones(self):
        drones = db.get_active_drones()
        self._send_json(drones)
    
    def _handle_get_powerlines(self):
        lines = db.get_power_lines()
        self._send_json(lines)
    
    def _handle_add_powerline(self):
        body = self._read_body()
        try:
            data = json.loads(body)
        except:
            self._send_json({"error": "invalid json"}, 400)
            return
        
        name = data.get("name", "")
        lat1 = data.get("lat1", 0)
        lon1 = data.get("lon1", 0)
        alt1 = data.get("alt1", 0)
        lat2 = data.get("lat2", 0)
        lon2 = data.get("lon2", 0)
        alt2 = data.get("alt2", 0)
        
        if not name:
            self._send_json({"error": "名称不能为空"}, 400)
            return
        
        line_id = db.add_power_line(name, lat1, lon1, alt1, lat2, lon2, alt2)
        self._send_json({"status": "added", "id": line_id})
    
    def _handle_alerts(self):
        alerts = db.get_recent_alerts(limit=50)
        self._send_json(alerts)


def main():
    # 解析命令行参数
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000, help="监听端口")
    args = parser.parse_args()
    port = args.port
    
    # 启动 HTTP 服务器
    # 重写 HTTPServer 以绕过 socket.getfqdn() 卡死在 Arago DNS 的问题
    class FastHTTPServer(HTTPServer):
        def server_bind(self):
            import SocketServer
            SocketServer.TCPServer.server_bind(self)
            host, port = self.socket.getsockname()[:2]
            self.server_name = host
    
    server = FastHTTPServer(("0.0.0.0", port), RIDHandler)
    logger.info("Web 服务器启动: http://0.0.0.0:%d" % port)
    logger.info("电力线数: %d" % len(db.get_power_lines()))
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        logger.info("服务器已关闭")


if __name__ == "__main__":
    main()
