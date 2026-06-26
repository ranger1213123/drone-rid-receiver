"""
Web GUI REST API Blueprint — 云服务器模式
Session-based 鉴权 (admin/operator)，数据库持久化
"""
import csv
import io
import os
import secrets
import time
import threading
from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify, session, g

from .models import (
    get_devices, get_all_drones,
    get_recent_alerts, acknowledge_alert, get_hourly_alert_counts,
    get_power_lines, upsert_power_line, delete_power_line,
    get_web_users, verify_web_user, upsert_web_user, delete_web_user, count_admin_users,
    get_stations, upsert_station, delete_station,
    get_settings, get_setting, set_setting,
    add_audit_log, get_audit_logs,
    get_device_secrets, upsert_device_secret, delete_device_secret,
    get_personnel_by_station, get_all_personnel, upsert_personnel, delete_personnel,
    create_tenant, get_tenants, get_tenant_by_key, update_tenant, delete_tenant,
    count_users_in_tenant, get_tenant_stations, get_user_stations,
    estimate_tower_height, get_drone_model_distribution,
    get_whitelist, add_to_whitelist, remove_from_whitelist,
    compute_azimuth_distance,
)
from .auth import require_auth
from logging_config import get_logger
from werkzeug.security import generate_password_hash, check_password_hash

bp = Blueprint("api_web", __name__)
logger = get_logger(__name__)


def _safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ── Geocode utilities ──

from .geocode import get_geocoder


def _auto_geocode_station(station_name: str, lat: float, lon: float):
    """Reverse geocode via offline shapely+GeoJSON and write province/city/county."""
    if not lat or not lon:
        return
    geocoder = get_geocoder()
    if not geocoder.available:
        return
    try:
        parsed = geocoder.reverse(lat, lon)
    except Exception:
        return
    if not parsed:
        return
    prov = (parsed.get("province") or "").strip()
    city = (parsed.get("city") or "").strip()
    county = (parsed.get("county") or "").strip()
    if not prov and not city and not county:
        return

    from .models import upsert_station
    upsert_station(
        name=station_name,
        province=prov,
        city=city,
        county=county,
    )


def _compute_conductor_alt(alt_input, tower_h, voltage_level):
    """计算导线海拔: 有塔高时 alt_input 为地面高程; 无塔高时 alt_input 即导线海拔 (兼容旧数据)"""
    alt = _safe_float(alt_input)
    if tower_h is not None:
        return alt + _safe_float(tower_h)
    return alt


def _resolve_tower_height(tower_h, voltage_level):
    """解析塔高: 明确值优先, 否则按电压估算"""
    if tower_h is not None:
        return _safe_float(tower_h)
    if voltage_level:
        return estimate_tower_height(voltage_level)
    return None


def _valid_phone(phone: str) -> bool:
    """中国大陆手机号校验: 1 开头 11 位数字"""
    import re
    return bool(re.match(r'^1\d{10}$', phone))


def _enrich_drones_with_station(drones: list) -> list:
    """为每个无人机附加方位角和距离 (从关联站点计算)"""
    stations = get_stations()
    dev_to_station = {}
    for s in stations:
        dn = s.get("device_name")
        if dn and s.get("lat") and s.get("lon"):
            dev_to_station[dn] = s
    for d in drones:
        st = dev_to_station.get(d.get("device_name") or "")
        if st and d.get("last_lat") and d.get("last_lon"):
            bearing, dist = compute_azimuth_distance(
                st["lat"], st["lon"], d["last_lat"], d["last_lon"]
            )
            d["bearing"] = bearing
            d["station_distance"] = dist
        else:
            d["bearing"] = None
            d["station_distance"] = None
    return drones


# ── Rate Limiting ──

_rate_limit_store: dict = {}  # key → [(timestamp, ...)]
_rate_limit_lock = threading.Lock()

def _rate_limit(key: str, max_requests: int, window_sec: int) -> bool:
    """简单滑动窗口限流，返回 True 表示允许"""
    now = time.time()
    with _rate_limit_lock:
        timestamps = [t for t in _rate_limit_store.get(key, []) if now - t < window_sec]
        if len(timestamps) >= max_requests:
            _rate_limit_store[key] = timestamps
            return False
        timestamps.append(now)
        _rate_limit_store[key] = timestamps
        return True


# ── CSRF Token ──

def _get_csrf_token():
    """生成或获取 session CSRF token"""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]


def _check_csrf():
    """验证 CSRF token。GET/HEAD/OPTIONS 跳过"""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    token = request.headers.get("X-CSRF-Token") or (request.json or {}).get("_csrf_token", "")
    expected = session.get("_csrf_token", "")
    if not token or not expected:
        return False
    return secrets.compare_digest(token, expected)


# ── Auth decorators ──

def require_web_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "未登录"}), 401
        if not _check_csrf():
            return jsonify({"error": "CSRF 验证失败"}), 403
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "未登录"}), 401
        if session["user"].get("role") != "admin":
            return jsonify({"error": "需要管理员权限"}), 403
        return f(*args, **kwargs)
    return decorated


def _user_scope():
    """返回 (tenant_id, permitted_stations: list | None)
    admin → (None, None) 无限制
    tenant_admin → (tenant_id, [...]) 租户全部站点
    user → (tenant_id, [assigned_station]) 单个站点
    """
    u = session.get("user", {})
    if u.get("role") == "admin":
        return None, None
    stations = get_user_stations(u.get("username", ""))
    return u.get("tenant_id"), stations


