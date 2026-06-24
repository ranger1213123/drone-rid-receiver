"""
云侧轻量告警处理器 — 无 SQLite 依赖, 输出 buffer 由 MQTT Consumer 批量写入

与 edge 的 core/alert.py AlertSystem 不同:
- 不依赖 storage/database.py 的 Database (SQLite)
- 告警不直接写库, 而是放入 buffer 供 consumer 批量 flush
- 仅做阈值判定 + 去重 + 冷却
"""

import threading
import time
from datetime import datetime, timezone
from typing import Optional


class CloudAlertProcessor:
    """云侧轻量告警处理器

    用法:
        proc = CloudAlertProcessor(thresholds={"warning":200,"severe":100,"critical":50})
        level = proc.process(drone_id="ABC", distance=45, line_name="杭富线",
                             line_id=1, drone_alt=100, drone_lat=30.0, drone_lon=120.0,
                             device_name="EXD001")
        # 定期 drain
        alerts = proc.drain_alerts()
    """

    def __init__(self, thresholds: dict = None, cooldown: float = 30.0):
        self.thresholds = thresholds or {
            "warning": 200, "severe": 100, "critical": 50,
        }
        self.cooldown = cooldown
        self._last_alert: dict = {}    # (drone_id, level) → timestamp
        self._drone_level: dict = {}   # drone_id → current level
        self._lock = threading.Lock()
        self._alert_buffer: list = []

    # ── 公共接口 ──

    def process(self, drone_id: str, distance: float, line_name: str,
                line_id: int, drone_alt: float, drone_lat: float,
                drone_lon: float, device_name: str) -> Optional[str]:
        """判定告警等级, 去重, 返回 alert_level 或 None"""
        level = self._classify(distance)
        if level is None:
            with self._lock:
                self._drone_level.pop(drone_id, None)
            return None

        with self._lock:
            # 去重: 同 drone + 同 level 在冷却期内不重复
            key = (drone_id, level)
            now = time.time()
            last = self._last_alert.get(key, 0)
            if now - last < self.cooldown:
                self._drone_level[drone_id] = level
                return None

            self._last_alert[key] = now
            self._drone_level[drone_id] = level

            self._alert_buffer.append({
                "device_name": device_name,
                "drone_id": drone_id,
                "level": level,
                "distance": distance,
                "line_name": line_name,
                "latitude": drone_lat,
                "longitude": drone_lon,
                "altitude": drone_alt,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": f"[{level}] {drone_id} 接近 {line_name} 距离{distance:.0f}m",
            })

        return level

    def drain_alerts(self) -> list:
        """取出所有待写入告警并清空 buffer (线程安全)"""
        with self._lock:
            alerts = self._alert_buffer[:]
            self._alert_buffer.clear()
            return alerts

    def cleanup_stale(self, active_drone_ids: set):
        """清理已不在线的无人机告警状态"""
        with self._lock:
            stale = set(self._drone_level) - active_drone_ids
            for did in stale:
                self._drone_level.pop(did, None)

    @property
    def drone_level(self) -> dict:
        with self._lock:
            return dict(self._drone_level)

    # ── 内部逻辑 ──

    def _classify(self, distance: float) -> Optional[str]:
        """根据距离判定告警等级 (复用 pipeline 的阈值逻辑)"""
        if distance <= self.thresholds.get("critical", 50):
            return "critical"
        elif distance <= self.thresholds.get("severe", 100):
            return "severe"
        elif distance <= self.thresholds.get("warning", 200):
            return "warning"
        return None
