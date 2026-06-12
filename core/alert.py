"""
告警系统 - 阈值判断、去重、数据库记录

告警级别:
  warning  (≤200m): 开始记录轨迹
  severe   (≤100m): 严重警告
  critical (≤50m):  危险

去重: 同一无人机同一级别在冷却期内不重复记录
"""

import time
from datetime import datetime
from typing import Dict, Optional

from logging_config import get_logger
from storage.database import Database

logger = get_logger(__name__)


class AlertSystem:
    """
    告警系统 - 管理告警状态、阈值判断

    去重策略: 同一无人机同一告警级别，在冷却期内不重复触发。
    - warning:  冷却 120 秒
    - severe:   冷却 60 秒
    - critical: 冷却 30 秒
    """

    COOLDOWNS = {
        "warning":  120,
        "severe":   60,
        "critical": 30,
    }

    def __init__(self, db: Database,
                 thresholds: Dict[str, float]):
        """
        thresholds: {"warning": 200, "severe": 100, "critical": 50}
        """
        self.db = db
        self.thresholds = thresholds

        self._last_alert: Dict[tuple, float] = {}
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
            if drone_id in self._drone_level:
                logger.info("%s 已离开告警区域 (距离=%.1fm)", drone_id, distance)
                del self._drone_level[drone_id]
            return None

        if not self._should_alert(drone_id, level):
            return None

        old_level = self._drone_level.get(drone_id)
        self._drone_level[drone_id] = level

        message = (
            f"[{level}] {drone_id} 接近电力线 {line_name}\n"
            f"距离: {distance:.1f}m\n"
            f"位置: {drone_lat:.5f}, {drone_lon:.5f}, 高度: {drone_alt:.1f}m\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        self.db.add_alert(
            drone_id=drone_id,
            level=level,
            distance=distance,
            line_id=line_id,
            message=message,
        )

        log_fn = {"warning": logger.warning, "severe": logger.error, "critical": logger.critical}
        log_fn.get(level, logger.info)(
            "%s 距离 %s %.1fm [%s]", drone_id, line_name, distance, level
        )

        return level

    def _should_alert(self, drone_id: str, level: str) -> bool:
        """检查是否应触发告警 (去重)"""
        key = (drone_id, level)
        now = time.time()
        cooldown = self.COOLDOWNS.get(level, 60)

        last_time = self._last_alert.get(key, 0)
        if now - last_time < cooldown:
            return False

        self._last_alert[key] = now
        return True

    def get_status_summary(self) -> str:
        """获取当前告警状态摘要字符串"""
        if not self._drone_level:
            return "当前无告警中的无人机"
        level_names = {"warning": "[警告]", "severe": "[严重]", "critical": "[危险]"}
        lines = ["当前告警无人机:"]
        for drone_id, level in sorted(self._drone_level.items()):
            lines.append(f"  {drone_id}: {level_names.get(level, level)}")
        return "\n".join(lines)