def require_tenant_admin(f):
    """租户管理员或全局管理员"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "未登录"}), 401
        u = session["user"]
        if u.get("role") == "admin":
            return f(*args, **kwargs)
        if u.get("role") != "tenant_admin":
            return jsonify({"error": "权限不足"}), 403
        if not u.get("tenant_id"):
            return jsonify({"error": "未关联租户"}), 403
        kwargs["_tenant_id"] = u["tenant_id"]
        return f(*args, **kwargs)
    return decorated


def _get_permitted_device_set():
    """返回当前用户有权访问的 device_name 集合。admin 返回 None (全部)"""
    tenant_id, permitted = _user_scope()
    if permitted is None:
        return None
    all_stations = get_stations()
    return {s["device_name"] for s in all_stations if s["name"] in permitted and s.get("device_name")}


def _check_device_permission(device_name):
    """验证当前用户是否有权访问指定设备。返回 (ok, response, status)"""
    if not device_name:
        return True, None, None
    permitted = _get_permitted_device_set()
    if permitted is not None and device_name not in permitted:
        return False, jsonify({"error": "设备不属于您的租户"}), 403
    return True, None, None


# ── Power Lines ──

@bp.route("/api/powerlines", methods=["GET", "POST"])
@require_web_auth
def api_powerlines():
    if request.method == "GET":
        # 可选 ?device_name=X 过滤
        dev = request.args.get("device_name", "").strip() or None
        lines = get_power_lines(device_name=dev)
        # 租户过滤: 只显示有权限站点下的电力线
        tenant_id, permitted = _user_scope()
        if permitted is not None:
            permitted_devices = set()
            all_stations = get_stations()
            for s in all_stations:
                if s["name"] in permitted and s.get("device_name"):
                    permitted_devices.add(s["device_name"])
            lines = [l for l in lines if (l.get("device_name") or "") in permitted_devices or l.get("device_name") is None]
        return jsonify(lines)

    # POST — 新增
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "电力线名称不能为空"}), 400
    lat1 = _safe_float(data.get("lat1"))
    lon1 = _safe_float(data.get("lon1"))
    lat2 = _safe_float(data.get("lat2"))
    lon2 = _safe_float(data.get("lon2"))
    if lat1 == 0 and lon1 == 0 and lat2 == 0 and lon2 == 0:
        return jsonify({"error": "请填写有效的经纬度坐标"}), 400
    # tenant 隔离: 验证 device_name 归属
    dev = (data.get("device_name") or "").strip() or None
    ok, err_resp, err_status = _check_device_permission(dev)
    if not ok:
        return err_resp, err_status

    voltage = (data.get("voltage_level") or "").strip()
    th1 = _resolve_tower_height(data.get("tower_height1"), voltage)
    th2 = _resolve_tower_height(data.get("tower_height2"), voltage)
    alt1 = _compute_conductor_alt(data.get("alt1"), data.get("tower_height1"), voltage)
    alt2 = _compute_conductor_alt(data.get("alt2"), data.get("tower_height2"), voltage)

    pl_id = upsert_power_line({
        "name": name,
        "lat1": lat1, "lon1": lon1, "alt1": alt1,
        "lat2": lat2, "lon2": lon2, "alt2": alt2,
        "tower_height1": th1, "tower_height2": th2,
        "voltage_level": voltage,
        "device_name": dev,
    })
    add_audit_log(session["user"]["username"], "INSERT", "power_lines", pl_id,
                  f"新增电力线: {name}")
    return jsonify({"status": "ok", "id": pl_id})


@bp.route("/api/powerlines/<int:pl_id>", methods=["PUT", "DELETE"])
@require_web_auth
def api_modify_powerline(pl_id):
    pl = get_power_lines()
    target = next((l for l in pl if l["id"] == pl_id), None)
    if not target:
        if request.method == "DELETE":
            return jsonify({"error": "电力线不存在"}), 404
        # PUT — allow upsert; tenant check below uses empty dict
        data = request.json or {}
        dev = (data.get("device_name") or "").strip() or None
        ok, err_resp, err_status = _check_device_permission(dev)
        if not ok:
            return err_resp, err_status
        target = {}  # placeholder for upsert path

    # tenant 隔离: 验证已有电力线归属
    if target and target.get("id") is not None:
        ok, err_resp, err_status = _check_device_permission(target.get("device_name") or "")
        if not ok:
            return err_resp, err_status

    if request.method == "DELETE":
        ok = delete_power_line(pl_id)
        if ok:
            add_audit_log(session["user"]["username"], "DELETE", "power_lines", pl_id,
                          f"删除电力线: {target['name'] if target else pl_id}")
            return jsonify({"status": "ok"})
        return jsonify({"error": "电力线不存在"}), 404

    # PUT — 编辑 (只更新前端实际传了的字段，避免坐标被覆盖为0)
    data = request.json or {}
    # 如果要修改 device_name，验证新 target
    new_dev = (data.get("device_name") or "").strip() or None
    if new_dev != (target.get("device_name") or ""):
        ok, err_resp, err_status = _check_device_permission(new_dev)
        if not ok:
            return err_resp, err_status
    upsert_data = {"id": pl_id}
    _num_fields = ("lat1", "lon1", "alt1", "lat2", "lon2", "alt2")
    for f in ("name", "voltage_level", "device_name"):
        if f in data:
            upsert_data[f] = (data[f] or "").strip() if data[f] else ""
    for f in _num_fields:
        if f in data:
            upsert_data[f] = _safe_float(data[f])
    # 塔高: 如果传了 tower_height, alt 视为地面高程需要重新计算
    for f in ("tower_height1", "tower_height2"):
        if f in data:
            upsert_data[f] = _safe_float(data[f]) if data[f] is not None else None
    if "tower_height1" in data and "alt1" in data:
        upsert_data["alt1"] = _compute_conductor_alt(data["alt1"], data.get("tower_height1"), data.get("voltage_level"))
    if "tower_height2" in data and "alt2" in data:
        upsert_data["alt2"] = _compute_conductor_alt(data["alt2"], data.get("tower_height2"), data.get("voltage_level"))
    if new_dev != (target.get("device_name") or ""):
        upsert_data["device_name"] = new_dev
    upsert_power_line(upsert_data)
    add_audit_log(session["user"]["username"], "UPDATE", "power_lines", pl_id,
                  f"编辑电力线: {data.get('name', pl_id)}")
    return jsonify({"status": "ok"})


@bp.route("/api/powerlines/import", methods=["POST"])
@require_admin
def api_import_powerlines():
    """批量导入电力线 — JSON 数组或 CSV 文本 (仅限 admin)"""
    data = request.json or {}
    items = data.get("items", [])
    csv_text = data.get("csv", "").strip()

    if csv_text and not items:
        reader = csv.DictReader(io.StringIO(csv_text))
        for row in reader:
            try:
                items.append({
                    "name": row.get("name", row.get("名称", "")),
                    "lat1": float(row.get("lat1", row.get("纬度1", 0))),
                    "lon1": float(row.get("lon1", row.get("经度1", 0))),
                    "alt1": float(row.get("alt1", row.get("海拔1", 0))),
                    "lat2": float(row.get("lat2", row.get("纬度2", 0))),
                    "lon2": float(row.get("lon2", row.get("经度2", 0))),
                    "alt2": float(row.get("alt2", row.get("海拔2", 0))),
                    "voltage_level": row.get("voltage_level", row.get("电压等级", "")),
                })
            except (ValueError, KeyError):
                continue

    if not items:
        return jsonify({"error": "没有有效的导入数据"}), 400

    count = 0
    for item in items:
        if not item.get("name"):
            continue
        upsert_power_line(item)
        count += 1

    add_audit_log(session["user"]["username"], "IMPORT", "power_lines", None,
                  f"批量导入 {count} 条电力线")
    return jsonify({"status": "ok", "imported": count})


@bp.route("/api/powerlines/sync")
@require_auth
def api_powerlines_sync():
    """边缘设备轮询电力线配置 (JWT device auth)"""
    device_name = g.device_name
    lines = get_power_lines(device_name=device_name)
    # 简化的版本号: 取最后更新时间
    max_updated = max(
        (l.get("updated_at", "") for l in lines if l.get("updated_at")),
        default=""
    )
    return jsonify({
        "lines": lines,
        "version": max_updated,
        "count": len(lines),
    })


@bp.route("/api/powerlines/push", methods=["POST"])
@require_admin
def api_push_powerlines():
    """管理员推送电力线配置到边缘设备 (通过 MQTT Consumer)"""
    data = request.json or {}
    device_name = (data.get("device_name") or "").strip()

    lines = get_power_lines(device_name=device_name or None)
    max_updated = max(
        (l.get("updated_at", "") for l in lines if l.get("updated_at")),
        default=""
    )
    payload = {"lines": lines, "version": max_updated, "count": len(lines)}

    # 通过 MQTT Consumer 的内部 HTTP 端点发布
    consumer_host = os.environ.get("MQTT_CONSUMER_HOST", "localhost")
    consumer_port = os.environ.get("MQTT_CONSUMER_PORT", "8080")
    try:
        import requests
        topic = f"cmd/{device_name}/config" if device_name else "cmd/broadcast"
        resp = requests.post(
            f"http://{consumer_host}:{consumer_port}/publish",
            json={"topic": topic, "payload": payload, "qos": 1},
            timeout=5,
        )
        if resp.status_code >= 500:
            return jsonify({"error": "MQTT Consumer 不可用"}), 502
    except Exception as e:
        return jsonify({"error": f"MQTT Consumer 连接失败: {e}"}), 502

    add_audit_log(
        session["user"]["username"], "PUSH_POWERLINES",
        "power_lines", None,
        f"MQTT推送电力线 → {device_name or '(全部)'}: {len(lines)}条"
    )
    return jsonify({
        "status": "ok",
        "topic": topic,
        "lines_count": len(lines),
        "version": max_updated,
    })


# ── Stations ──

@bp.route("/api/stations", methods=["GET", "POST", "PUT", "DELETE"])
@require_web_auth
def api_stations():
    if request.method == "GET":
        stations = get_stations()
        tenant_id, permitted = _user_scope()
        if permitted is not None:
            stations = [s for s in stations if s["name"] in permitted]
        return jsonify(stations)

    if request.method == "POST":
        u = session["user"]
        if u.get("role") not in ("admin", "tenant_admin"):
            return jsonify({"error": "需要管理员或租户管理员权限"}), 403
        data = request.json or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "站点名称不能为空"}), 400
        # tenant_admin 自动绑定租户, admin 可选指定
        if u.get("role") == "tenant_admin":
            tid = u.get("tenant_id")
        else:
            tid = data.get("tenant_id")
            if tid is not None:
                tid = int(tid)
        upsert_station(
            name=name,
            location=(data.get("location") or "").strip(),
            province=(data.get("province") or "").strip(),
            city=(data.get("city") or "").strip(),
            county=(data.get("county") or "").strip(),
            lat=_safe_float(data.get("lat")),
            lon=_safe_float(data.get("lon")),
            alt=_safe_float(data.get("alt")),
            device_name=(data.get("device_name") or "").strip() or None,
            tenant_id=tid,
        )
        add_audit_log(u["username"], "INSERT", "stations", None,
                      f"新增站点: {name}")
        return jsonify({"status": "ok"})

    if request.method == "PUT":
        u = session["user"]
        if u.get("role") not in ("admin", "tenant_admin"):
            return jsonify({"error": "需要管理员或租户管理员权限"}), 403
        data = request.json or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "站点名称不能为空"}), 400
        # tenant_admin 只能更新自己租户的站点，且不得转移站点到其他租户
        if u.get("role") == "tenant_admin":
            stations = get_stations()
            target = next((s for s in stations if s["name"] == name), None)
            if not target or target.get("tenant_id") != u.get("tenant_id"):
                return jsonify({"error": "站点不存在或不属于您的租户"}), 403
            new_tenant_id = data.get("tenant_id")
            if new_tenant_id is not None and int(new_tenant_id) != u.get("tenant_id"):
                return jsonify({"error": "不允许将站点转移到其他租户"}), 403
        new_lat = _safe_float(data.get("lat"))
        new_lon = _safe_float(data.get("lon"))
        tenant_id = data.get("tenant_id")
        if u.get("role") == "tenant_admin":
            tenant_id = u["tenant_id"]
        upsert_station(
            name=name,
            location=(data.get("location") or "").strip(),
            province=(data.get("province") or "").strip(),
            city=(data.get("city") or "").strip(),
            county=(data.get("county") or "").strip(),
            lat=new_lat,
            lon=new_lon,
            alt=_safe_float(data.get("alt")),
            device_name=(data.get("device_name") or "").strip() or None,
            tenant_id=tenant_id,
        )
        # Auto-geocode if coordinates changed (only if province is still empty)
        if new_lat and new_lon and not (data.get("province") or "").strip():
            _auto_geocode_station(name, new_lat, new_lon)
        add_audit_log(u["username"], "UPDATE", "stations", None,
                      f"编辑站点: {name}")
        return jsonify({"status": "ok"})

    # DELETE
    u = session["user"]
    if u.get("role") not in ("admin", "tenant_admin"):
        return jsonify({"error": "需要管理员或租户管理员权限"}), 403
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "缺少 name 参数"}), 400
    # tenant_admin 只能删除自己租户的站点
    if u.get("role") == "tenant_admin":
        stations = get_stations()
        target = next((s for s in stations if s["name"] == name), None)
        if not target or target.get("tenant_id") != u.get("tenant_id"):
            return jsonify({"error": "站点不存在或不属于您的租户"}), 403
    ok = delete_station(name)
    if ok:
        add_audit_log(u["username"], "DELETE", "stations", None,
                      f"删除站点: {name}")
    return jsonify({"status": "ok" if ok else "not found"})


# ── Geocode ──

@bp.route("/api/geocode", methods=["POST"])
@require_web_auth
def api_geocode():
    """Reverse geocode lat/lon to province/city/county via offline shapely+GeoJSON."""
    data = request.json or {}
    lat = _safe_float(data.get("lat"))
    lon = _safe_float(data.get("lon"))
    if not lat or not lon:
        return jsonify({"error": "请提供有效的经纬度坐标"}), 400

    geocoder = get_geocoder()
    if not geocoder.available:
        return jsonify({"error": "离线地理编码服务未就绪，请检查数据文件"}), 503

    try:
        parsed = geocoder.reverse(lat, lon)
    except Exception:
        return jsonify({"error": "地理编码查询失败"}), 500

    if not parsed:
        return jsonify({"error": "该坐标无对应地址信息"}), 404

    return jsonify({
        "province": parsed["province"],
        "city": parsed["city"],
        "county": parsed["county"],
    })


# ── Users ──

@bp.route("/api/users", methods=["GET", "POST", "PUT", "DELETE"])
@require_web_auth
def api_users():
    if request.method == "GET":
        users = get_web_users()
        tenant_id, permitted = _user_scope()
        if permitted is not None:
            # tenant_admin 看租户内用户, station_user 只看自己
            if tenant_id:
                users = [u for u in users if u.get("tenant_id") == tenant_id
                         or u.get("role") == "admin"]
            else:
                users = [u for u in users if u["username"] == session["user"]["username"]]
        return jsonify(users)

    u = session["user"]
    if u.get("role") not in ("admin", "tenant_admin"):
        return jsonify({"error": "需要管理员或租户管理员权限"}), 403

    if request.method == "POST":
        data = request.json or {}
        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()
        role = (data.get("role") or "user").strip()
        station = (data.get("station") or "").strip()
        if not username or not password:
            return jsonify({"error": "用户名和密码不能为空"}), 400
        # tenant_admin 只能创建 user, 不能创建 admin
        if u.get("role") == "tenant_admin":
            if role not in ("user", "tenant_admin"):
                return jsonify({"error": "租户管理员只能创建 user 或 tenant_admin 角色"}), 403
        existing = get_web_users()
        if any(u["username"] == username for u in existing):
            return jsonify({"error": "用户名已存在"}), 409
        # tenant_admin 创建的用户自动绑定同一租户
        tid = u.get("tenant_id") if u.get("role") == "tenant_admin" else None
        scope = "tenant" if role == "tenant_admin" else "station"
        upsert_web_user(username, password, role, station,
                        tenant_id=tid, scope=scope,
                        assigned_station=station)
        add_audit_log(u["username"], "INSERT", "web_users", None,
                      f"新增用户: {username}")
        return jsonify({"status": "ok"})

    if request.method == "PUT":
        data = request.json or {}
        username = (data.get("username") or "").strip()
        if not username:
            return jsonify({"error": "缺少 username 参数"}), 400
        existing = get_web_users()
        target = next((u for u in existing if u["username"] == username), None)
        if not target:
            return jsonify({"error": "用户不存在"}), 404
        # tenant_admin 只能编辑自己租户内的非 admin
        if u.get("role") == "tenant_admin":
            if target.get("tenant_id") != u.get("tenant_id"):
                return jsonify({"error": "用户不属于您的租户"}), 403
            if target.get("role") == "admin":
                return jsonify({"error": "不能修改管理员账户"}), 403
        new_role = (data.get("role") or target["role"]).strip()
        new_station = (data.get("station") or target["station"]).strip()
        new_password = (data.get("password") or "").strip()
        new_scope = (data.get("scope") or target.get("scope", "station")).strip()
        upsert_web_user(username,
                        password=new_password or None,
                        role=new_role,
                        station=new_station,
                        scope=new_scope,
                        assigned_station=new_station)
        add_audit_log(u["username"], "UPDATE", "web_users", None,
                      f"编辑用户: {username}")
        return jsonify({"status": "ok"})

    # DELETE
    data = request.json or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"error": "缺少 username 参数"}), 400
    existing = get_web_users()
    admins = [u for u in existing if u.get("role") == "admin"]
    target = next((u for u in existing if u["username"] == username), None)
    if not target:
        return jsonify({"status": "not found"})
    if target.get("role") == "admin" and len(admins) <= 1:
        return jsonify({"error": "不能删除最后一个管理员账户"}), 400
    # tenant_admin 只能删除自己租户下的非 admin
    if u.get("role") == "tenant_admin":
        if target.get("tenant_id") != u.get("tenant_id"):
            return jsonify({"error": "用户不属于您的租户"}), 403
        if target.get("role") == "admin":
            return jsonify({"error": "不能删除管理员账户"}), 403
    ok = delete_web_user(username)
    if ok:
        add_audit_log(u["username"], "DELETE", "web_users", None,
                      f"删除用户: {username}")
    return jsonify({"status": "ok" if ok else "not found"})


# ── Password Management ──

@bp.route("/api/password", methods=["PUT"])
@require_web_auth
def api_change_password():
    """当前用户修改自己的密码"""
    u = session["user"]
    if not _rate_limit(f"chpw:{u['username']}", max_requests=5, window_sec=300):
        return jsonify({"error": "密码修改次数过多，请 5 分钟后再试"}), 429
    data = request.json or {}
    old_pw = data.get("old_password", "")
    new_pw = (data.get("new_password") or "").strip()
    if len(new_pw) < 6:
        return jsonify({"error": "新密码至少 6 位"}), 400
    from .models import get_session, WebUser
    sess = get_session()
    try:
        user = sess.get(WebUser, u["username"])
        if not user:
            return jsonify({"error": "用户不存在"}), 404
        if not check_password_hash(user.password_hash, old_pw):
            return jsonify({"error": "原密码错误"}), 403
        user.password_hash = generate_password_hash(new_pw)
        sess.commit()
        add_audit_log(u["username"], "CHANGE_PASSWORD", "web_users", None, "修改密码")
        return jsonify({"status": "ok", "message": "密码已更新"})
    finally:
        sess.close()


@bp.route("/api/users/<username>/reset-password", methods=["POST"])
@require_web_auth
def api_reset_user_password(username):
    """管理员/租户管理员重置用户密码"""
    u = session["user"]
    role = u.get("role", "")
    if role not in ("admin", "tenant_admin"):
        return jsonify({"error": "需要管理员权限"}), 403
    if not _rate_limit(f"resetpw:{u['username']}", max_requests=5, window_sec=300):
        return jsonify({"error": "重置次数过多，请 5 分钟后再试"}), 429

    data = request.json or {}
    new_pw = (data.get("new_password") or "").strip()
    if len(new_pw) < 6:
        return jsonify({"error": "新密码至少 6 位"}), 400

    from .models import get_session, WebUser
    sess = get_session()
    try:
        target = sess.get(WebUser, username)
        if not target:
            return jsonify({"error": "用户不存在"}), 404
        # tenant_admin 只能重置自己租户下的用户
        if role == "tenant_admin" and target.tenant_id != u.get("tenant_id"):
            return jsonify({"error": "只能重置本租户下的用户"}), 403
        target.password_hash = generate_password_hash(new_pw)
        sess.commit()
        add_audit_log(u["username"], "RESET_PASSWORD", "web_users", None,
                      f"重置用户 {username} 的密码")
        return jsonify({"status": "ok", "message": f"已重置 {username} 的密码"})
    finally:
        sess.close()


# ── Public Password Reset (self-service, no auth) ──

@bp.route("/api/forgot-password", methods=["POST"])
def api_forgot_password():
    """自助重置密码: 通过 license_key 验证身份后重置"""
    if not _rate_limit("forgotpw:global", max_requests=20, window_sec=300):
        return jsonify({"error": "重置请求过于频繁，请 5 分钟后再试"}), 429

    data = request.json or {}
    username = (data.get("username") or "").strip()
    license_key = (data.get("license_key") or "").strip().upper()
    new_password = (data.get("new_password") or "").strip()

    if not username or not license_key:
        return jsonify({"error": "请填写用户名和 License Key"}), 400

    if not _rate_limit(f"forgotpw:user:{username}", max_requests=5, window_sec=900):
        return jsonify({"error": "该用户重置请求过于频繁，请 15 分钟后再试"}), 429
    if len(new_password) < 6:
        return jsonify({"error": "新密码至少 6 位"}), 400

    from .models import get_session, WebUser, Tenant
    sess = get_session()
    try:
        user = sess.get(WebUser, username)
        if not user:
            return jsonify({"error": "用户不存在"}), 404
        if not user.tenant_id:
            return jsonify({"error": "该用户无关联租户，请联系系统管理员"}), 403

        tenant = sess.get(Tenant, user.tenant_id)
        if not tenant or tenant.license_key != license_key:
            return jsonify({"error": "License Key 不匹配"}), 403
        if not tenant.is_active:
            return jsonify({"error": "该租户已被停用"}), 403

        user.password_hash = generate_password_hash(new_password)
        sess.commit()
        add_audit_log(username, "FORGOT_PASSWORD", "web_users", None,
                      f"用户自助重置密码 via license_key")
        return jsonify({"status": "ok", "message": "密码重置成功，请登录"})
    finally:
        sess.close()


# ── Tenant Self-Service ──

@bp.route("/api/tenant/info")
@require_web_auth
def api_tenant_info():
    """返回当前用户所属租户信息 (tenant_admin/user 用)"""
    u = session["user"]
    tid = u.get("tenant_id")
    if not tid:
        return jsonify(None)
    from .models import get_tenant_by_id, count_users_in_tenant, get_tenant_stations
    try:
        tenant = get_tenant_by_id(tid)
    except Exception:
        return jsonify(None)
    if not tenant:
        return jsonify(None)
    stations = get_tenant_stations(tid)
    return jsonify({
        "id": tenant.id,
        "name": tenant.name,
        "license_key": tenant.license_key if u.get("role") == "tenant_admin" else None,
        "max_users": tenant.max_users,
        "current_users": count_users_in_tenant(tid),
        "contact": tenant.contact or "",
        "is_active": tenant.is_active,
        "stations": [{"name": s["name"], "location": s.get("location") or ""} for s in stations],
    })


# ── CSRF Token ──

@bp.route("/api/csrf-token")
def api_csrf_token():
    return jsonify({"token": _get_csrf_token()})


# ── Personnel (站点负责人) ──

@bp.route("/api/personnel", methods=["GET", "POST", "DELETE"])
@require_web_auth
def api_personnel():
    if request.method == "GET":
        station_name = request.args.get("station", "").strip() or None
        tenant_id, permitted = _user_scope()
        if permitted is not None:
            # 用户只能看自己权限内站点的人员
            if station_name and station_name not in permitted:
                return jsonify([])
            personnel = get_all_personnel(station_name=station_name)
            personnel = [p for p in personnel if p.get("station_name") in permitted]
            return jsonify(personnel)
        return jsonify(get_all_personnel(station_name=station_name))

    u = session["user"]

    data = request.json or {}

    if request.method == "POST":
        station_name = (data.get("station_name") or "").strip()
        name = (data.get("name") or "").strip()
        phone = (data.get("phone") or "").strip()
        if not station_name or not phone:
            return jsonify({"error": "站点名称和联系电话不能为空"}), 400
        if not _valid_phone(phone):
            return jsonify({"error": "联系电话格式无效，需为11位手机号"}), 400

        # 权限校验: 只能给自己有权站点添加人员
        if u.get("role") != "admin":
            tenant_id, permitted = _user_scope()
            if permitted is not None and station_name not in permitted:
                return jsonify({"error": "站点不存在或不属于您的权限范围"}), 403

        pid = upsert_personnel(station_name=station_name, name=name, phone=phone)
        add_audit_log(u["username"], "INSERT", "station_personnel", pid,
                      f"新增站点人员: {station_name}/{name}/{phone}")
        return jsonify({"status": "ok", "id": pid})

    # DELETE
    personnel_id = data.get("id")
    if not personnel_id:
        return jsonify({"error": "缺少 id 参数"}), 400

    # 权限校验: 只能删除自己权限范围内站点的人员
    if u.get("role") != "admin":
        all_p = get_all_personnel()
        target_p = next((p for p in all_p if p.get("id") == int(personnel_id)), None)
        if not target_p:
            return jsonify({"error": "人员不存在"}), 404
        tenant_id, permitted = _user_scope()
        if permitted is not None and target_p.get("station_name") not in permitted:
            return jsonify({"error": "人员不属于您的权限范围"}), 403

    ok = delete_personnel(int(personnel_id))
    if ok:
        add_audit_log(u["username"], "DELETE", "station_personnel",
                      personnel_id, f"删除站点人员 id={personnel_id}")
    return jsonify({"status": "ok" if ok else "not found"})


# ── Alerts ──

@bp.route("/api/alerts/history")
@require_web_auth
def api_alerts_history():
    level = request.args.get("level", "").strip() or None
    drone_id = request.args.get("drone_id", "").strip() or None
    since = request.args.get("since", "").strip() or None
    to_date = request.args.get("to", "").strip() or None
    device_name = request.args.get("device_name", "").strip() or None
    ack = request.args.get("acknowledged")
    acknowledged = None
    if ack == "0":
        acknowledged = 0
    elif ack == "1":
        acknowledged = 1

    # 租户/站点过滤
    tenant_id, permitted = _user_scope()
    if permitted is not None:
        all_stations = get_stations()
        permitted_devices = [s["device_name"] for s in all_stations
                            if s["name"] in permitted and s.get("device_name")]
        if device_name:
            if device_name not in permitted_devices:
                return jsonify([])
        else:
            # 用多个 device_name 过滤 — SQLite 不支持 IN 多值高效查询,
            # 直接用全部数据 + Python 过滤 (alert 量小)
            pass

    limit = min(int(request.args.get("limit", 100)), 1000)
    alerts = get_recent_alerts(
        limit=limit, level=level, since=since, to_date=to_date,
        drone_id=drone_id, device_name=device_name, acknowledged=acknowledged,
    )
    # 租户过滤: 只显示有权限站点设备的告警
    if permitted is not None and not device_name:
        alerts = [a for a in alerts if a.get("device_name") in permitted_devices]
    return jsonify(alerts)


@bp.route("/api/alerts/<int:alert_id>/acknowledge", methods=["POST"])
@require_web_auth
def api_acknowledge_alert(alert_id):
    user = session.get("user", {})
    # tenant 隔离: 验证告警属于用户租户
    if user.get("role") != "admin":
        from .models import get_session, Alert
        sess = get_session()
        try:
            alert = sess.get(Alert, alert_id)
            if alert and alert.device_name:
                ok, err_resp, err_status = _check_device_permission(alert.device_name)
                if not ok:
                    return err_resp, err_status
        finally:
            sess.close()
    note = (request.json or {}).get("note", "").strip()
    ok = acknowledge_alert(alert_id, user.get("username", "system"), note)
    if ok:
        add_audit_log(user["username"], "ACKNOWLEDGE", "alerts", alert_id,
                      f"确认告警 #{alert_id}")
    return jsonify({"status": "ok" if ok else "not found"})


@bp.route("/api/alerts/export")
@require_web_auth
def api_alerts_export():
    level = request.args.get("level", "").strip() or None
    since = request.args.get("since", "").strip() or None
    rows = get_recent_alerts(limit=5000, level=level, since=since)
    # tenant 过滤
    permitted = _get_permitted_device_set()
    if permitted is not None:
        rows = [r for r in rows if r.get("device_name") in permitted]
    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(["ID", "时间", "无人机ID", "等级", "距离(m)", "电力线", "消息", "已确认", "确认人", "确认时间"])
    for r in rows:
        w.writerow([
            r["id"], r["timestamp"][:19] if r["timestamp"] else "",
            r["drone_id"], r["level"],
            f"{r['distance']:.1f}" if r.get("distance") else "",
            r["line_name"] or "", r["message"] or "",
            "是" if r.get("acknowledged") else "否",
            r.get("ack_by", ""), r.get("ack_time", "")[:19] if r.get("ack_time") else "",
        ])
    from flask import make_response
    resp = make_response(si.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8-sig"
    resp.headers["Content-Disposition"] = "attachment; filename=alerts_export.csv"
    return resp


@bp.route("/api/drones")
@require_web_auth
def api_drones():
    """GET /api/drones — 分页查询无人机列表
    ?page=1&per_page=50&device_name=X&status=active&q=搜索
    """
    page = max(int(request.args.get("page", 1)), 1)
    per_page = min(int(request.args.get("per_page", 50)), 200)
    device_name = request.args.get("device_name", "").strip() or None
    status = request.args.get("status", "").strip() or None
    q = request.args.get("q", "").strip() or None

    drones = get_all_drones()

    # tenant/site filtering
    permitted = _get_permitted_device_set()
    if permitted is not None:
        drones = [d for d in drones if d.get("device_name") in permitted]

    # filters
    if device_name:
        drones = [d for d in drones if d.get("device_name") == device_name]
    if status:
        drones = [d for d in drones if d.get("status") == status]
    if q:
        ql = q.lower()
        drones = [d for d in drones if ql in (d.get("id") or "").lower()
                  or ql in (d.get("device_name") or "").lower()
                  or ql in (d.get("nearest_line") or "").lower()]

    total = len(drones)
    start = (page - 1) * per_page
    paged = drones[start:start + per_page]
    paged = _enrich_drones_with_station(paged)

    return jsonify({
        "items": paged,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max((total + per_page - 1) // per_page, 1),
    })


@bp.route("/api/drones/export")
@require_web_auth
def api_drones_export():
    drones = get_all_drones()
    # tenant 过滤
    permitted = _get_permitted_device_set()
    if permitted is not None:
        drones = [d for d in drones if d.get("device_name") in permitted]
    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(["无人机ID", "来源设备", "纬度", "经度", "海拔(m)", "速度(m/s)", "航向", "状态", "最近距离(m)", "最近电力线", "最后更新"])
    for d in drones:
        w.writerow([
            d["id"], d["device_name"],
            f"{d['last_lat']:.6f}" if d.get("last_lat") else "",
            f"{d['last_lon']:.6f}" if d.get("last_lon") else "",
            f"{d['last_alt']:.1f}" if d.get("last_alt") else "",
            f"{d.get('last_speed', 0):.1f}",
            f"{d.get('last_heading', 0):.0f}",
            d.get("status", "active"),
            f"{d.get('min_distance', 0):.0f}" if d.get("min_distance") else "-",
            d.get("nearest_line") or "",
            d["last_seen"][:19] if d.get("last_seen") else "",
        ])
    from flask import make_response
    resp = make_response(si.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8-sig"
    resp.headers["Content-Disposition"] = "attachment; filename=drones_export.csv"
    return resp


# ── 注册 & 密钥管理 ──

@bp.route("/api/register", methods=["POST"])
def api_register():
    """客户用密钥自助注册用户"""
    ip = request.remote_addr or "127.0.0.1"
    if not _rate_limit(f"register:{ip}", max_requests=3, window_sec=600):
        return jsonify({"error": "注册尝试次数过多，请 10 分钟后再试"}), 429

    data = request.json or {}
    raw_key = (data.get("license_key") or "").strip().upper()
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    scope = data.get("scope", "station")
    assigned_station = (data.get("station") or "").strip()

    if not raw_key or not username or not password:
        return jsonify({"error": "密钥、用户名和密码不能为空"}), 400
    if len(username) < 2 or len(username) > 32:
        return jsonify({"error": "用户名长度 2-32"}), 400
    if len(password) < 6:
        return jsonify({"error": "密码长度至少 6 位"}), 400
    if scope not in ("tenant", "station"):
        return jsonify({"error": "scope 必须是 tenant 或 station"}), 400

    # 统一转为 XXXX-XXXX-XXXX-XXXX 格式
    clean = raw_key.replace("-", "").replace(" ", "")
    if len(clean) >= 16:
        clean = clean[:16]
    normalized_key = "-".join([clean[i:i+4] for i in range(0, len(clean), 4)])

    tenant = get_tenant_by_key(normalized_key)
    if not tenant or not tenant.is_active:
        return jsonify({"error": "密钥无效或已停用"}), 403

    if count_users_in_tenant(tenant.id) >= tenant.max_users:
        return jsonify({"error": f"该密钥最多注册 {tenant.max_users} 人, 已满"}), 403

    existing = get_web_users()
    if any(u["username"] == username for u in existing):
        return jsonify({"error": "用户名已存在"}), 409

    role = "tenant_admin" if scope == "tenant" else "user"

    if scope == "station":
        if not assigned_station:
            return jsonify({"error": "请选择所属站点"}), 400
        tenant_stations = get_tenant_stations(tenant.id)
        if assigned_station not in [s["name"] for s in tenant_stations]:
            return jsonify({"error": "该站点不属于您的客户"}), 403

    upsert_web_user(username, password, role=role,
                    tenant_id=tenant.id, scope=scope,
                    assigned_station=assigned_station)
    add_audit_log(username, "REGISTER", "web_users", None,
                  f"密钥注册: {username} → tenant={tenant.name} scope={scope}")
    return jsonify({"status": "ok", "message": "注册成功"})


@bp.route("/api/register/stations")
def api_register_stations():
    """公开: 查密钥对应的可用站点 (注册页用) — 兼容有/无横线两种格式"""
    raw = (request.args.get("key") or "").strip().upper()
    if not raw:
        return jsonify([])
    # 统一转为 XXXX-XXXX-XXXX-XXXX 格式后再查询
    clean = raw.replace("-", "").replace(" ", "")
    if len(clean) >= 16:
        clean = clean[:16]
    formatted = "-".join([clean[i:i+4] for i in range(0, len(clean), 4)])
    tenant = get_tenant_by_key(formatted)
    if not tenant or not tenant.is_active:
        return jsonify([])
    return jsonify(get_tenant_stations(tenant.id))


@bp.route("/api/licenses", methods=["GET", "POST", "PUT", "DELETE"])
@require_admin
def api_licenses():
    """管理员管理租户密钥"""
    if request.method == "GET":
        return jsonify(get_tenants())

    if request.method == "POST":
        data = request.json or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "客户名称不能为空"}), 400
        t = create_tenant(
            name=name,
            max_users=int(data.get("max_users", 3)),
            contact=(data.get("contact") or "").strip(),
            created_by=session["user"]["username"],
        )
        add_audit_log(session["user"]["username"], "INSERT", "tenants", t["id"],
                      f"创建租户: {name} 密钥={t['license_key']}")
        return jsonify(t)

    # PUT / DELETE
    data = request.json or {}
    tenant_id = data.get("id")
    if not tenant_id:
        return jsonify({"error": "缺少 id 参数"}), 400

    if request.method == "DELETE":
        ok = delete_tenant(tenant_id)
        if ok:
            add_audit_log(session["user"]["username"], "DELETE", "tenants", tenant_id,
                          f"停用租户 id={tenant_id}")
        return jsonify({"status": "ok" if ok else "not found"})

    # PUT
    ok = update_tenant(tenant_id,
                       name=data.get("name"),
                       max_users=data.get("max_users"),
                       is_active=data.get("is_active"),
                       contact=data.get("contact"))
    if ok:
        add_audit_log(session["user"]["username"], "UPDATE", "tenants", tenant_id,
                      f"更新租户 id={tenant_id}")
    return jsonify({"status": "ok" if ok else "not found"})


# ── Settings ──

@bp.route("/api/settings", methods=["GET", "PUT"])
@require_web_auth
def api_settings():
    if request.method == "GET":
        settings = get_settings()
        # 默认值
        defaults = {
            "threshold_warning": "200",
            "threshold_severe": "100",
            "threshold_critical": "50",
            "anti_flapping_enabled": "false",
            "debounce_in": "3",
            "debounce_out": "10",
            "sms_enabled": "false",
            "sms_alert_phones": "",
            "raw_archive_enabled": "true",
            "raw_archive_retention_days": "30",
        }
        for k, v in defaults.items():
            if k not in settings:
                settings[k] = v
        # 附加空 backhaul 兼容字段
        settings["backhaul"] = {"mode": "cloud"}
        return jsonify(settings)

    # PUT — 管理员
    if session["user"].get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403

    data = request.json or {}
    for key, value in data.items():
        set_setting(key, str(value) if value is not None else "")
    add_audit_log(session["user"]["username"], "UPDATE", "system_settings", None,
                  "更新系统设置")
    return jsonify({"status": "ok"})


# ── Audit Logs ──

@bp.route("/api/audit")
@require_web_auth
def api_audit_logs_endpoint():
    limit = min(int(request.args.get("limit", 100)), 500)
    logs = get_audit_logs(limit=limit)
    return jsonify(logs)


# ── Stats / Dashboard ──

@bp.route("/api/stats/dashboard")
@require_web_auth
def api_stats_dashboard():
    try:
        hourly = get_hourly_alert_counts(24)
        stations = get_stations()
        # 设备统计
        devices = get_devices()
        drones = get_all_drones()

        tenant_id, permitted = _user_scope()
        if permitted is not None:
            stations = [s for s in stations if s["name"] in permitted]
            station_devs = set(s["device_name"] for s in stations if s.get("device_name"))
            devices = [d for d in devices if d["name"] in station_devs]
            drones = [d for d in drones if d["device_name"] in station_devs]

        # 构建站点摘要列表（全国视图展示所有站点）
        station_list = []
        for st in stations:
            dev = next((d for d in devices if d["name"] == st.get("device_name")), None)
            station_list.append({
                "name": st.get("name", ""),
                "device_name": st.get("device_name") or (dev["name"] if dev else "cloud"),
                "device_location": st.get("location") or st.get("name") or "云服务器",
                "location": st.get("location") or st.get("name") or "云服务器",
                "position": {
                    "lat": st.get("lat", 0),
                    "lon": st.get("lon", 0),
                    "alt": st.get("alt", 0),
                },
                "mqtt_online": dev["status"] == "online" if dev else False,
            })

        # 主站点信息（向后兼容，用于站点视图）
        primary_station = station_list[0] if station_list else {}
        primary_device = devices[0] if devices else {}
        station_info = {
            "device_name": primary_station.get("device_name") or primary_device.get("name") or "cloud",
            "device_location": primary_station.get("location") or primary_station.get("name") or "云服务器",
            "location": primary_station.get("location") or primary_station.get("name") or "云服务器",
            "position": primary_station.get("position", {"lat": 0, "lon": 0, "alt": 0}),
            "mqtt_online": primary_station.get("mqtt_online", False),
            "pl_count": len(get_power_lines()),
            "drone_count": len(drones),
        }

        return jsonify({
            "hourly_alerts": hourly,
            "model_dist": get_drone_model_distribution(
                {d["name"] for d in devices} if permitted is not None else None
            ),
            "station": station_info,
            "stations": stations,
            "station_list": station_list,
        })
    except Exception as e:
        logger.error("stats/dashboard error: %s", e)
        return jsonify({"error": "服务器内部错误"}), 500


# ── Device Provisioning ──

def _require_device_admin(f):
    """允许 admin 或 tenant_admin 访问设备管理接口"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "未登录"}), 401
        if session["user"].get("role") not in ("admin", "tenant_admin"):
            return jsonify({"error": "需要管理员或租户管理员权限"}), 403
        return f(*args, **kwargs)
    return decorated


