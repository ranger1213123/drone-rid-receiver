"""
告警系统 - 阈值判断、短信发送、去重逻辑

告警级别:
  warning  (≤200m): 给飞手发"警告"短信 + 开始记录轨迹
  severe   (≤100m): 给飞手发"严重警告"短信
  critical (≤50m):  给飞手发"立即驱离"短信 + 给预设人员发短信

去重: 同一无人机同一级别在冷却期内不重复发送
"""

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from db import Database


# ─────────────────── SMS 后端 ───────────────────

class SMSBackend(ABC):
    """短信发送后端抽象基类"""

    @abstractmethod
    def send(self, phone: str, message: str) -> bool:
        """发送短信，返回是否成功"""
        ...


class MockSMSBackend(SMSBackend):
    """模拟短信后端 - 打印到控制台 (测试用)"""

    def send(self, phone: str, message: str) -> bool:
        now = datetime.now().strftime("%H:%M:%S")
        print(f"\n{'='*60}")
        print(f"[短信模拟] {now}")
        print(f"  收件人: {phone}")
        print(f"  内容:   {message}")
        print(f"{'='*60}\n")
        return True


class TwilioSMSBackend(SMSBackend):
    """Twilio 短信后端"""

    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number

    def send(self, phone: str, message: str) -> bool:
        try:
            from twilio.rest import Client
            client = Client(self.account_sid, self.auth_token)
            msg = client.messages.create(
                body=message,
                from_=self.from_number,
                to=phone,
            )
            print(f"[Twilio] 短信已发送: {msg.sid}")
            return True
        except Exception as e:
            print(f"[Twilio] 发送失败: {e}")
            return False


class AliyunSMSBackend(SMSBackend):
    """阿里云短信后端"""

    def __init__(self, access_key_id: str, access_key_secret: str,
                 sign_name: str, template_codes: Dict[str, str]):
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.sign_name = sign_name
        self.template_codes = template_codes

    def send(self, phone: str, message: str) -> bool:
        print(f"[阿里云短信] 功能需配置模板后启用: {phone}")
        # 实际使用时需要:
        # from alibabacloud_dysmsapi20170525.client import Client
        # 以及对应的 SendSmsRequest
        return False


# ─────────────────── 告警系统 ───────────────────

