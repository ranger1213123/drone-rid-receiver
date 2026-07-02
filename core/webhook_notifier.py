"""
企业微信机器人 Webhook 通知模块 — 替代 SMS 短信网关

Webhook 地址在系统设置中配置: webhook_url
支持企微机器人 Markdown 格式，包含 @all 提醒
"""

import json
import threading
import time
import urllib.request
from typing import Optional

from logging_config import get_logger

logger = get_logger(__name__)

# 去重冷却: (station_name, drone_id, level) → timestamp
_cooldown: dict = {}
_cooldown_lock = threading.Lock()
COOLDOWN_SECONDS = 300


class WebhookNotifier:
    """企业微信机器人 Webhook 通知器"""

    def __init__(self, webhook_url: str = ""):
        self.webhook_url = webhook_url

    @property
    def available(self) -> bool:
        return bool(self.webhook_url)

    def send_alert(self, station_name: str, drone_id: str, level: str,
                   distance: float, line_name: str, lat: float = 0, lon: float = 0) -> bool:
        """发送告警通知到企业微信机器人"""
        if not self.webhook_url:
            return False

        cooldown_key = (station_name, drone_id, level)
        now = time.time()
        with _cooldown_lock:
            last = _cooldown.get(cooldown_key, 0)
            if now - last < COOLDOWN_SECONDS:
                return False
            _cooldown[cooldown_key] = now

        # 周期性清理过期冷却记录
        stale = [k for k, v in _cooldown.items() if now - v > COOLDOWN_SECONDS * 2]
        for k in stale:
            _cooldown.pop(k, None)

        level_label = {"warning": "⚠️ 警告", "severe": "🚨 严重", "critical": "🔴 危急"}.get(level, level)
        level_color = {"warning": "comment", "severe": "warning", "critical": "warning"}.get(level, "info")

        loc_str = f"> 位置: <font color=\"info\">{lat:.5f}, {lon:.5f}</font>\n" if lat or lon else ""

        markdown = (
            f"## {level_label}\n"
            f"> 无人机: <font color=\"comment\">{drone_id}</font>\n"
            f"> 距离: <font color=\"{level_color}\">{distance:.0f}m</font>\n"
            f"> 电力线: **{line_name}**\n"
            f"{loc_str}"
            f"> 站点: {station_name}\n"
            f"> 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"\n请立即处置 <@all>"
        )

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": markdown,
            },
        }

        try:
            req = urllib.request.Request(
                self.webhook_url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("errcode") == 0:
                logger.info("企微通知已发送: station=%s drone=%s level=%s",
                            station_name, drone_id, level)
                return True
            else:
                logger.warning("企微通知失败: %s", result.get("errmsg", "unknown"))
                return False
        except Exception as e:
            logger.error("企微通知发送异常: %s", e)
            return False


def create_webhook_notifier(webhook_url: str = "") -> Optional[WebhookNotifier]:
    """工厂函数: 创建 WebhookNotifier 实例"""
    if not webhook_url:
        logger.info("Webhook URL 未配置，企微通知禁用")
        return None
    return WebhookNotifier(webhook_url)
