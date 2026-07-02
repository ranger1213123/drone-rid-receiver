"""
SMS 短信网关 — 向预设专责人员发送告警短信

注意: 根据需求修正，短信仅发给专责人员（不直接发给飞手手机号）。
"""

import time
from abc import ABC, abstractmethod
from typing import List, Dict

from logging_config import get_logger

logger = get_logger(__name__)


class SMSGateway(ABC):
    @abstractmethod
    def send(self, phone_numbers: List[str], message: str,
             template_id: str = "") -> bool:
        ...


class SimulatedSMSGateway(SMSGateway):
    """模拟短信网关 — 日志输出 + 内存速率限制"""

    def __init__(self, rate_limit_per_hour: int = 10):
        self.rate_limit = rate_limit_per_hour
        self._sent: Dict[str, List[float]] = {}

    def send(self, phone_numbers: List[str], message: str,
             template_id: str = "") -> bool:
        ok = True
        for phone in phone_numbers:
            if not self._check_rate_limit(phone):
                logger.warning("[SMS模拟] 速率限制: %s (跳过)", phone)
                ok = False
                continue
            logger.info("[SMS模拟] → %s: %s", phone, message[:80])
        return ok

    def _check_rate_limit(self, phone: str) -> bool:
        now = time.time()
        timestamps = [t for t in self._sent.get(phone, [])
                      if now - t < 3600]
        if len(timestamps) >= self.rate_limit:
            return False
        timestamps.append(now)
        self._sent[phone] = timestamps
        return True


class TwilioSMSGateway(SMSGateway):
    """Twilio 短信网关 — 国际短信，支持中国大陆"""

    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number

    def send(self, phone_numbers: list, message: str,
             template_id: str = "") -> bool:
        try:
            from twilio.rest import Client
        except ImportError:
            logger.warning("twilio SDK 未安装，降级为模拟模式")
            sim = SimulatedSMSGateway()
            return sim.send(phone_numbers, message, template_id)

        client = Client(self.account_sid, self.auth_token)
        ok = True
        for phone in phone_numbers:
            # Twilio 要求中国大陆号码带 +86 前缀
            to = phone if phone.startswith("+") else f"+86{phone}"
            try:
                client.messages.create(body=message, from_=self.from_number, to=to)
                logger.info("[Twilio短信] 发送成功: %s", phone)
            except Exception as e:
                logger.error("[Twilio短信] 发送失败: %s -> %s", phone, e)
                ok = False
        return ok


class AlibabaSMSGateway(SMSGateway):
    """阿里云短信网关 (alibabacloud-dysmsapi)"""

    def __init__(self, access_key: str, access_secret: str,
                 sign_name: str, template_code: str):
        self.access_key = access_key
        self.access_secret = access_secret
        self.sign_name = sign_name
        self.template_code = template_code

    def send(self, phone_numbers: List[str], message: str,
             template_id: str = "") -> bool:
        import json

        try:
            from alibabacloud_dysmsapi20170525.client import Client
            from alibabacloud_dysmsapi20170525 import models
            from alibabacloud_tea_openapi import models as open_api_models
        except ImportError:
            logger.warning("阿里云短信 SDK 未安装，降级为模拟模式")
            sim = SimulatedSMSGateway()
            return sim.send(phone_numbers, message, template_id)

        config = open_api_models.Config(
            access_key_id=self.access_key,
            access_key_secret=self.access_secret,
        )
        config.endpoint = "dysmsapi.aliyuncs.com"
        client = Client(config)

        template = template_id or self.template_code
        # 阿里云短信模板变量: ${msg} → 需在控制台创建模板 "无人机告警: ${msg}"
        template_param = json.dumps({"msg": message}, ensure_ascii=False)
        ok = True
        for phone in phone_numbers:
            try:
                req = models.SendSmsRequest(
                    phone_numbers=phone,
                    sign_name=self.sign_name,
                    template_code=template,
                    template_param=template_param,
                )
                client.send_sms(req)
                logger.info("[阿里云短信] 发送成功: %s", phone)
            except Exception as e:
                logger.error("[阿里云短信] 发送失败: %s -> %s", phone, e)
                ok = False
        return ok


def create_sms_gateway(config: dict = None) -> SMSGateway:
    """工厂函数 — 支持 twilio / alibaba / simulated"""
    if config:
        sms_cfg = config.get("backhaul", {}).get("sms", {})
        if not sms_cfg.get("enabled", False):
            return SimulatedSMSGateway(rate_limit_per_hour=10)
        provider = sms_cfg.get("provider", "simulated")
        if provider == "alibaba":
            ali_cfg = sms_cfg.get("alibaba", {})
            return AlibabaSMSGateway(
                access_key=ali_cfg.get("access_key", ""),
                access_secret=ali_cfg.get("access_secret", ""),
                sign_name=ali_cfg.get("sign_name", ""),
                template_code=ali_cfg.get("template_code", ""),
            )
        if provider == "twilio":
            tw_cfg = sms_cfg.get("twilio", {})
            return TwilioSMSGateway(
                account_sid=tw_cfg.get("account_sid", ""),
                auth_token=tw_cfg.get("auth_token", ""),
                from_number=tw_cfg.get("from_number", ""),
            )
        return SimulatedSMSGateway(rate_limit_per_hour=10)

    # 兜底: 返回模拟网关
    return SimulatedSMSGateway(rate_limit_per_hour=10)