class AlertSystem:
    """
    告警系统 - 管理告警状态、阈值判断、短信发送

    去重策略: 同一无人机同一告警级别，在冷却期内不重复发送。
    - warning:  冷却 120 秒 (每 2 分钟最多发一次)
    - severe:   冷却 60 秒
    - critical: 冷却 30 秒
    """

    # 冷却时间 (秒)
    COOLDOWNS = {
        "warning":  120,
        "severe":   60,
        "critical": 30,
    }

    def __init__(self, db: Database, sms_backend: SMSBackend,
                 thresholds: Dict[str, float],
                 alert_contacts: List[Dict[str, str]],
                 pilot_phones: Dict[str, str]):
        """
        thresholds: {"warning": 200, "severe": 100, "critical": 50}
        alert_contacts: [{"name": "张三", "phone": "+8613800138000"}, ...]
        pilot_phones: {"drone_id": "phone", ...}
        """
        self.db = db
        self.sms = sms_backend
        self.thresholds = thresholds
        self.alert_contacts = alert_contacts
        self.pilot_phones = pilot_phones or {}

        # 内存中的去重状态: {(drone_id, level): last_alert_timestamp}
        self._last_alert: Dict[tuple, float] = {}

        # 内存中的级别状态追踪: {drone_id: current_level}
        self._drone_level: Dict[str, str] = {}

    def get_level(self, distance: float) -> Optional[str]:
        """
        根据距离判断告警级别
        返回: "critical" | "severe" | "warning" | None (无需告警)
        """
        if distance <= self.thresholds.get("critical", 50):
            return "critical"
        elif distance <= self.thresholds.get("severe", 100):
            return "severe"
        elif distance <= self.thresholds.get("warning", 200):
            return "warning"
        return None

    def process(self, drone_id: str, distance: float, line_name: str,
                line_id: int, drone_alt: float, drone_lat: float,
                drone_lon: float) -> Optional[str]:
        """
        处理一次距离更新

        返回: 触发的告警级别 (若被去重则返回 None)
        """
        level = self.get_level(distance)
        if level is None:
            # 无人机已离开告警区域
            if drone_id in self._drone_level:
                print(f"[告警] {drone_id} 已离开告警区域 (距离={distance:.1f}m)")
                del self._drone_level[drone_id]
            return None

        # 检查是否需要去重
        if not self._should_alert(drone_id, level):
            return None

        # 记录告警级别变化
        old_level = self._drone_level.get(drone_id)
        self._drone_level[drone_id] = level

        # 构造短信消息
        level_names = {"warning": "注意", "severe": "严重警告", "critical": "立即驱离"}
        level_name = level_names.get(level, level)

        message = (
            f"[{level_name}] 无人机 {drone_id} 接近电力线!\n"
            f"距离: {distance:.1f}m (阈值: {self.thresholds.get(level, '?')}m)\n"
            f"位置: {drone_lat:.5f}, {drone_lon:.5f}, 高度: {drone_alt:.1f}m\n"
            f"最近电力线: {line_name}\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        # 发送给飞手的短信
        sms_pilot_ok = False
        pilot_phone = self.pilot_phones.get(drone_id)
        if pilot_phone:
            pilot_msg = message + (
                "\n请立即调整航向远离电力线设施!" if level != "critical"
                else "\n请立即降落或远离! 已通知相关人员!"
            )
            sms_pilot_ok = self.sms.send(pilot_phone, pilot_msg)
        else:
            print(f"[告警] 未找到无人机 {drone_id} 的飞手号码映射")

        # 低于 50m (critical) 时额外通知预设人员
        sms_staff_ok = False
        if level == "critical":
            staff_msg = (
                f"[紧急] 无人机 {drone_id} 进入 50m 危险区域!\n"
                f"距离电力线 {line_name} 仅 {distance:.1f}m\n"
                f"位置: {drone_lat:.5f}, {drone_lon:.5f}\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"请立即响应!"
            )
            for contact in self.alert_contacts:
                try:
                    ok = self.sms.send(contact["phone"], staff_msg)
                    if ok:
                        sms_staff_ok = True
                except Exception as e:
                    print(f"[告警] 发送给 {contact['name']} 失败: {e}")

        # 记录告警到数据库
        self.db.add_alert(
            drone_id=drone_id,
            level=level,
            distance=distance,
            line_id=line_id,
            message=message,
            sms_pilot=sms_pilot_ok,
            sms_staff=sms_staff_ok,
        )

        # 控制台输出
        status_icons = {"warning": "⚠️", "severe": "🚨", "critical": "🔴"}
        icon = status_icons.get(level, "!")
        print(f"\n{icon} [告警:{level}] {drone_id} 距离 {line_name} {distance:.1f}m")

        return level

    def _should_alert(self, drone_id: str, level: str) -> bool:
        """检查是否应发送告警 (去重)"""
        key = (drone_id, level)
        now = time.time()
        cooldown = self.COOLDOWNS.get(level, 60)

        last_time = self._last_alert.get(key, 0)
        if now - last_time < cooldown:
            return False

        self._last_alert[key] = now
        return True

    def print_status_summary(self):
        """打印当前告警状态摘要"""
        if self._drone_level:
            print("\n--- 当前告警无人机 ---")
            for drone_id, level in sorted(self._drone_level.items()):
                level_names = {"warning": "⚠ 警告", "severe": "🚨 严重", "critical": "🔴 危险"}
                print(f"  {drone_id}: {level_names.get(level, level)}")
        else:
            print("当前无告警中的无人机")