@bp.route("/api/devices/provision", methods=["POST"])
@_require_device_admin
def api_provision_device():
    """管理员/租户管理员注册新边缘设备: 生成 secret + 签发 mTLS 证书 + 关联站点"""
    if not _rate_limit(f"provision:{session['user']['username']}", max_requests=10, window_sec=3600):
        return jsonify({"error": "设备注册次数过多，请 1 小时后再试"}), 429
    import secrets
    from datetime import datetime, timezone
    from .cert_manager import get_cert_manager

    u = session["user"]
    data = request.json or {}
    device_name = (data.get("device_name") or "").strip()
    station = (data.get("station") or "").strip()

    if not device_name:
        return jsonify({"error": "device_name 不能为空"}), 400
    if len(device_name) < 2 or len(device_name) > 64:
        return jsonify({"error": "device_name 长度 2-64"}), 400

    # tenant_admin 自动绑定到自己的租户, admin 必须选择租户
    if u.get("role") == "tenant_admin":
        tenant_id = u.get("tenant_id")
    else:
        tenant_id = data.get("tenant_id")
        if not tenant_id:
            return jsonify({"error": "请选择所属租户（admin 注册设备必须绑定租户）"}), 400
        # 验证租户存在
        from .models import get_tenants as _gt
        valid_ids = {t["id"] for t in _gt()}
        if int(tenant_id) not in valid_ids:
            return jsonify({"error": f"租户 #{tenant_id} 不存在，请先创建租户"}), 400

    from .models import get_device_secrets as _gds
    existing_devices = {d["device_name"] for d in _gds()}
    if device_name in existing_devices:
        return jsonify({"error": f"设备 {device_name} 已注册，请先删除或吊销"}), 409

    # 生成随机密钥
    device_secret = secrets.token_hex(24)

    # 签发 mTLS 客户端证书
    try:
        cert_mgr = get_cert_manager()
        cert_data = cert_mgr.issue_device_cert(device_name)
    except Exception as e:
        return jsonify({"error": f"证书签发失败: {e}"}), 500

    upsert_device_secret(device_name, device_secret, station,
                         client_cert=cert_data["cert"],
                         cert_serial=cert_data["serial"],
                         cert_issued_at=datetime.now(timezone.utc),
                         tenant_id=tenant_id)

    # 自动创建对应站点 (绑定租户)
    if station:
        from .models import get_stations, upsert_station
        stations = get_stations()
        if not any(s["name"] == station for s in stations):
            upsert_station(name=station, device_name=device_name, tenant_id=tenant_id)

    add_audit_log(session["user"]["username"], "PROVISION", "device_secrets", None,
                  f"注册设备: {device_name} → {station or '(无站点)'} tenant={tenant_id} cert={cert_data['serial']}")

    return jsonify({
        "status": "ok",
        "device_name": device_name,
        "device_secret": device_secret,
        "client_cert": cert_data["cert"],
        "client_key": cert_data["key"],
        "ca_cert": cert_data["ca_cert"],
        "station": station,
    })


