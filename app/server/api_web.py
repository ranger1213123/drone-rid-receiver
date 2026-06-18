"""
Web GUI REST API Blueprint — 云服务器模式
Session-based 鉴权 (admin/operator)，数据库持久化
"""
import csv
import io
import os
from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify, session

from .models import (
    get_devices, get_all_drones,
    get_recent_alerts, acknowledge_alert, get_hourly_alert_counts,
    get_power_lines, upsert_power_line, delete_power_line,
    get_web_users, verify_web_user, upsert_web_user, delete_web_user, count_admin_users,
    get_stations, upsert_station, delete_station,
    get_settings, get_setting, set_setting,
    add_audit_log, get_audit_logs,
    get_device_secrets, upsert_device_secret, delete_device_secret,
)
from .auth import require_auth
from logging_config import get_logger

bp = Blueprint("api_web", __name__)
logger = get_logger(__name__)


def _safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ── Auth decorators ──

def require_web_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "未登录"}), 401
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


def _user_station():
    """当前用户的站点名 (operator 被限定到站点)"""
    u = session.get("user", {})
    if u.get("role") == "admin":
        return None  # admin 看全部
    return u.get("station", "") or None


# ── Power Lines ──

@bp.route("/api/powerlines", methods=["GET", "POST"])
@require_web_auth
def api_powerlines():
    if request.method == "GET":
        # 可选 ?device_name=X 过滤
        dev = request.args.get("device_name", "").strip() or None
        lines = get_power_lines(device_name=dev)
        return jsonify(lines)

    # POST — 新增
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "电力线名称不能为空"}), 400

    pl_id = upsert_power_line({
        "name": name,
        "lat1": _safe_float(data.get("lat1")),
        "lon1": _safe_float(data.get("lon1")),
        "alt1": _safe_float(data.get("alt1")),
        "lat2": _safe_float(data.get("lat2")),
        "lon2": _safe_float(data.get("lon2")),
        "alt2": _safe_float(data.get("alt2")),
        "voltage_level": (data.get("voltage_level") or "").strip(),
        "device_name": (data.get("device_name") or "").strip() or None,
    })
    add_audit_log(session["user"]["username"], "INSERT", "power_lines", pl_id,
                  f"新增电力线: {name}")
    return jsonify({"status": "ok", "id": pl_id})


@bp.route("/api/powerlines/<int:pl_id>", methods=["PUT", "DELETE"])
@require_web_auth
def api_modify_powerline(pl_id):
    if request.method == "DELETE":
        pl = get_power_lines()
        target = next((l for l in pl if l["id"] == pl_id), None)
        ok = delete_power_line(pl_id)
        if ok:
            add_audit_log(session["user"]["username"], "DELETE", "power_lines", pl_id,
                          f"删除电力线: {target['name'] if target else pl_id}")
            return jsonify({"status": "ok"})
        return jsonify({"error": "电力线不存在"}), 404

    # PUT — 编辑
    data = request.json or {}
    upsert_power_line({
        "id": pl_id,
        "name": (data.get("name") or "").strip(),
        "lat1": _safe_float(data.get("lat1")),
        "lon1": _safe_float(data.get("lon1")),
        "alt1": _safe_float(data.get("alt1")),
        "lat2": _safe_float(data.get("lat2")),
        "lon2": _safe_float(data.get("lon2")),
        "alt2": _safe_float(data.get("alt2")),
        "voltage_level": (data.get("voltage_level") or "").strip(),
        "device_name": (data.get("device_name") or "").strip() or None,
    })
    add_audit_log(session["user"]["username"], "UPDATE", "power_lines", pl_id,
                  f"编辑电力线: {data.get('name', pl_id)}")
    return jsonify({"status": "ok"})


@bp.route("/api/powerlines/import", methods=["POST"])
@require_web_auth
def api_import_powerlines():
    """批量导入电力线 — JSON 数组或 CSV 文本"""
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
    device_name = request.args.get("device_name", "").strip() or None
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
@require_web_auth
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

@bp.route("/api/stations", methods=["GET", "POST", "DELETE"])
@require_web_auth
def api_stations():
    if request.method == "GET":
        stations = get_stations()
        station_filter = _user_station()
        if station_filter:
            stations = [s for s in stations if s["name"] == station_filter]
        return jsonify(stations)

    if request.method == "POST":
        if session["user"].get("role") != "admin":
            return jsonify({"error": "需要管理员权限"}), 403
        data = request.json or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "站点名称不能为空"}), 400
        upsert_station(
            name=name,
            location=(data.get("location") or "").strip(),
            lat=_safe_float(data.get("lat")),
            lon=_safe_float(data.get("lon")),
            alt=_safe_float(data.get("alt")),
            device_name=(data.get("device_name") or "").strip() or None,
        )
        add_audit_log(session["user"]["username"], "INSERT", "stations", None,
                      f"新增站点: {name}")
        return jsonify({"status": "ok"})

    # DELETE
    if session["user"].get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "缺少 name 参数"}), 400
    ok = delete_station(name)
    if ok:
        add_audit_log(session["user"]["username"], "DELETE", "stations", None,
                      f"删除站点: {name}")
    return jsonify({"status": "ok" if ok else "not found"})


# ── Users ──

