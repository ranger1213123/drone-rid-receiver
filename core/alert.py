"""
告警系统 - 阈值判断、去重、防抖、数据库记录

告警级别:
  warning  (≤200m): 开始记录轨迹
  severe   (≤100m): 严重警告
  critical (≤50m):  危险

去重: 同一无人机同一级别在冷却期内不重复记录
防抖: 防止无人机在边界反复进出导致的重复告警 (仅管理 ENTER/LEAVE)
持续: INSIDE 状态下由冷却机制定期重触发, 级别升级立即触发
"""

import time
from datetime import datetime
from typing import Dict, Optional, TYPE_CHECKING

from logging_config import get_logger
from storage.database import Database

if TYPE_CHECKING:
    from core.anti_flapping import AntiFlappingEngine

logger = get_logger(__name__)

LEVEL_RANK = {"warning": 1, "severe": 2, "critical": 3}


class AlertSystem:
    """
    告警系统 - 管理告警状态、阈值判断

    机制:
      防抖 (AntiFlapping): 仅管理进出转换 (ENTER/LEAVE), INSIDE 时放行
      冷却 (Cooldown):     同级别持续告警速率限制
      升级 (Escalation):   级别提高时绕过冷却, 立即触发
    """

    COOLDOWNS = {
        "warning":  120,
        "severe":   60,
        "critical": 30,
    }

    def __init__(self, db: Database,
                 thresholds: Dict[str, float],
                 anti_flapping: "AntiFlappingEngine" = None):
        self.db = db
        self.thresholds = thresholds
        self.anti_flapping = anti_flapping

        self._last_alert: Dict[tuple, float] = {}
        self._drone_level: Dict[str, str] = {}
        self._lock = __import__('threading').Lock()

    def get_level(self, distance: float) -> Optional[str]:
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
        """处理一次距离更新, 返回触发的告警级别 (被抑制则返回 None)"""
        level = self.get_level(distance)

        # 所有 _drone_level / _last_alert 的读写均在锁内, 防止多线程竞态
        with self._lock:
            return self._process_locked(
                drone_id, distance, line_name, line_id, drone_alt, drone_lat,
                drone_lon, level
            )

    def _process_locked(self, drone_id: str, distance: float, line_name: str,
                        line_id: int, drone_alt: float, drone_lat: float,
                        drone_lon: float, level: Optional[str]) -> Optional[str]:
        # ── 防抖: 仅管理进出转换 ──
        if self.anti_flapping:
            is_inside = level is not None
            should_fire = self.anti_flapping.evaluate(drone_id, is_inside, level)

            if not should_fire:
                # 抑制期间仍更新实时状态
                if is_inside:
                    self._drone_level[drone_id] = level
                elif drone_id in self._drone_level:
                    if not self.anti_flapping.is_inside(drone_id):
                        del self._drone_level[drone_id]
                return None

            # 防抖放行但 level 为 None: 确认离开, 清除状态
            if level is None:
                if drone_id in self._drone_level:
                    logger.info("%s 已离开告警区域 (距离=%.1fm)", drone_id, distance)
                    del self._drone_level[drone_id]
                return None

        else:
            # 无防抖引擎: 直接判断
            if level is None:
                if drone_id in self._drone_level:
                    logger.info("%s 已离开告警区域 (距离=%.1fm)", drone_id, distance)
                    del self._drone_level[drone_id]
                return None

        # ── 升级检测: 级别提高立即触发, 绕过冷却 ──
        old_level = self._drone_level.get(drone_id)
        is_escalation = (old_level is not None and level != old_level
                         and LEVEL_RANK.get(level, 0) > LEVEL_RANK.get(old_level, 0))

        # ── 冷却检查 (升级时绕过) ──
        if not is_escalation and not self._should_alert_locked(drone_id, level):
            self._drone_level[drone_id] = level
            return None

        # ── 触发告警 ──
        self._drone_level[drone_id] = level

        action = self._get_action(level)
        message = (
            f"[{level}] {drone_id} 接近电力线 {line_name}\n"
            f"距离: {distance:.1f}m\n"
            f"位置: {drone_lat:.5f}, {drone_lon:.5f}, 高度: {drone_alt:.1f}m\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"处置建议: {action}"
        )

        self.db.add_alert(
            drone_id=drone_id,
            level=level,
            distance=distance,
            line_id=line_id,
            message=message,
        )

        log_prefix = "[升级] " if is_escalation else ""
        log_fn = {"warning": logger.warning, "severe": logger.error, "critical": logger.critical}
        log_fn.get(level, logger.info)(
            "%s%s 距离 %s %.1fm [%s]", log_prefix, drone_id, line_name, distance, level
        )

        return level

    def _should_alert_locked(self, drone_id: str, level: str) -> bool:
        """冷却去重: 同 drone + 同 level 在冷却期内不重复 (调用方已持锁)"""
        key = (drone_id, level)
        now = time.time()
        cooldown = self.COOLDOWNS.get(level, 60)
        last_time = self._last_alert.get(key, 0)
        if now - last_time < cooldown:
            return False
        self._last_alert[key] = now
        return True

    @staticmethod
    def _get_action(level: str) -> str:
        if level == "critical":
            return "立即降落或返航，远离电力线"
        elif level == "severe":
            return "立即调整航向，远离电力线"
        return "注意飞行路径，保持与电力线的安全距离"

    def get_status_summary(self) -> str:
        with self._lock:
            if not self._drone_level:
                return "当前无告警中的无人机"
            level_names = {"warning": "[警告]", "severe": "[严重]", "critical": "[危险]"}
            lines = ["当前告警无人机:"]
            for drone_id, level in sorted(self._drone_level.items()):
                lines.append(f"  {drone_id}: {level_names.get(level, level)}")
            return "\n".join(lines)

    @property
    def drone_level(self) -> Dict[str, str]:
        """线程安全返回当前告警无人机快照"""
        with self._lock:
            return dict(self._drone_level)

    def cleanup_stale(self, active_drone_ids: set):
        """清理已离开区域且不再活跃的无人机状态 (由定期清理循环调用)"""
        now = time.time()
        with self._lock:
            stale_drones = [
                did for did in self._drone_level
                if did not in active_drone_ids
            ]
            for did in stale_drones:
                self._drone_level.pop(did, None)
            # 同时清理冷却记录
            stale_keys = [
                k for k in self._last_alert
                if k[0] not in active_drone_ids
                and now - self._last_alert[k] > 600
            ]
            for k in stale_keys:
                self._last_alert.pop(k, None)
        if self.anti_flapping:
            for did in stale_drones:
                self.anti_flapping.clear(did)
