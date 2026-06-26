"""POST /api/report, /api/report_alert — 含云侧 SMS + 阈值分类 + 去重 + 白名单"""

import os
import time as _time

from flask import Blueprint, request, jsonify, g

from .models import (
    upsert_device, upsert_drone, update_drone_status, add_alert,
    get_personnel_by_station, get_all_alert_phones,
    get_setting, is_drone_whitelisted,
)
from . import socketio
from .auth import require_auth
from logging_config import get_logger

bp = Blueprint("report", __name__)
logger = get_logger(__name__)

# ── 告警去重冷却 (模块级状态) ──
_alert_cooldown: dict = {}       # {(drone_id, level): last_alert_time}
_ALERT_COOLDOWN_SEC = 30.0

# ── SMS 网关 (懒加载) ──
_sms_gateway = None


def _get_sms_gateway():
    global _sms_gateway
    if _sms_gateway is not None:
        return _sms_gateway

    sms_enabled = os.environ.get("SMS_ENABLED", "0")
    if sms_enabled not in ("1", "true", "True"):
        from core.sms_gateway import SimulatedSMSGateway
        _sms_gateway = SimulatedSMSGateway()
        logger.info("SMS: 模拟模式 (SMS_ENABLED=0)")
        return _sms_gateway

    provider = os.environ.get("SMS_PROVIDER", "alibaba")
    if provider == "alibaba":
        from core.sms_gateway import AlibabaSMSGateway
        _sms_gateway = AlibabaSMSGateway(
            access_key=os.environ.get("ALIBABA_ACCESS_KEY", ""),
            access_secret=os.environ.get("ALIBABA_ACCESS_SECRET", ""),
            sign_name=os.environ.get("ALIBABA_SIGN_NAME", "无人机防碰撞监测"),
            template_code=os.environ.get("ALIBABA_TEMPLATE_CODE", ""),
        )
        logger.info("SMS: 阿里云模式")
    else:
        from core.sms_gateway import SimulatedSMSGateway
        _sms_gateway = SimulatedSMSGateway()
        logger.info("SMS: 模拟模式 (unknown provider=%s)", provider)

    return _sms_gateway


def _notify_station_personnel(device_name: str, drone_id: str, level: str,
                              distance: float, line_name: str, lat: float, lon: float):
    """向站点负责人发送告警短信"""
    try:
        gateway = _get_sms_gateway()
        # 查找该设备的站点负责人
        from .models import get_stations
        stations = get_stations()
        station_name = None
        for s in stations:
            if s["device_name"] == device_name:
                station_name = s["name"]
                break

        if station_name:
            personnel = get_personnel_by_station(station_name)
            phones = [p["phone"] for p in personnel if p.get("phone")]
        else:
            phones = get_all_alert_phones()

        if not phones:
            # 回退: 环境变量配置的应急电话
            fallback = os.environ.get("SMS_ALERT_PHONES", "")
            phones = [p.strip() for p in fallback.split(",") if p.strip()]

        if not phones:
            logger.info("SMS: 无接收号码 (device=%s station=%s)", device_name, station_name)
            return

        coords = f"({lat:.4f},{lon:.4f})" if lat or lon else ""
        msg = f"[{level.upper()}] {drone_id} 接近 {line_name} 距离{distance:.0f}m {coords} — 设备{device_name}"
        gateway.send(phones, msg)
    except Exception as e:
        logger.error("SMS notification error: %s", e)


@bp.route("/api/report", methods=["POST"])
@require_auth
def api_report():
    try:
        data = request.json
        if not data:
            return jsonify({"error": "empty body"}), 400

        device_name = g.device_name
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
            # WebSocket 实时推送
            socketio.emit('drone_update', {
                'drone_id': drone_id,
                'lat': lat, 'lon': lon, 'alt': alt,
                'distance': distance or 0,
                'nearest_line': line_name,
                'status': status,
            })

        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error("report error: %s", e)
        return jsonify({"error": "服务器内部错误"}), 500


@bp.route("/api/report_alert", methods=["POST"])
@require_auth
def api_report_alert():
    try:
        data = request.json
        if not data:
            return jsonify({"error": "empty body"}), 400

        device_name = g.device_name
        drone_id = data.get("drone_id", "")
        level = data.get("level", "warning")
        distance = data.get("distance", 0)
        line_name = data.get("nearest_line", "")
        lat = data.get("latitude", 0)
        lon = data.get("longitude", 0)
        alt = data.get("altitude", 0)

        # ── 服务器阈值重分类 ──
        t_warn = int(get_setting("threshold_warning", "200"))
        t_sev = int(get_setting("threshold_severe", "100"))
        t_crit = int(get_setting("threshold_critical", "50"))
        d = float(distance) if distance else 0
        if d <= t_crit:
            level = "critical"
        elif d <= t_sev:
            level = "severe"
        elif d <= t_warn:
            level = "warning"
        elif not level:
            level = "active"

        upsert_device(device_name, lat=lat, lon=lon, alt=alt)

        # ── 白名单检查 ──
        if is_drone_whitelisted(drone_id):
            logger.info("Whitelisted drone %s: alert suppressed", drone_id)
            # 仍然更新无人机位置
            if drone_id:
                upsert_drone(device_name, drone_id, lat, lon, alt)
                update_drone_status(device_name, drone_id, distance, line_name, level)
                socketio.emit('drone_update', {
                    'drone_id': drone_id, 'lat': lat, 'lon': lon, 'alt': alt,
                    'distance': distance or 0, 'nearest_line': line_name, 'status': level,
                })
            return jsonify({"status": "ok", "whitelisted": True})

        # ── 告警去重 ──
        _cooldown_key = (drone_id, level)
        _now = _time.time()
        _last = _alert_cooldown.get(_cooldown_key, 0)
        if _now - _last >= _ALERT_COOLDOWN_SEC:
            _alert_cooldown[_cooldown_key] = _now

            # 清理超过 120 秒的旧冷却条目
            stale = [k for k, v in _alert_cooldown.items() if _now - v > 120]
            for k in stale:
                del _alert_cooldown[k]

            message = f"[{level}] {drone_id} 接近 {line_name} 距离{d:.0f}m"
            add_alert(device_name, drone_id, level, distance, line_name, message)

            if drone_id:
                upsert_drone(device_name, drone_id, lat, lon, alt)
                update_drone_status(device_name, drone_id, distance, line_name, level)

            # 云侧 SMS
            _notify_station_personnel(device_name, drone_id, level, distance,
                                      line_name, lat, lon)

            # WebSocket 实时推送
            if drone_id:
                socketio.emit('drone_update', {
                    'drone_id': drone_id, 'lat': lat, 'lon': lon, 'alt': alt,
                    'distance': distance or 0, 'nearest_line': line_name, 'status': level,
                })
            socketio.emit('alert_update', {
                'drone_id': drone_id, 'level': level,
                'line_name': line_name, 'distance': d,
            })
        else:
            # 跳过重复告警，但更新位置
            if drone_id:
                upsert_drone(device_name, drone_id, lat, lon, alt)
                update_drone_status(device_name, drone_id, distance, line_name, level)
                socketio.emit('drone_update', {
                    'drone_id': drone_id, 'lat': lat, 'lon': lon, 'alt': alt,
                    'distance': distance or 0, 'nearest_line': line_name, 'status': level,
                })

        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error("report_alert error: %s", e)
        return jsonify({"error": "服务器内部错误"}), 500