@bp.route("/api/users", methods=["GET", "POST", "DELETE"])
@require_web_auth
def api_users():
    if request.method == "GET":
        users = get_web_users()
        station_filter = _user_station()
        if station_filter:
            users = [u for u in users if u["station"] == station_filter or u["role"] == "admin"]
        return jsonify(users)

    if session["user"].get("role") != "admin":
        return jsonify({"error": "需要管理员权限"}), 403

    if request.method == "POST":
        data = request.json or {}
        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()
        role = (data.get("role") or "user").strip()
        station = (data.get("station") or "").strip()
        if not username or not password:
            return jsonify({"error": "用户名和密码不能为空"}), 400
        # 检查是否已存在
        existing = get_web_users()
        if any(u["username"] == username for u in existing):
            return jsonify({"error": "用户名已存在"}), 409
        upsert_web_user(username, password, role, station)
        add_audit_log(session["user"]["username"], "INSERT", "web_users", None,
                      f"新增用户: {username}")
        return jsonify({"status": "ok"})

    # DELETE
    data = request.json or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"error": "缺少 username 参数"}), 400
    existing = get_web_users()
    admins = [u for u in existing if u.get("role") == "admin"]
    target = next((u for u in existing if u["username"] == username), None)
    if target and target.get("role") == "admin" and len(admins) <= 1:
        return jsonify({"error": "不能删除最后一个管理员账户"}), 400
    ok = delete_web_user(username)
    if ok:
        add_audit_log(session["user"]["username"], "DELETE", "web_users", None,
                      f"删除用户: {username}")
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

    # operator 只看自己站点的设备
    station_filter = _user_station()
    if station_filter:
        # 查该站点的 device_name
        stations = get_stations()
        station_devs = [s["device_name"] for s in stations if s["name"] == station_filter and s["device_name"]]
        if station_devs:
            device_name = station_devs[0] if not device_name else device_name

    limit = min(int(request.args.get("limit", 100)), 1000)
    alerts = get_recent_alerts(
        limit=limit, level=level, since=since, to_date=to_date,
        drone_id=drone_id, device_name=device_name, acknowledged=acknowledged,
    )
    return jsonify(alerts)


@bp.route("/api/alerts/<int:alert_id>/acknowledge", methods=["POST"])
@require_web_auth
def api_acknowledge_alert(alert_id):
    user = session.get("user", {})
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


@bp.route("/api/drones/export")
@require_web_auth
def api_drones_export():
    drones = get_all_drones()
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
            "pilot_notify_enabled": "false",
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

        station_filter = _user_station()
        if station_filter:
            stations = [s for s in stations if s["name"] == station_filter]
            devices = [d for d in devices if any(
                s["device_name"] == d["name"] for s in stations
            )]
            station_devs = [s["device_name"] for s in stations if s["device_name"]]
            drones = [d for d in drones if d["device_name"] in station_devs]

        return jsonify({
            "hourly_alerts": hourly,
            "model_dist": [],   # 云端暂无机型识别
            "station": {
                "device_name": "cloud",
                "device_location": "云服务器",
                "position": {"lat": 0, "lon": 0, "alt": 0},
                "active_channel": "cloud",
                "primary_online": True,
                "beidou_online": False,
                "beidou_signal": 0,
                "queue_size": 0,
                "http_sent": 0,
                "beidou_sent": 0,
                "last_send": "--",
                "pl_count": len(get_power_lines()),
                "drone_count": len(drones),
            },
            "stations": stations,
        })
    except Exception as e:
        logger.error("stats/dashboard error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Device Provisioning ──

@bp.route("/api/devices/provision", methods=["POST"])
@require_admin
def api_provision_device():
    """管理员注册新边缘设备: 生成 secret + 签发 mTLS 证书 + 关联站点"""
    import secrets
    from datetime import datetime, timezone
    from .cert_manager import get_cert_manager

    data = request.json or {}
    device_name = (data.get("device_name") or "").strip()
    station = (data.get("station") or "").strip()

    if not device_name:
        return jsonify({"error": "device_name 不能为空"}), 400
    if len(device_name) < 2 or len(device_name) > 64:
        return jsonify({"error": "device_name 长度 2-64"}), 400

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
                         cert_issued_at=datetime.now(timezone.utc))

    # 自动创建对应站点
    if station:
        from .models import get_stations, upsert_station
        stations = get_stations()
        if not any(s["name"] == station for s in stations):
            upsert_station(name=station, device_name=device_name)

    add_audit_log(session["user"]["username"], "PROVISION", "device_secrets", None,
                  f"注册设备: {device_name} → {station or '(无站点)'} cert={cert_data['serial']}")

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
@require_admin
def api_list_devices():
    """列出所有注册设备及其密钥信息"""
    secrets = get_device_secrets()
    stations = get_stations()
    station_map = {s["device_name"]: s["name"] for s in stations if s["device_name"]}

    return jsonify([{
        "device_name": name,
        "station": station_map.get(name, ""),
        "created_at": "",  # get_device_secrets doesn't return created_at
    } for name in secrets])


@bp.route("/api/devices/<device_name>", methods=["DELETE"])
@require_admin
def api_delete_device(device_name):
    ok = delete_device_secret(device_name)
    if ok:
        add_audit_log(session["user"]["username"], "DELETE", "device_secrets", None,
                      f"注销设备: {device_name}")
    return jsonify({"status": "ok" if ok else "not found"})
