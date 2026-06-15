"""
飞手端推送 — 通过 USS / UOM 平台向飞手 App/遥控器推送驱离告警

可行性分析:
  - DJI MSDK: 是移动端 SDK，用于开发自定义遥控器 App，不支持从外部系统向任意飞手推送
  - 可行路径: USS (U-Space Service) / UOM 平台飞行服务接口
  - 前提: 无人机需在 USS/UOM 平台注册，通过平台身份获取通信通道

中国接入方式 (MH/T 4053-2022):
  - UOM 平台: https://uom.caac.gov.cn
  - 注册获取 appId + appKey
  - 接口: HTTPS + JSON + MD5 签名
"""

import hashlib
import time
import json
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

import requests

from logging_config import get_logger

logger = get_logger(__name__)


def _generate_sign(app_key: str, timestamp: str, biz_content: str) -> str:
    raw = f"appKey={app_key}&timestamp={timestamp}&bizContent={biz_content}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


class PilotNotifier(ABC):
    """飞手通知抽象接口"""

    @abstractmethod
    def notify(self, drone_id: str, alert_level: str, message: str) -> bool:
        ...


class UOMFlightServiceNotifier(PilotNotifier):
    """
    UOM 平台飞行服务接口 (MH/T 4053-2022)

    流程:
      1. 在 https://uom.caac.gov.cn 注册单位账号
      2. 通过"系统接口申请"获取 appId + appKey
      3. 调用飞行服务相关接口推送告警/通知

    注意: 此接口仅对已在 UOM 注册的无人机有效。
          对未注册无人机，需通过其他渠道 (如 SMS 通知专责人员)。
    """

    def __init__(self, app_id: str, app_key: str,
                 base_url: str = "https://uom.caac.gov.cn/api"):
        self.app_id = app_id
        self.app_key = app_key
        self.base_url = base_url

    def notify(self, drone_id: str, alert_level: str, message: str) -> bool:
        if not self.app_id or self.app_id == "your_uom_app_id":
            logger.info("[UOM通知] appId 未配置, 跳过飞手推送")
            return False

        level_map = {"warning": "1", "severe": "2", "critical": "3"}
        biz_content = json.dumps({
            "uasID": drone_id,
            "alertLevel": level_map.get(alert_level, "1"),
            "message": message[:500],
            "action": "立即返航" if alert_level == "critical" else "请尽快离开禁飞区",
            "timestamp": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
        }, ensure_ascii=False)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

        payload = {
            "appId": self.app_id,
            "format": "JSON",
            "charset": "UTF-8",
            "signType": "md5",
            "sign": _generate_sign(self.app_key, ts, biz_content),
            "timestamp": ts,
            "version": "1.0",
            "bizContent": biz_content,
        }

        try:
            resp = requests.post(
                f"{self.base_url}/flight/alert",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 1:
                logger.info("[UOM通知] 推送成功: %s", drone_id)
                return True
            logger.warning("[UOM通知] 返回异常: %s", data.get("msg", ""))
            return False
        except Exception as e:
            logger.error("[UOM通知] 请求失败: %s", e)
            return False


class ConsolePilotNotifier(PilotNotifier):
    """本地日志通知 — 无 USS/UOM 接入时的降级方案

    将飞手推送内容记录到文件，由系统管理员手动处理。
    """

    def __init__(self, log_path: str = "data/pilot_notifications.log"):
        self.log_path = log_path

    def notify(self, drone_id: str, alert_level: str, message: str) -> bool:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        action = "立即返航" if alert_level == "critical" else "请尽快离开禁飞区"
        entry = f"[{ts}] [{alert_level.upper()}] {drone_id}: {message} → {action}\n"
        logger.info("[飞手通知] %s: %s → %s", drone_id, message[:60], action)
        try:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception as e:
            logger.error("飞手通知日志写入失败: %s", e)
        return True


def create_pilot_notifier(config: dict) -> PilotNotifier:
    """
    创建飞手通知器

    config['pilot_notify']['provider']:
      - 'uom':  UOM 平台飞行服务接口 (需要 appId + appKey)
      - 'console': 本地日志降级方案 (默认, 不需要外部凭据)
    """
    cfg = config.get("pilot_notify", {})
    if not cfg.get("enabled", False):
        return ConsolePilotNotifier()

    provider = cfg.get("provider", "console")
    if provider == "uom":
        uom_cfg = cfg.get("uom", {})
        app_id = uom_cfg.get("app_id", "") or os.environ.get("UOM_APP_ID", "")
        app_key = uom_cfg.get("app_key", "") or os.environ.get("UOM_APP_KEY", "")
        base_url = uom_cfg.get("base_url", "https://uom.caac.gov.cn/api")
        if not app_id or not app_key:
            logger.warning("UOM appId/appKey 未配置, 降级为本地日志通知")
            return ConsolePilotNotifier()
        return UOMFlightServiceNotifier(app_id, app_key, base_url)

    return ConsolePilotNotifier()
