"""POST /api/report, /api/report_alert"""

from flask import Blueprint, request, jsonify

from .models import upsert_device, upsert_drone, update_drone_status, add_alert
from .auth import require_auth
from logging_config import get_logger

bp = Blueprint("report", __name__)
logger = get_logger(__name__)


@bp.route("/api/report", methods=["POST"])
@require_auth
def api_report():
    try:
        data = request.json
        if not data:
            return jsonify({"error": "empty body"}), 400

        device_name = data.get("device", "unknown")
        drone_id = data.get("drone_id", "")
        lat = data.get("latitude", 0)
        lon = data.get("longitude", 0)
        alt = data.get("altitude", 0)
        distance = data.get("distance_to_line")
        line_name = data.get("nearest_line", "")
        status = data.get("status", "active")

        upsert_device(device_name, lat=lat, lon=lon, alt=alt)

        if drone_id:
            upsert_drone(device_name, drone_id, lat, lon, alt)
            if distance is not None:
                update_drone_status(device_name, drone_id, distance,
                                    line_name, status)

        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error("report error: %s", e)
        return jsonify({"error": str(e)}), 500


@bp.route("/api/report_alert", methods=["POST"])
@require_auth
def api_report_alert():
    try:
        data = request.json
        if not data:
            return jsonify({"error": "empty body"}), 400

        device_name = data.get("device", "unknown")
        drone_id = data.get("drone_id", "")
        level = data.get("level", "warning")
        distance = data.get("distance", 0)
        line_name = data.get("nearest_line", "")
        lat = data.get("latitude", 0)
        lon = data.get("longitude", 0)
        alt = data.get("altitude", 0)

        message = f"[{level}] {drone_id} 接近 {line_name} 距离{distance:.0f}m"

        upsert_device(device_name, lat=lat, lon=lon, alt=alt)
        add_alert(device_name, drone_id, level, distance, line_name, message)
        if drone_id:
            upsert_drone(device_name, drone_id, lat, lon, alt)
            update_drone_status(device_name, drone_id, distance, line_name, level)

        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error("report_alert error: %s", e)
        return jsonify({"error": str(e)}), 500
