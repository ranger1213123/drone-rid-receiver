"""POST /api/heartbeat"""

from datetime import datetime

from flask import Blueprint, request, jsonify, g

from .models import upsert_device, get_session, Station
from . import socketio
from .auth import require_auth

from logging_config import get_logger

bp = Blueprint("heartbeat", __name__)
logger = get_logger(__name__)

@bp.route("/api/heartbeat", methods=["POST"])
@require_auth
def api_heartbeat():
    try:
        data = request.json or {}
        device_name = getattr(g, "device_name", data.get("device", "unknown"))
        device_lat = data.get("device_lat")
        device_lon = data.get("device_lon")
        device_alt = data.get("device_alt")
        upsert_device(
            device_name,
            location=data.get("location", ""),
            lat=device_lat if device_lat and device_lat != 0 else None,
            lon=device_lon if device_lon and device_lon != 0 else None,
            alt=device_alt if device_alt is not None else None,
        )

        # Sync GPS to associated station + auto-geocode
        if device_name != "unknown" and device_lat and device_lon:
            sess = get_session()
            st = sess.query(Station).filter(
                Station.device_name == device_name
            ).first()
            if st:
                st.lat = device_lat
                st.lon = device_lon
                sess.commit()
                # Auto-geocode if station has no province yet
                if not st.province:
                    from .api_web import _auto_geocode_station
                    _auto_geocode_station(st.name, device_lat, device_lon)

        return jsonify({"status": "ok", "server_time": datetime.now().isoformat()})
    except Exception as e:
        logger.error("heartbeat error: %s", e)
        return jsonify({"error": "服务器内部错误"}), 500
