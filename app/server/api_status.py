"""GET /api/status — 双模式: web session + device JWT"""

from datetime import datetime

from flask import Blueprint, jsonify, request, session

from .models import (
    get_devices, get_all_drones, get_recent_alerts,
    get_user_stations, get_stations,
)
from .auth import _verify_token

bp = Blueprint("status", __name__)


def _is_web_session():
    return "user" in session


def _is_device_auth():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    return _verify_token(auth_header[7:]) is not None


@bp.route("/api/status")
def api_status():
    # 双模式鉴权: web session 或 device JWT
    if not _is_web_session() and not _is_device_auth():
        return jsonify({"error": "未登录或 token 无效"}), 401

    # ── 分页参数 (前端列表场景) ──
    page = request.args.get("page", type=int)
    per_page = request.args.get("per_page", 50, type=int)
    since = request.args.get("since")  # ISO timestamp, 增量模式

    devices = get_devices()
    drones = get_all_drones()
    alerts = get_recent_alerts(limit=50)

    # Web session: 按租户/站点过滤
    if _is_web_session():
        u = session.get("user", {})
        if u.get("role") != "admin":
            permitted = get_user_stations(u.get("username", ""))
            if permitted is not None:
                all_stations = get_stations()
                station_devs = set(
                    s["device_name"] for s in all_stations
                    if s["name"] in permitted and s.get("device_name")
                )
                devices = [d for d in devices if d["name"] in station_devs]
                drones = [d for d in drones if d.get("device_name") in station_devs]
                alerts = [a for a in alerts if a.get("device_name") in station_devs]

    total_devices = len(devices)
    online_devices = sum(1 for d in devices if d["status"] == "online")
    active_drones = len(drones)
    crit = sum(1 for d in drones if d["status"] == "critical")
    sev = sum(1 for d in drones if d["status"] == "severe")
    warn = sum(1 for d in drones if d["status"] == "warning")

    # ── 增量模式: 只返回 since 时间之后更新的无人机 ──
    if since and drones:
        drones = [d for d in drones if d.get("last_seen", "") >= since]

    total_drones = len(drones)

    # ── 分页 ──
    if page and page >= 1:
        start = (page - 1) * per_page
        drones = drones[start:start + per_page]

    result = {
        "server_time": datetime.now().strftime("%H:%M:%S"),
        "mode": "cloud",
        "running": True,
        "devices": {
            "total": total_devices,
            "online": online_devices,
            "offline": total_devices - online_devices,
            "list": devices,
        },
        "drones": drones,
        "drone_count": active_drones,
        "drone_total": total_drones,
        "drone_stats": {
            "total": active_drones,
            "critical": crit,
            "severe": sev,
            "warning": warn,
        },
        "alerts": [{
            "time": a["timestamp"][:19] if a["timestamp"] else "",
            "device": a["device_name"],
            "drone": a["drone_id"],
            "level": a["level"],
            "distance": a["distance"],
            "line": a["line_name"],
            "msg": a["message"],
        } for a in alerts],
        "backhaul": None,       # 云端无边缘回传, 前端 if(bh) 守卫兼容
        "alert_count": len(alerts),
        "pl_count": 0,          # 由 /api/powerlines 独立获取
    }

    # Web session: 附加 current_user
    if _is_web_session():
        u = session["user"]
        result["current_user"] = {
            "username": u.get("username", ""),
            "role": u.get("role", "user"),
            "station": u.get("station", ""),
            "tenant_id": u.get("tenant_id"),
            "scope": u.get("scope", "station"),
            "assigned_station": u.get("assigned_station", ""),
        }

    return jsonify(result)
