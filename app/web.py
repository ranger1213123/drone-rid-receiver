"""
Flask Web GUI - 无人机 RID 接收与电力线防碰撞监控系统

纯 Python 依赖 (flask), 不依赖 tkinter。
浏览器访问 http://localhost:5000 即可使用。
MQTT 启用时通过 WebSocket (Socket.IO) 实时推送无人机位置, 替代 AJAX 轮询。
"""

import json
import os
import secrets
import sys
import threading
import time
import asyncio
from pathlib import Path
from datetime import datetime

from functools import wraps
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from flask_socketio import SocketIO
from werkzeug.security import generate_password_hash, check_password_hash

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from logging_config import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = SCRIPT_DIR.parent

from core.parser.types import UA_TYPE_NAMES, lookup_model_by_sn

app = Flask(__name__, template_folder=str(PROJECT_ROOT / 'templates'))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# 注册服务器蓝图 — 统一 web.py(边缘/模拟) 和 server.py(云端) 的路由
try:
    from app.server.api_web import bp as api_web_bp
    from app.server.api_status import bp as status_bp
    from app.server.api_auth import bp as auth_bp
    from app.server.api_report import bp as report_bp
    from app.server.api_heartbeat import bp as heartbeat_bp
    from app.server.dashboard import bp as dashboard_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(report_bp)
    app.register_blueprint(heartbeat_bp)
    app.register_blueprint(status_bp)
    app.register_blueprint(api_web_bp)
    app.register_blueprint(dashboard_bp)
    # Session secret key
    app.secret_key = os.environ.get("WEB_SECRET_KEY", secrets.token_hex(32))
except ImportError:
    pass  # server blueprints not available, use standalone mode


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

# Session 密钥: 优先从配置文件读取, 无配置时生成并持久化到文件避免重启登出
_secret_file = PROJECT_ROOT / 'data' / '.session_secret'
try:
    if _secret_file.exists():
        app.secret_key = _secret_file.read_text().strip()
    elif _web_auth_cfg.get('secret_key'):
        app.secret_key = _web_auth_cfg['secret_key']
    else:
        app.secret_key = os.urandom(24).hex()
        _secret_file.parent.mkdir(parents=True, exist_ok=True)
        _secret_file.write_text(app.secret_key)
except Exception:
    app.secret_key = os.urandom(24).hex()

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

def _migrate_passwords(users):
    """将明文密码自动迁移为 werkzeug 哈希值 (原地修改并保存)"""
    changed = False
    for u in users:
        pw = u.get('password', '')
        if pw and not pw.startswith('scrypt:'):
            u['password'] = generate_password_hash(pw)
            changed = True
    if changed:
        _save_web_users(users)
        logger.info("已迁移 %d 个用户密码为哈希存储",
                    sum(1 for u in users if u['password'].startswith('scrypt:')))

WEB_USERS = _load_web_users()
_migrate_passwords(WEB_USERS)

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
live_feed = None  # MQTT→WebSocket 实时推送桥接

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ''
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        # 1. 先查 YAML 用户 (本地开发)
        for u in WEB_USERS:
            stored_pw = u.get('password', '')
            ok = False
            if stored_pw.startswith('scrypt:'):
                ok = check_password_hash(stored_pw, password)
            else:
                ok = (stored_pw == password)
            if u['username'] == username and ok:
                session['user'] = {
                    'username': username,
                    'role': u.get('role', 'user'),
                    'station': u.get('station', ''),
                    'tenant_id': u.get('tenant_id'),
                    'scope': u.get('scope', 'station'),
                    'assigned_station': u.get('assigned_station', ''),
                }
                return redirect(url_for('index'))
        # 2. 回退到 ORM 用户 (云服务器)
        try:
            from app.server.models import verify_web_user
            user = verify_web_user(username, password)
            if user:
                # 同步回 YAML 以便下次登录
                global WEB_USERS
                WEB_USERS.append({
                    'username': user['username'],
                    'password': generate_password_hash(password),
                    'role': user.get('role', 'user'),
                    'station': user.get('assigned_station', ''),
                    'tenant_id': user.get('tenant_id'),
                    'scope': user.get('scope', 'station'),
                    'assigned_station': user.get('assigned_station', ''),
                })
                _save_web_users(WEB_USERS)
                session['user'] = {
                    'username': user['username'],
                    'role': user.get('role', 'user'),
                    'station': user.get('assigned_station', ''),
                    'tenant_id': user.get('tenant_id'),
                    'scope': user.get('scope', 'station'),
                    'assigned_station': user.get('assigned_station', ''),
                }
                return redirect(url_for('index'))
        except Exception as e:
            logger.warning("ORM 登录回退失败: %s", e)
        error = '用户名或密码错误'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/register', methods=['GET'])
