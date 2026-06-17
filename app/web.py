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

from functools import wraps
from flask import Flask, render_template, jsonify, request, session, redirect, url_for

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from logging_config import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = SCRIPT_DIR.parent

from core.parser.types import UA_TYPE_NAMES, lookup_model_by_sn

app = Flask(__name__, template_folder=str(PROJECT_ROOT / 'templates'))


def _safe_float(val, default=0.0):
    """安全转换为 float，失败返回 default"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ── 加载 Web 认证配置 ──
_web_auth_cfg = {}
try:
    from core.config import load_config
    _cfg = load_config(str(PROJECT_ROOT / 'config' / 'config.yaml'))
    _web_auth_cfg = _cfg.get('web_auth', {})
except Exception:
    pass

_fallback_key = os.urandom(24).hex()
app.secret_key = _web_auth_cfg.get('secret_key', _fallback_key)

def _load_web_users():
    """从 web_users.yaml 加载用户列表，失败则回退到 config.yaml 默认值"""
    import yaml
    users_file = PROJECT_ROOT / 'config' / 'web_users.yaml'
    try:
        if users_file.exists():
            with open(str(users_file), 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
                users = data.get('users', [])
                if users:
                    return users
    except Exception:
        pass
    return _web_auth_cfg.get('users', [
        {'username': 'admin', 'password': 'admin123', 'role': 'admin'},
    ])

def _save_web_users(users):
    """将用户列表写回 web_users.yaml"""
    import yaml
    users_file = PROJECT_ROOT / 'config' / 'web_users.yaml'
    with open(str(users_file), 'w', encoding='utf-8') as f:
        yaml.dump({'users': users}, f, allow_unicode=True, default_flow_style=False)

WEB_USERS = _load_web_users()

def require_web_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        if session['user'].get('role') != 'admin':
            return jsonify({'error': '需要管理员权限'}), 403
        return f(*args, **kwargs)
    return decorated

# ── 全局状态 ──
controller = None

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ''
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        for u in WEB_USERS:
            if u['username'] == username and u['password'] == password:
                session['user'] = {
                    'username': username,
                    'role': u.get('role', 'user'),
                    'station': u.get('station', ''),
                }
                return redirect(url_for('index'))
        error = '用户名或密码错误'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@require_web_auth
def index():
    return render_template('map.html')


@app.route('/list')
@require_web_auth
def list_view():
    return render_template('dashboard.html')

@app.route('/api/status')
@require_web_auth
def api_status():
    global controller
    drones = controller.db.get_active_drones() if controller else []
    alert_drones = dict(controller.alert_system._drone_level) if controller else {}

    # Add power line names, model names to drones
    if controller:
        line_names = {l.line_id: l.name for l in controller.pl_manager.lines}
        for d in drones:
            d['category_name'] = UA_TYPE_NAMES.get(d.get('ua_type', 0), '未知')
            d['product_model'] = lookup_model_by_sn(d['id']) or ''
            line_id = d.get('nearest_line_id')
            d['line_name'] = line_names.get(line_id, '') if line_id else ''

    logs = controller._log_buffer[-50:] if controller and hasattr(controller, '_log_buffer') else []

    bhaul = controller.backhaul if controller else None
    user = session.get('user', {})
    return jsonify({
        'running': controller.running if controller else False,
        'mode': controller.mode if controller else '',
        'drone_count': len(drones),
        'alert_count': len(alert_drones),
        'pl_count': len(controller.pl_manager.lines) if controller else 0,
        'drones': drones,
        'logs': logs,
        'now': datetime.now().strftime('%H:%M:%S'),
        'current_user': {
            'username': user.get('username', ''),
            'role': user.get('role', 'user'),
            'station': user.get('station', ''),
        },
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
@require_web_auth
def api_start():
    global controller
    data = request.json or {}
    mode = data.get('mode', 'ble')
    if controller is None:
        controller = WebController()
    controller.switch_mode(mode, wifi_interface=data.get('interface'))
    return jsonify({'status': 'ok', 'mode': mode})

@app.route('/api/stop', methods=['POST'])
@require_web_auth
def api_stop():
    global controller
    if controller:
        controller.stop()
    return jsonify({'status': 'ok'})

@app.route('/api/powerlines', methods=['GET', 'POST', 'DELETE'])
@require_web_auth
def api_powerlines():
    global controller
    if not controller:
        return jsonify([])
    if request.method == 'GET':
        return jsonify([{
            'name': l.name, 'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
            'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2,
            'voltage_level': getattr(l, 'voltage_level', ''),
        } for l in controller.pl_manager.lines])
    elif request.method == 'POST':
        data = request.json or {}
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'error': '电力线名称不能为空'}), 400
        from core.powerline import PowerLineSegment
        voltage_level = (data.get('voltage_level') or '').strip()
        seg = PowerLineSegment(
            name=name,
            lat1=_safe_float(data.get('lat1')), lon1=_safe_float(data.get('lon1')),
            alt1=_safe_float(data.get('alt1')),
            lat2=_safe_float(data.get('lat2')), lon2=_safe_float(data.get('lon2')),
            alt2=_safe_float(data.get('alt2')),
            line_id=len(controller.pl_manager.lines) + 1,
            voltage_level=voltage_level,
        )
        controller.pl_manager.lines.append(seg)
        controller.db.load_power_lines([{
            'name': l.name, 'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
            'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2, 'id': l.line_id,
            'voltage_level': getattr(l, 'voltage_level', ''),
        } for l in controller.pl_manager.lines])
        controller._save_power_lines_to_yaml()
        return jsonify({'status': 'ok'})

@app.route('/api/powerlines/<int:idx>', methods=['DELETE', 'PUT'])
@require_web_auth
def api_modify_powerline(idx):
    global controller
    if not controller:
        return jsonify({'error': '系统未启动'}), 503

    if request.method == 'DELETE':
        if idx < len(controller.pl_manager.lines):
            del controller.pl_manager.lines[idx]
            controller.db.load_power_lines([{
                'name': l.name, 'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
                'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2, 'id': i+1,
                'voltage_level': getattr(l, 'voltage_level', ''),
            } for i, l in enumerate(controller.pl_manager.lines)])
            controller._save_power_lines_to_yaml()
        return jsonify({'status': 'ok'})

    if request.method == 'PUT':
        if idx >= len(controller.pl_manager.lines):
            return jsonify({'error': '无效的电力线索引'}), 404
        data = request.json or {}
        line = controller.pl_manager.lines[idx]
        if 'name' in data:
            line.name = data['name'].strip()
        if 'voltage_level' in data:
            line.voltage_level = data['voltage_level'].strip()
        for attr in ['alt1', 'alt2', 'lat1', 'lon1', 'lat2', 'lon2']:
            if attr in data and data[attr] is not None:
                setattr(line, attr, _safe_float(data[attr]))
        controller.db.load_power_lines([{
            'name': l.name, 'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
            'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2, 'id': i+1,
            'voltage_level': getattr(l, 'voltage_level', ''),
        } for i, l in enumerate(controller.pl_manager.lines)])
        controller._save_power_lines_to_yaml()
        return jsonify({'status': 'ok'})


@app.route('/api/stations', methods=['GET', 'POST', 'DELETE'])
@require_web_auth
def api_stations():
    global controller
    if not controller:
        return jsonify([])

    if request.method == 'GET':
        return jsonify(controller.stations)

    # POST / DELETE require admin
    if session['user'].get('role') != 'admin':
        return jsonify({'error': '需要管理员权限'}), 403

    if request.method == 'POST':
        data = request.json or {}
        name = (data.get('name', '') or '').strip()
        location = (data.get('location', '') or '').strip()
        lat = _safe_float(data.get('lat'))
        lon = _safe_float(data.get('lon'))
        alt = _safe_float(data.get('alt'))
        if not name:
            return jsonify({'error': '站点名称不能为空'}), 400
        controller.stations.append({
            'name': name, 'location': location,
            'lat': lat, 'lon': lon, 'alt': alt,
        })
        controller._save_stations_to_yaml()
        return jsonify({'status': 'ok'})

    if request.method == 'DELETE':
        data = request.json or {}
        idx = data.get('idx')
        if idx is None:
            return jsonify({'error': '缺少 idx 参数'}), 400
        idx = int(idx)
        if 0 <= idx < len(controller.stations):
            del controller.stations[idx]
            controller._save_stations_to_yaml()
            return jsonify({'status': 'ok'})
        return jsonify({'error': '无效的站点索引'}), 404


@app.route('/api/users', methods=['GET', 'POST', 'DELETE'])
@require_web_auth
def api_users():
    global WEB_USERS
    if request.method == 'GET':
        # 返回用户列表，隐藏密码字段
        return jsonify([{
            'username': u.get('username', ''),
            'role': u.get('role', 'user'),
            'station': u.get('station', ''),
        } for u in WEB_USERS])

    # POST / DELETE 需管理员权限
    if session['user'].get('role') != 'admin':
        return jsonify({'error': '需要管理员权限'}), 403

    if request.method == 'POST':
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        role = data.get('role', 'user').strip()
        station = data.get('station', '').strip()
        if not username or not password:
            return jsonify({'error': '用户名和密码不能为空'}), 400
        # 检查是否已存在
        if any(u['username'] == username for u in WEB_USERS):
            return jsonify({'error': '用户名已存在'}), 409
        WEB_USERS.append({
            'username': username, 'password': password,
            'role': role, 'station': station,
        })
        _save_web_users(WEB_USERS)
        return jsonify({'status': 'ok'})

    if request.method == 'DELETE':
        data = request.json or {}
        username = data.get('username', '').strip()
        if not username:
            return jsonify({'error': '缺少 username 参数'}), 400
        # 禁止删除最后一个管理员
        admins = [u for u in WEB_USERS if u.get('role') == 'admin']
        target = next((u for u in WEB_USERS if u['username'] == username), None)
        if target and target.get('role') == 'admin' and len(admins) <= 1:
            return jsonify({'error': '不能删除最后一个管理员账户'}), 400
        WEB_USERS = [u for u in WEB_USERS if u['username'] != username]
        _save_web_users(WEB_USERS)
        return jsonify({'status': 'ok'})


@app.route('/api/alerts/history')
@require_web_auth
def api_alerts_history():
    """查询历史告警记录，支持筛选"""
    global controller
    if not controller:
        return jsonify([])
    level = request.args.get('level', '').strip() or None
    drone_id = request.args.get('drone_id', '').strip() or None
    since = request.args.get('since', '').strip() or None
    to_date = request.args.get('to', '').strip() or None
    limit = min(int(request.args.get('limit', 200)), 1000)
    rows = controller.db.get_recent_alerts(limit=limit, level=level,
                                            drone_id=drone_id, since=since,
                                            to_date=to_date)
    line_names = {}
    if controller.pl_manager:
        line_names = {l.line_id: l.name for l in controller.pl_manager.lines}
    return jsonify([{
        'id': r['id'],
        'drone_id': r['drone_id'],
        'timestamp': r['timestamp'][:19] if r['timestamp'] else '',
        'level': r['level'],
        'distance': r['distance'],
        'line_name': line_names.get(r.get('line_id'), ''),
        'message': r.get('message', ''),
        'acknowledged': bool(r.get('acknowledged', 0)),
        'ack_time': (r.get('ack_time') or '')[:19],
        'ack_by': r.get('ack_by', ''),
        'ack_note': r.get('ack_note', ''),
    } for r in rows])


@app.route('/api/alerts/<int:alert_id>/acknowledge', methods=['POST'])
@require_web_auth
def api_acknowledge_alert(alert_id):
    """确认告警"""
    global controller
    if not controller:
        return jsonify({'error': '系统未启动'}), 503
    user = session.get('user', {})
    note = (request.json or {}).get('note', '').strip()
    controller.db.acknowledge_alert(alert_id, user.get('username', 'system'), note)
    return jsonify({'status': 'ok'})


@app.route('/api/alerts/export')
@require_web_auth
def api_alerts_export():
    """导出告警历史为 CSV"""
    global controller
    if not controller:
        return jsonify({'error': '系统未启动'}), 503
    import csv, io
    level = request.args.get('level', '').strip() or None
    since = request.args.get('since', '').strip() or None
    rows = controller.db.get_recent_alerts(limit=5000, level=level, since=since)
    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(['ID', '时间', '无人机ID', '等级', '距离(m)', '电力线', '已确认', '确认人', '确认时间'])
    line_names = {l.line_id: l.name for l in controller.pl_manager.lines}
    for r in rows:
        w.writerow([
            r['id'], r['timestamp'][:19] if r['timestamp'] else '',
            r['drone_id'], r['level'], f"{r['distance']:.1f}" if r['distance'] else '',
            line_names.get(r.get('line_id'), ''), '是' if r.get('acknowledged') else '否',
            r.get('ack_by', ''), (r.get('ack_time') or '')[:19],
        ])
    resp = app.make_response(si.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8-sig'
    resp.headers['Content-Disposition'] = 'attachment; filename=alerts_export.csv'
    return resp


@app.route('/api/drones/export')
@require_web_auth
def api_drones_export():
    """导出活跃无人机列表为 CSV"""
    global controller
    if not controller:
        return jsonify({'error': '系统未启动'}), 503
    import csv, io
    from core.parser.types import UA_TYPE_NAMES, lookup_model_by_sn
    drones = controller.db.get_active_drones()
    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(['无人机ID', '推测型号', '类型', '纬度', '经度', '海拔(m)', '速度(m/s)', '航向', '状态', '最近距离(m)', '最近电力线', '最后更新'])
    line_names = {l.line_id: l.name for l in controller.pl_manager.lines}
    for d in drones:
        w.writerow([
            d['id'], lookup_model_by_sn(d['id']) or '', UA_TYPE_NAMES.get(d.get('ua_type', 0), ''),
            f"{d['last_lat']:.6f}" if d.get('last_lat') else '',
            f"{d['last_lon']:.6f}" if d.get('last_lon') else '',
            f"{d['last_alt']:.1f}" if d.get('last_alt') else '',
            f"{d.get('speed', 0):.1f}", f"{d.get('heading', 0):.0f}",
            d.get('status', 'active'), f"{d.get('min_distance', 0):.0f}",
            line_names.get(d.get('nearest_line_id'), ''), (d.get('last_seen') or '')[:19],
        ])
    resp = app.make_response(si.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8-sig'
    resp.headers['Content-Disposition'] = 'attachment; filename=drones_export.csv'
    return resp


@app.route('/api/powerlines/import', methods=['POST'])
@require_web_auth
def api_import_powerlines():
    """批量导入电力线 — 支持 JSON 数组或 CSV 文本"""
    global controller
    if not controller:
        return jsonify({'error': '系统未启动'}), 503
    data = request.json or {}
    items = data.get('items', [])
    csv_text = data.get('csv', '').strip()

    # CSV 模式: 文本解析
    if csv_text and not items:
        import csv, io
        reader = csv.DictReader(io.StringIO(csv_text))
        for row in reader:
            try:
                items.append({
                    'name': row.get('name', row.get('名称', '')),
                    'lat1': float(row.get('lat1', row.get('纬度1', 0))),
                    'lon1': float(row.get('lon1', row.get('经度1', 0))),
                    'alt1': float(row.get('alt1', row.get('海拔1', 0))),
                    'lat2': float(row.get('lat2', row.get('纬度2', 0))),
                    'lon2': float(row.get('lon2', row.get('经度2', 0))),
                    'alt2': float(row.get('alt2', row.get('海拔2', 0))),
                    'voltage_level': row.get('voltage_level', row.get('电压等级', '')),
                })
            except (ValueError, KeyError):
                continue

    if not items:
        return jsonify({'error': '没有有效的导入数据'}), 400

    from core.powerline import PowerLineSegment
    count = 0
    for item in items:
        if not item.get('name'):
            continue
        seg = PowerLineSegment(
            name=item['name'],
            lat1=_safe_float(item.get('lat1')), lon1=_safe_float(item.get('lon1')),
            alt1=_safe_float(item.get('alt1')),
            lat2=_safe_float(item.get('lat2')), lon2=_safe_float(item.get('lon2')),
            alt2=_safe_float(item.get('alt2')),
            line_id=len(controller.pl_manager.lines) + 1,
            voltage_level=item.get('voltage_level', ''),
        )
        controller.pl_manager.lines.append(seg)
        count += 1

    controller.db.load_power_lines([{
        'name': l.name, 'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
        'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2, 'id': l.line_id,
        'voltage_level': getattr(l, 'voltage_level', ''),
    } for l in controller.pl_manager.lines])
    controller._save_power_lines_to_yaml()
    return jsonify({'status': 'ok', 'imported': count})


@app.route('/api/audit')
@require_web_auth
def api_audit_logs():
    """操作审计日志"""
    global controller
    if not controller:
        return jsonify([])
    limit = min(int(request.args.get('limit', 100)), 500)
    rows = controller.db.get_audit_logs(limit=limit)
    return jsonify([{
        'id': r['id'],
        'timestamp': r['timestamp'][:19] if r.get('timestamp') else '',
        'operation': r.get('operation', ''),
        'table_name': r.get('table_name', ''),
        'record_id': r.get('record_id'),
        'operator': r.get('operator', 'system'),
        'details': r.get('details', ''),
    } for r in rows])


@app.route('/api/settings', methods=['GET', 'PUT'])
@require_web_auth
def api_settings():
    global controller
    _settings_file = PROJECT_ROOT / 'config' / 'settings.yaml'

    def _read_settings():
        import yaml
        try:
            if _settings_file.exists():
                with open(str(_settings_file), 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f) or {}
        except Exception:
            pass
        return {}

    def _write_settings(data):
        import yaml
        with open(str(_settings_file), 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)

    if request.method == 'GET':
        # 返回当前设置，首次从 config.yaml 读取默认值
        s = _read_settings()
        if not s:
            cfg = controller._config if controller else {}
            s = {
                'thresholds': cfg.get('thresholds', {'warning': 200, 'severe': 100, 'critical': 50}),
                'anti_flapping': cfg.get('anti_flapping', {'enabled': False, 'debounce_in': 3, 'debounce_out': 10}),
                'sms': {'enabled': cfg.get('sms', {}).get('enabled', cfg.get('backhaul', {}).get('sms', {}).get('enabled', False)),
                        'alert_phones': cfg.get('sms', {}).get('alert_phones', cfg.get('backhaul', {}).get('sms', {}).get('alert_phones', []))},
                'pilot_notify': {'enabled': cfg.get('pilot_notify', {}).get('enabled', False)},
                'raw_archive': cfg.get('raw_archive', {'enabled': True, 'retention_days': 30}),
            }
        return jsonify(s)

    # PUT — 管理员才能修改
    if session['user'].get('role') != 'admin':
        return jsonify({'error': '需要管理员权限'}), 403

    data = request.json or {}
    _write_settings(data)

    # 实时应用阈值变更
    if controller and 'thresholds' in data:
        try:
            controller.thresholds.update(data['thresholds'])
        except Exception:
            pass

    return jsonify({'status': 'ok'})


@app.route('/api/trajectories')
@require_web_auth
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
@require_web_auth
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
@require_web_auth
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


@app.route('/api/stats/dashboard')
@require_web_auth
def api_stats_dashboard():
    """聚合统计: 24h 告警趋势 + 机型分布 + 站点信息"""
    global controller
    if not controller:
        return jsonify({})

    try:
        from core.parser.types import UA_TYPE_NAMES, lookup_model_by_sn
        from collections import Counter

        hourly = controller.db.get_hourly_alert_counts(24)
        ua_stats = controller.db.get_ua_type_stats()
        # 产品型号分布 — 优先 lookup_model_by_sn，无匹配 fallback 到 UA_TYPE_NAMES
        model_counts = Counter()
        for d in controller.db.get_active_drones():
            model = lookup_model_by_sn(d['id']) or UA_TYPE_NAMES.get(d.get('ua_type', 0), '未知')
            model_counts[model] += 1
        model_dist = [{'name': k, 'count': v} for k, v in model_counts.most_common()]

        bh = controller.backhaul
        pos_lat, pos_lon, pos_alt = 0.0, 0.0, 0.0
        try:
            if bh:
                pos_lat, pos_lon, pos_alt = bh._get_device_position()
        except Exception:
            pass

        station = {
            'device_name': controller._config.get('backhaul', {}).get('device_name', 'NW-F1'),
            'device_location': controller._config.get('backhaul', {}).get('device_location', ''),
            'position': {'lat': pos_lat, 'lon': pos_lon, 'alt': pos_alt},
            'active_channel': str(bh.active_channel) if bh else 'none',
            'primary_online': bool(bh.primary_online) if bh else False,
            'beidou_online': bool(bh.beidou_online) if bh else False,
            'beidou_signal': 0,
            'queue_size': int(bh.queue_size) if bh else 0,
            'http_sent': int(bh.stats.get('http_sent', 0)) if bh else 0,
            'beidou_sent': int(bh.stats.get('beidou_sent', 0)) if bh else 0,
            'last_send': str(bh.stats.get('last_send_time', '--')) if bh else '--',
            'pl_count': len(controller.pl_manager.lines),
            'drone_count': len(controller.db.get_active_drones()),
        }
    except Exception as e:
        logger.warning("stats/dashboard 查询失败: %s", e)
        return jsonify({'error': str(e)}), 500

    return jsonify({
        'hourly_alerts': hourly,
        'model_dist': model_dist,
        'station': station,
        'stations': controller.stations if controller else [],
    })


@app.route('/api/archive/<drone_id>')
@require_web_auth
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
@require_web_auth
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

        # 使用共享工厂初始化所有核心组件
        from core.bootstrap import bootstrap_core
        config_path = str(PROJECT_ROOT / 'config' / 'config.yaml')
        core = bootstrap_core(config_path=config_path)
        self._config = core['config']
        self._pl_file = core['pl_file']
        self._stations_file = PROJECT_ROOT / 'config' / 'stations.yaml'
        self.stations = self._load_stations()
        self.db = core['db']
        self.pl_manager = core['pl_manager']
        self.alert_system = core['alert_system']
        self.trajectory_recorder = core['trajectory_recorder']
        self.raw_archive = core['raw_archive']
        self.pilot_notifier = core['pilot_notifier']
        self.pipeline = core['pipeline']
        self.backhaul = core['backhaul']
        self.thresholds = core['thresholds']

        self.backhaul.start()
        self._stale_cleanup_event = threading.Event()
        self._stale_thread = None

    def _load_stations(self):
        """从 YAML 文件加载监控站点列表"""
        import yaml
        try:
            if self._stations_file.exists():
                with open(str(self._stations_file), 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f) or {}
                    return data.get('stations', [])
        except Exception:
            pass
        return []

    def _save_stations_to_yaml(self):
        """将当前站点数据写回 YAML 文件"""
        import yaml
        data = {"stations": self.stations}
        with open(str(self._stations_file), 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)

    def _save_power_lines_to_yaml(self):
        """将当前电力线数据写回 YAML 文件"""
        import yaml
        pl_data = {
            "power_lines": [
                {
                    "name": l.name,
                    "lat1": l.lat1, "lon1": l.lon1, "alt1": l.alt1,
                    "lat2": l.lat2, "lon2": l.lon2, "alt2": l.alt2,
                    "voltage_level": getattr(l, 'voltage_level', ''),
                }
                for l in self.pl_manager.lines
            ]
        }
        with open(str(self._pl_file), 'w', encoding='utf-8') as f:
            yaml.dump(pl_data, f, allow_unicode=True, default_flow_style=False)

    def _stale_drone_cleanup_loop(self):
        """后台线程：定期将超时无人机标记为 gone"""
        stale_timeout = self._config.get('stale_timeout', 120)
        while not self._stale_cleanup_event.is_set():
            self._stale_cleanup_event.wait(timeout=30)
            if self._stale_cleanup_event.is_set():
                break
            try:
                count = self.db.mark_stale_drones_as_gone(stale_timeout)
                if count > 0:
                    self._log(f"清理 {count} 个过期无人机", "info")
            except Exception:
                pass

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
        self._stale_cleanup_event.clear()
        self._stale_thread = threading.Thread(
            target=self._stale_drone_cleanup_loop, daemon=True
        )
        self._stale_thread.start()

    def stop(self):
        self.running = False
        receiver = self._receiver
        self._receiver = None
        loop = self._loop
        if loop and receiver:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    receiver.stop(), loop
                )
                future.result(timeout=5)
            except Exception:
                pass
        if loop and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        self._loop = None
        self._stale_cleanup_event.set()
        if self._stale_thread and self._stale_thread.is_alive():
            self._stale_thread.join(timeout=5)
        self._stale_thread = None

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

        def safe_callback(parsed):
            if not self.running or not self._receiver:
                return
            try:
                self._on_rid(parsed)
            except RuntimeError:
                pass  # event loop closed during shutdown

        if self.mode == 'simulated':
            from receiver.simulated import create_simulated_receiver
            self._receiver = create_simulated_receiver(
                callback=safe_callback,
                pl_manager=self.pl_manager,
                drone_count=6,
                update_interval=1.0,
            )
        elif self.mode == 'wifi':
            from receiver.wifi import create_wifi_receiver
            self._receiver = create_wifi_receiver(
                callback=safe_callback,
                interface=self._wifi_interface,
            )
        else:
            self._receiver = BLE_RIDReceiver(
                callback=safe_callback,
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