@bp.route("/api/devices", methods=["GET"])
@_require_device_admin
def api_list_devices():
    """列出设备及其密钥信息 — admin 看全部, tenant_admin 只看自己租户的"""
    u = session["user"]
    tid = None if u.get("role") == "admin" else u.get("tenant_id")
    device_secrets = get_device_secrets(tenant_id=tid)
    stations = get_stations()
    station_map = {s["device_name"]: s["name"] for s in stations if s["device_name"]}

    return jsonify([{
        "device_name": d["device_name"],
        "station": d.get("station") or station_map.get(d["device_name"], ""),
        "cert_serial": d.get("cert_serial") or "",
        "cert_issued_at": d.get("cert_issued_at") or "",
        "revoked": d.get("revoked", False),
        "created_at": d.get("created_at") or "",
        "tenant_id": d.get("tenant_id"),
    } for d in device_secrets])


@bp.route("/api/devices/<device_name>", methods=["DELETE"])
@_require_device_admin
def api_delete_device(device_name):
    """删除设备 — 租户管理员只能删除自己租户的设备"""
    u = session["user"]
    if u.get("role") == "tenant_admin":
        devices = get_device_secrets(tenant_id=u.get("tenant_id"))
        if not any(d["device_name"] == device_name for d in devices):
            return jsonify({"error": "设备不存在或不属于您的租户"}), 403
    ok = delete_device_secret(device_name)
    if ok:
        add_audit_log(session["user"]["username"], "DELETE", "device_secrets", None,
                      f"注销设备: {device_name}")
    return jsonify({"status": "ok" if ok else "not found"})


