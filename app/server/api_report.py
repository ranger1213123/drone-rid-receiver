"""POST /api/report, /api/report_alert — 含云侧企微通知 + 阈值分类 + 去重 + 白名单"""

import os
import time as _time
from typing import Optional

from flask import Blueprint, request, jsonify, g

from .models import (
    upsert_device, upsert_drone, update_drone_status, add_alert,
    get_setting, is_drone_whitelisted, add_drone_position,
)
from . import socketio
from .auth import require_auth
from logging_config import get_logger

bp = Blueprint("report", __name__)
logger = get_logger(__name__)

# ── 告警去重冷却 (使用 DB 防抖设置) ──
_alert_cooldown: dict = {}       # {(drone_id, level): last_alert_time}


def _get_cooldown_sec(level: str) -> float:
    """根据防抖设置返回该告警级别的冷却时间(秒)"""
    if get_setting("anti_flapping_enabled", "false") not in ("1", "true", "True"):
        return 30.0  # 关闭防抖时仍保留基础冷却
    if level == "critical":
        return float(get_setting("debounce_in", "3"))
    else:
        return float(get_setting("debounce_out", "10"))

# ── 企业微信 Webhook 通知 (按 URL 缓存实例) ──
_webhook_cache: dict = {}


def _get_webhook_for_station(station_name: str = "") -> Optional[object]:
    """获取站点级 Webhook URL (优先站点配置，兜底全局设置)"""
    from core.webhook_notifier import create_webhook_notifier

    webhook_url = os.environ.get("WEBHOOK_URL", "")

    # 站点级 URL 优先
    if station_name:
        try:
            from .models import get_stations
            stations = get_stations()
            for s in stations:
                if s["name"] == station_name and s.get("webhook_url"):
                    webhook_url = s["webhook_url"]
                    break
        except Exception:
            pass

    # 兜底全局设置
    if not webhook_url:
        try:
            webhook_url = get_setting("webhook_url", "")
        except Exception:
            pass

    if not webhook_url:
        return None

    # 按 URL 缓存实例
    webhook = _webhook_cache.get(webhook_url)
    if webhook is None:
        webhook = create_webhook_notifier(webhook_url)
        _webhook_cache[webhook_url] = webhook
    return webhook


def _notify_station_personnel(device_name: str, drone_id: str, level: str,
                              distance: float, line_name: str, lat: float, lon: float):
    """通过企业微信机器人 + SMS 发送告警通知"""
    try:
        from .models import get_stations
        stations = get_stations()
        station_name = None
        for s in stations:
            if s["device_name"] == device_name:
                station_name = s["name"]
                break

        if not station_name:
            station_name = device_name

        # 1. 企业微信 Webhook 通知
        webhook = _get_webhook_for_station(station_name)
        if webhook is not None:
            webhook.send_alert(
                station_name=station_name,
                drone_id=drone_id,
                level=level,
                distance=distance,
                line_name=line_name,
                lat=lat,
                lon=lon,
            )

        # 2. SMS 通知 (发送给站点联系人)
        _send_sms_alert(station_name, drone_id, level, distance, line_name)
    except Exception as e:
        logger.error("通知异常: %s", e)


def _send_sms_alert(station_name: str, drone_id: str, level: str,
                    distance: float, line_name: str):
    """向站点联系人发送 SMS 告警"""
    try:
        from .models import get_personnel_by_station
        from core.sms_gateway import create_sms_gateway, SimulatedSMSGateway

        phones = [p["phone"] for p in get_personnel_by_station(station_name) if p.get("phone")]
        if not phones:
            return

        msg = f"[{level}] {drone_id}接近{line_name} {distance:.0f}m"
        gw = _get_sms_gateway()
        gw.send(phones, msg)
    except Exception as e:
        logger.error("SMS 发送异常: %s", e)


_sms_gateway = None

def _get_sms_gateway():
    """获取 SMS 网关实例 (延迟初始化, 按环境变量配置)

    环境变量:
      SMS_PROVIDER=twilio|alibaba|simulated
      Twilio: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER
      阿里云: SMS_AK / SMS_SK / SMS_SIGN_NAME / SMS_TEMPLATE_CODE
    """
    global _sms_gateway
    if _sms_gateway is not None:
        return _sms_gateway

    from core.sms_gateway import SimulatedSMSGateway, AlibabaSMSGateway, TwilioSMSGateway
    import os

    provider = os.environ.get("SMS_PROVIDER", "simulated").lower()
    if provider == "twilio":
        sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        from_num = os.environ.get("TWILIO_FROM_NUMBER", "")
        if sid and token and from_num:
            _sms_gateway = TwilioSMSGateway(
                account_sid=sid,
                auth_token=token,
                from_number=from_num,
            )
            logger.info("SMS 网关: Twilio")
        else:
            logger.warning("SMS_PROVIDER=twilio 但缺少凭据，降级为模拟模式")
            _sms_gateway = SimulatedSMSGateway(rate_limit_per_hour=30)
    elif provider == "alibaba":
        _sms_gateway = AlibabaSMSGateway(
            access_key=os.environ.get("SMS_AK", ""),
            access_secret=os.environ.get("SMS_SK", ""),
            sign_name=os.environ.get("SMS_SIGN_NAME", "无人机监测"),
            template_code=os.environ.get("SMS_TEMPLATE_CODE", ""),
        )
        logger.info("SMS 网关: 阿里云")
    else:
        _sms_gateway = SimulatedSMSGateway(rate_limit_per_hour=30)
        logger.info("SMS 网关: 模拟模式 (日志输出)")
    return _sms_gateway


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

        upsert_device(device_name)

        if drone_id:
            upsert_drone(device_name, drone_id, lat, lon, alt)
            if distance is not None:
                update_drone_status(device_name, drone_id, distance,
                                    line_name, status)
            add_drone_position(drone_id, device_name, lat, lon, alt,
                               distance, line_name)
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

        upsert_device(device_name)

        # ── 白名单检查 ──
        if is_drone_whitelisted(drone_id):
            logger.info("Whitelisted drone %s: alert suppressed", drone_id)
            # 仍然更新无人机位置
            if drone_id:
                upsert_drone(device_name, drone_id, lat, lon, alt)
                update_drone_status(device_name, drone_id, distance, line_name, level)
                add_drone_position(drone_id, device_name, lat, lon, alt, distance, line_name)
                socketio.emit('drone_update', {
                    'drone_id': drone_id, 'lat': lat, 'lon': lon, 'alt': alt,
                    'distance': distance or 0, 'nearest_line': line_name, 'status': level,
                })
            return jsonify({"status": "ok", "whitelisted": True})

        # ── 告警去重 ──
        _cooldown_key = (drone_id, level)
        _now = _time.time()
        _last = _alert_cooldown.get(_cooldown_key, 0)
        _cooldown_sec = _get_cooldown_sec(level)
        if _now - _last >= _cooldown_sec:
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
                add_drone_position(drone_id, device_name, lat, lon, alt, distance, line_name)

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
                add_drone_position(drone_id, device_name, lat, lon, alt, distance, line_name)
                socketio.emit('drone_update', {
                    'drone_id': drone_id, 'lat': lat, 'lon': lon, 'alt': alt,
                    'distance': distance or 0, 'nearest_line': line_name, 'status': level,
                })

        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error("report_alert error: %s", e)
        return jsonify({"error": "服务器内部错误"}), 500
