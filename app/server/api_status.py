"""GET /api/status — 双模式: web session + device JWT"""

from datetime import datetime

from flask import Blueprint, jsonify, request, session

from .models import get_devices, get_all_drones, get_recent_alerts, mark_stale_devices
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

    mark_stale_devices(timeout_seconds=120)

    devices = get_devices()
    drones = get_all_drones()
    alerts = get_recent_alerts(limit=50)

    total_devices = len(devices)
    online_devices = sum(1 for d in devices if d["status"] == "online")
    active_drones = len(drones)
    crit = sum(1 for d in drones if d["status"] == "critical")
    sev = sum(1 for d in drones if d["status"] == "severe")
    warn = sum(1 for d in drones if d["status"] == "warning")

    result = {
        "server_time": datetime.now().strftime("%H:%M:%S"),
        "devices": {
            "total": total_devices,
            "online": online_devices,
            "offline": total_devices - online_devices,
            "list": devices,
        },
        "drones": {
            "total": active_drones,
            "critical": crit,
            "severe": sev,
            "warning": warn,
            "list": drones,
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
    }

    # Web session: 附加 current_user + backhaul null (前端兼容)
    if _is_web_session():
        u = session["user"]
        result["current_user"] = {
            "username": u.get("username", ""),
            "role": u.get("role", "user"),
            "station": u.get("station", ""),
        }
        result["backhaul"] = None
        result["mode"] = "cloud"
        result["running"] = True
        result["drone_count"] = active_drones
        result["alert_count"] = len(alerts)
        result["pl_count"] = 0  # 由 api_powerlines 独立获取

    return jsonify(result)