def register_page():
    return render_template('register.html')


@app.route('/api/register/stations')
def api_register_stations():
    """根据密钥返回该租户的站点列表"""
    key = (request.args.get('key') or '').strip().upper()
    if not key:
        return jsonify([])
    # 标准化密钥格式: 去掉所有非字母数字字符，按4位分组
    clean = key.replace('-', '').upper()
    if len(clean) >= 16:
        key = '-'.join(clean[i:i+4] for i in range(0, 16, 4))
    try:
        from app.server.models import get_tenant_by_key, get_tenant_stations
        tenant = get_tenant_by_key(key)
        if tenant and tenant.is_active:
            stations = get_tenant_stations(tenant.id)
            return jsonify([{'name': s.name, 'location': s.location or ''} for s in stations])
    except Exception as e:
        logger.warning("查询租户站点失败: %s", e)
    return jsonify([])


@app.route('/api/register', methods=['POST'])
def api_register():
    """客户用密钥自助注册用户账号"""
    data = request.json or {}
    license_key = (data.get('license_key') or '').strip().upper()
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '')
    scope = (data.get('scope') or 'station').strip()
    assigned_station = (data.get('station') or '').strip()

    # 1. 输入校验
    if not license_key or not username or not password:
        return jsonify({"error": "请填写所有必填字段"}), 400
    if len(username) < 2 or len(username) > 32:
        return jsonify({"error": "用户名需 2-32 位"}), 400
    if len(password) < 6:
        return jsonify({"error": "密码至少 6 位"}), 400

    # 2. 验密钥
    try:
        from app.server.models import get_tenant_by_key, count_users_in_tenant, get_tenant_stations, get_session
        clean = license_key.replace('-', '').upper()
        if len(clean) >= 16:
            license_key = '-'.join(clean[i:i+4] for i in range(0, 16, 4))
        tenant = get_tenant_by_key(license_key)
        if not tenant or not tenant.is_active:
            return jsonify({"error": "密钥无效或已停用"}), 403

        # 3. 检查用户数上限
        if count_users_in_tenant(tenant.id) >= tenant.max_users:
            return jsonify({"error": f"该密钥最多注册 {tenant.max_users} 人，已满"}), 403

        # 4. 验证站点归属
        if scope == 'station':
            if not assigned_station:
                return jsonify({"error": "请选择所属站点"}), 400
            tenant_stations = get_tenant_stations(tenant.id)
            station_names = [s.name for s in tenant_stations]
            if assigned_station not in station_names:
                return jsonify({"error": "该站点不属于您的客户"}), 403
    except Exception as e:
        logger.error("注册验证失败: %s", e)
        return jsonify({"error": "系统错误，请稍后重试"}), 500

    # 5. 检查用户名是否已存在
    global WEB_USERS
    for u in WEB_USERS:
        if u.get('username') == username:
            return jsonify({"error": "用户名已被占用"}), 409

    # 6. 创建用户 (保存到 YAML)
    new_user = {
        'username': username,
        'password': generate_password_hash(password),
        'role': 'user',
        'station': assigned_station if scope == 'station' else '',
        'tenant_id': tenant.id,
        'scope': scope,
        'assigned_station': assigned_station,
    }
    WEB_USERS.append(new_user)
    _save_web_users(WEB_USERS)

    # 7. 同时创建 ORM 用户 (供 server.py 认证使用)
    try:
        from app.server.models import upsert_web_user
        upsert_web_user(
            username=username,
            password=password,
            role='user',
            tenant_id=tenant.id,
            scope=scope,
            assigned_station=assigned_station,
        )
    except Exception as e:
        logger.warning("ORM 用户创建失败 (非关键): %s", e)

    logger.info("新用户注册: %s (tenant=%s, scope=%s, station=%s)",
                username, tenant.name, scope, assigned_station)
    return jsonify({"status": "ok", "message": "注册成功"})


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
    global controller, live_feed
    # 优先使用 LiveFeed 内存缓存 (MQTT→WebSocket 模式), 无 MQTT 时回退到 DB
    if live_feed is not None:
        cached_drones = live_feed.get_active_drones()
        if cached_drones:
            drones = cached_drones
            alert_drones = {}
        else:
            drones = controller.db.get_active_drones() if controller else []
            alert_drones = controller.alert_system.drone_level if controller else {}
    else:
        drones = controller.db.get_active_drones() if controller else []
        alert_drones = controller.alert_system.drone_level if controller else {}

    # Add power line names, model names to drones (skip for live_feed cached, already populated)
    device_name = controller._config.get('backhaul', {}).get('device_name', '') if controller else ''
    if controller and (live_feed is None or not live_feed.get_active_drones()):
        line_names = {l.line_id: l.name for l in controller.pl_manager.lines}
        for d in drones:
            d['category_name'] = UA_TYPE_NAMES.get(d.get('ua_type', 0), '未知')
            d['product_model'] = lookup_model_by_sn(d['id']) or ''
            line_id = d.get('nearest_line_id')
            d['line_name'] = line_names.get(line_id, '') if line_id else ''
            d.setdefault('device_name', device_name)  # 确保 DB 回退路径的无人机有所属设备

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
        'ws_enabled': live_feed is not None and live_feed.get_mqtt_connected(),
        'current_user': {
            'username': user.get('username', ''),
            'role': user.get('role', 'user'),
            'station': user.get('station', ''),
        },
        'backhaul': {
            'mqtt_online': bhaul.primary_online if bhaul else False,
            'queue_size': bhaul.queue_size if bhaul else 0,
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
    controller.switch_mode(
        mode,
        wifi_interface=data.get('interface'),
        serial_device=data.get('serial_device') or None,
        serial_baud=data.get('serial_baud'),
        serial_auto=data.get('serial_auto'),
        serial_probe_timeout=data.get('serial_probe_timeout'),
    )
    return jsonify({'status': 'ok', 'mode': mode})

@app.route('/api/stop', methods=['POST'])
@require_web_auth
def api_stop():
    global controller
    if controller:
        controller.stop()
    return jsonify({'status': 'ok'})

@app.route('/api/serial/scan', methods=['GET'])
@require_web_auth
def api_serial_scan():
    """扫描所有可用串口并检测 ESP32 设备"""
    from receiver.serial_scanner import scan_ports, list_serial_ports
    timeout = float(request.args.get('timeout', 2.0))
    try:
        ports = list_serial_ports()
        results = scan_ports(probe_timeout=timeout)
        return jsonify({
            'ports': ports,
            'found': [{'port': r.port, 'baud': r.baud, 'dev_id': r.dev_id}
                      for r in results],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/serial/connect', methods=['POST'])
@require_web_auth
def api_serial_connect():
    """切换到指定串口设备"""
    global controller
    if not controller:
        return jsonify({'error': '系统未启动'}), 503
    data = request.json or {}
    port = (data.get('port') or '').strip() or None
    baud = int(data.get('baud', 115200))
    auto_scan = data.get('auto_scan', False)
    probe_timeout = float(data.get('probe_timeout', 2.0))
    controller.switch_mode('serial',
                           serial_device=port,
                           serial_baud=baud,
                           serial_auto=auto_scan,
                           serial_probe_timeout=probe_timeout)
    return jsonify({'status': 'ok', 'port': port or '(auto)'})

@app.route('/api/powerlines', methods=['GET', 'POST', 'DELETE'])
@require_web_auth
def api_powerlines():
    global controller
    if not controller:
        return jsonify([])
    if request.method == 'GET':
        return jsonify([{
            'id': l.line_id,
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
        # 使用 DB 自增 ID 而非列表索引
        max_id = max((l.line_id for l in controller.pl_manager.lines), default=0)
        line_id = max_id + 1
        seg = PowerLineSegment(
            name=name,
            lat1=_safe_float(data.get('lat1')), lon1=_safe_float(data.get('lon1')),
            alt1=_safe_float(data.get('alt1')),
            lat2=_safe_float(data.get('lat2')), lon2=_safe_float(data.get('lon2')),
            alt2=_safe_float(data.get('alt2')),
            line_id=line_id,
            voltage_level=voltage_level,
        )
        controller.pl_manager.lines.append(seg)
        controller._reload_pl_db()
        return jsonify({'status': 'ok', 'id': line_id})

@app.route('/api/powerlines/<int:line_id>', methods=['DELETE', 'PUT'])
@require_web_auth
def api_modify_powerline(line_id):
    global controller
    if not controller:
        return jsonify({'error': '系统未启动'}), 503

    # 通过 line_id 查找而非列表索引, 防止并发编辑删错
    target_idx = None
    for i, l in enumerate(controller.pl_manager.lines):
        if l.line_id == line_id:
            target_idx = i
            break
    if target_idx is None:
        return jsonify({'error': '电力线不存在'}), 404

    if request.method == 'DELETE':
        del controller.pl_manager.lines[target_idx]
        controller._reload_pl_db()
        return jsonify({'status': 'ok'})

    if request.method == 'PUT':
        data = request.json or {}
        line = controller.pl_manager.lines[target_idx]
        if 'name' in data:
            line.name = data['name'].strip()
        if 'voltage_level' in data:
            line.voltage_level = data['voltage_level'].strip()
        for attr in ['alt1', 'alt2', 'lat1', 'lon1', 'lat2', 'lon2']:
            if attr in data and data[attr] is not None:
                setattr(line, attr, _safe_float(data[attr]))
        controller._reload_pl_db()
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
            'username': username, 'password': generate_password_hash(password),
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
    max_id = max((l.line_id for l in controller.pl_manager.lines), default=0)
    count = 0
    for item in items:
        if not item.get('name'):
            continue
        max_id += 1
        seg = PowerLineSegment(
            name=item['name'],
            lat1=_safe_float(item.get('lat1')), lon1=_safe_float(item.get('lon1')),
            alt1=_safe_float(item.get('alt1')),
            lat2=_safe_float(item.get('lat2')), lon2=_safe_float(item.get('lon2')),
            alt2=_safe_float(item.get('alt2')),
            line_id=max_id,
            voltage_level=item.get('voltage_level', ''),
        )
        controller.pl_manager.lines.append(seg)
        count += 1

    controller._reload_pl_db()
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
        return jsonify({'mqtt_online': False})
    bh = controller.backhaul
    return jsonify({
        'mqtt_online': bh.primary_online,
        'queue_size': bh.queue_size,
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
        cfg_pos = controller._config.get('position', {})
        pos_lat = float(cfg_pos.get('manual_lat', 0) or 0)
        pos_lon = float(cfg_pos.get('manual_lon', 0) or 0)
        pos_alt = float(cfg_pos.get('manual_alt', 0) or 0)

        station = {
            'device_name': controller._config.get('backhaul', {}).get('device_name', 'NW-F1'),
            'device_location': controller._config.get('backhaul', {}).get('device_location', ''),
            'position': {'lat': pos_lat, 'lon': pos_lon, 'alt': pos_alt},
            'mqtt_online': bool(bh.primary_online) if bh else False,
            'queue_size': int(bh.queue_size) if bh else 0,
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


@app.route('/api/airspace')
def api_airspace():
    """返回空域区域 (禁飞区/管制空域) 用于地图渲染"""
    global controller
    if not controller or not controller.airspace_manager:
        return jsonify([])
    try:
        from core.airspace import check_airspace_violation
        zones = controller.airspace_manager.fetch()
        return jsonify([{
            'zone_id': z.zone_id,
            'name': z.name,
            'zone_type': z.zone_type,
            'vertices': z.vertices,
            'altitude_floor': z.altitude_floor,
            'altitude_ceiling': z.altitude_ceiling,
            'source': z.source,
        } for z in zones])
    except Exception as e:
        logger.warning("获取空域数据失败: %s", e)
        return jsonify([])


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
        self._lock = threading.Lock()

        # 使用共享工厂初始化所有核心组件
        from core.bootstrap import bootstrap_core
        config_path = str(PROJECT_ROOT / 'config' / 'config.yaml')
        core = bootstrap_core(config_path=config_path)
        self._config = core['config']

        # 串口配置从 config.yaml 加载
        from receiver.serial import get_serial_config
        scfg = get_serial_config(self._config)
        self._serial_device = scfg["device"]
        self._serial_baud = scfg["baud"]
        self._serial_auto_scan = scfg["auto_scan"]
        self._serial_probe_timeout = scfg["probe_timeout"]
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
        self.airspace_manager = core.get('airspace_manager')
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

    def _reload_pl_db(self):
        """将内存中的电力线列表重新加载到 SQLite (用于 CRUD 后的持久化)"""
        self.db.load_power_lines([{
            'name': l.name, 'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
            'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2, 'id': l.line_id,
            'voltage_level': getattr(l, 'voltage_level', ''),
        } for l in self.pl_manager.lines])
        self._save_power_lines_to_yaml()

    def _stale_drone_cleanup_loop(self):
        """后台线程：定期标记过期无人机 + 清理过期数据 + 清理告警内存"""
        stale_timeout = self._config.get('stale_timeout', 120)
        iteration = 0
        while not self._stale_cleanup_event.is_set():
            self._stale_cleanup_event.wait(timeout=30)
            if self._stale_cleanup_event.is_set():
                break
            iteration += 1
            try:
                count = self.db.mark_stale_drones_as_gone(stale_timeout)
                if count > 0:
                    self._log(f"清理 {count} 个过期无人机", "info")
                # 每隔 1 小时 (120 * 30s) 清理过期数据
                if iteration % 120 == 0:
                    self.db.cleanup_stale_data()
                # 清理告警系统内存中的过期条目
                active_ids = {d['id'] for d in self.db.get_active_drones()}
                self.alert_system.cleanup_stale(active_ids)
            except Exception:
                pass

    def switch_mode(self, mode, wifi_interface=None,
                    serial_device=None, serial_baud=None,
                    serial_auto=None, serial_probe_timeout=None):
        """切换接收模式 (不重建 DB, 线程安全)"""
        with self._lock:
            self.stop()
            self.mode = mode
            self._wifi_interface = wifi_interface
            if serial_device is not None:
                self._serial_device = serial_device
            if serial_baud is not None:
                self._serial_baud = serial_baud
            if serial_auto is not None:
                self._serial_auto_scan = serial_auto
            if serial_probe_timeout is not None:
                self._serial_probe_timeout = serial_probe_timeout
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
        elif self.mode == 'serial':
            from receiver.serial import create_serial_receiver
            self._receiver = create_serial_receiver(
                callback=safe_callback,
                device=self._serial_device,
                baud=self._serial_baud,
                auto_scan=self._serial_auto_scan,
                scan_timeout=self._serial_probe_timeout,
            )
        else:
            self._receiver = BLE_RIDReceiver(
                callback=safe_callback,
                scan_duration=self._config.get('ble', {}).get('scan_duration', 5.0),
            )

        async def runner():
            mode_names = {'ble': 'BLE 蓝牙', 'wifi': 'WiFi', 'serial': '串口'}
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

    global controller, live_feed
    controller = WebController()
    controller.switch_mode('simulated')

    # 初始化 MQTT→WebSocket 实时推送 (可选, 未配置 MQTT 时退化为轮询)
    mqtt_cfg = controller._config.get("mqtt", {})
    if mqtt_cfg.get("enabled", False):
        from core.live_feed import LiveFeed
        live_feed = LiveFeed(mqtt_cfg, socketio)
        live_feed.start()
        logger.info("LiveFeed MQTT→WebSocket 已启用")
    else:
        logger.info("MQTT 未启用, 使用传统 AJAX 轮询模式")

    logger.info("Drone RID Receiver Web GUI 启动")
    logger.info("浏览器打开: http://%s:%s", args.host, args.port)
    logger.info("按 Ctrl+C 停止")

    try:
        socketio.run(app, host=args.host, port=args.port, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        pass
    finally:
        if live_feed:
            live_feed.stop()
        if controller:
            controller.shutdown()


if __name__ == '__main__':
    main()
