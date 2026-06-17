"""GET /api/status"""

from datetime import datetime

from flask import Blueprint, jsonify

from .models import get_devices, get_all_drones, get_recent_alerts, mark_stale_devices
from .auth import require_auth

bp = Blueprint("status", __name__)


@bp.route("/api/status")
@require_auth
def api_status():
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

    return jsonify({
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
    })