@bp.route("/api/devices/<device_name>/revoke", methods=["POST"])
@_require_device_admin
def api_revoke_device(device_name):
    """吊销设备证书 — 租户管理员只能吊销自己租户的设备"""
    u = session["user"]
    if u.get("role") == "tenant_admin":
        devices = get_device_secrets(tenant_id=u.get("tenant_id"))
        if not any(d["device_name"] == device_name for d in devices):
            return jsonify({"error": "设备不存在或不属于您的租户"}), 403
    try:
        from .cert_manager import get_cert_manager
        cm = get_cert_manager()
        if not cm:
            return jsonify({"error": "证书管理器未初始化"}), 503
        ok = cm.revoke_device_cert(device_name)
        if ok:
            add_audit_log(session["user"]["username"], "REVOKE", "device_secrets", None,
                          f"吊销设备证书: {device_name}")
            return jsonify({"status": "ok"})
        return jsonify({"error": "设备不存在"}), 404
    except Exception as e:
        logger.error("revoke device error: %s", e)
        return jsonify({"error": "服务器内部错误"}), 500


# ── Drone Whitelist ──

@bp.route("/api/whitelist", methods=["GET", "POST", "DELETE"])
@require_web_auth
def api_whitelist():
    if request.method == "GET":
        tenant_id, permitted = _user_scope()
        # admin 看全部, 租户看自己的
        tid = None if session["user"].get("role") == "admin" else session["user"].get("tenant_id")
        return jsonify(get_whitelist(tenant_id=tid))

    u = session["user"]
    if u.get("role") not in ("admin", "tenant_admin"):
        return jsonify({"error": "需要管理员或租户管理员权限"}), 403

    if request.method == "POST":
        data = request.json or {}
        sn = (data.get("sn") or "").strip()
        if not sn:
            return jsonify({"error": "SN 不能为空"}), 400
        match_mode = data.get("match_mode", "exact")
        if match_mode not in ("exact", "prefix"):
            return jsonify({"error": "match_mode 必须是 exact 或 prefix"}), 400
        note = (data.get("note") or "").strip()
        tid = u.get("tenant_id") if u.get("role") == "tenant_admin" else data.get("tenant_id")
        wid = add_to_whitelist(sn=sn, match_mode=match_mode, note=note,
                               tenant_id=tid, created_by=u["username"])
        add_audit_log(u["username"], "INSERT", "drone_whitelist", wid,
                      f"新增白名单: {sn} mode={match_mode}")
        return jsonify({"status": "ok", "id": wid})

    # DELETE
    data = request.json or {}
    wid = data.get("id")
    if not wid:
        return jsonify({"error": "缺少 id 参数"}), 400
    # 权限校验: tenant_admin 只能删除自己租户的
    if u.get("role") == "tenant_admin":
        entries = get_whitelist(tenant_id=u.get("tenant_id"))
        if not any(e["id"] == int(wid) for e in entries):
            return jsonify({"error": "白名单条目不存在或不属于您的租户"}), 403
    ok = remove_from_whitelist(int(wid))
    if ok:
        add_audit_log(u["username"], "DELETE", "drone_whitelist", wid,
                      f"删除白名单 id={wid}")
    return jsonify({"status": "ok" if ok else "not found"})
