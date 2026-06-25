"""POST /api/heartbeat"""

from datetime import datetime

from flask import Blueprint, request, jsonify

from .models import upsert_device
from .auth import require_auth

from logging_config import get_logger

bp = Blueprint("heartbeat", __name__)
logger = get_logger(__name__)

@bp.route("/api/heartbeat", methods=["POST"])
@require_auth
def api_heartbeat():
    try:
        data = request.json or {}
        device_name = data.get("device", "unknown")
        upsert_device(
            device_name,
            location=data.get("location", ""),
            lat=data.get("device_lat", 0),
            lon=data.get("device_lon", 0),
            alt=data.get("device_alt", 0),
        )
        return jsonify({"status": "ok", "server_time": datetime.now().isoformat()})
    except Exception as e:
        logger.error("heartbeat error: %s", e)
        return jsonify({"error": "服务器内部错误"}), 500
