"""
告警防抖引擎 — 防止无人机在禁飞区边界反复进出导致的重复告警

状态机:
  OUTSIDE → ENTERING(等待debounce_in秒) → INSIDE(触发告警)
  INSIDE  → LEAVING(等待debounce_out秒) → OUTSIDE(清除告警)
"""

import time
from enum import Enum, auto
from typing import Dict, Optional

from logging_config import get_logger

logger = get_logger(__name__)


class FlapState(Enum):
    OUTSIDE = auto()
    ENTERING = auto()
    INSIDE = auto()
    LEAVING = auto()


class AntiFlappingEngine:
    """告警防抖状态机 — 每架无人机独立跟踪"""

    def __init__(self, debounce_in: float = 3.0, debounce_out: float = 10.0):
        self.debounce_in = debounce_in
        self.debounce_out = debounce_out
        self._drones: Dict[str, dict] = {}

    def evaluate(self, drone_id: str, is_inside_zone: bool,
                 now: float = None) -> bool:
        """返回 True 表示应当触发告警, False 表示抑制"""
        if now is None:
            now = time.time()

        state = self._drones.get(drone_id)
        if state is None:
            state = {"state": FlapState.OUTSIDE, "entered": now}
            self._drones[drone_id] = state

        current = state["state"]

        if is_inside_zone:
            if current == FlapState.OUTSIDE:
                state["state"] = FlapState.ENTERING
                state["entered"] = now
                logger.debug("%s ENTERING 区域", drone_id)
                return False
            elif current == FlapState.ENTERING:
                if now - state["entered"] >= self.debounce_in:
                    state["state"] = FlapState.INSIDE
                    state["entered"] = now
                    logger.debug("%s → INSIDE (确认进入)", drone_id)
                    return True
                return False
            elif current == FlapState.LEAVING:
                state["state"] = FlapState.INSIDE
                state["entered"] = now
                logger.debug("%s 重新进入 → INSIDE", drone_id)
                return False
            else:  # INSIDE
                return False  # 已在区域内，不重复触发
        else:  # 不在区域内
            if current == FlapState.INSIDE:
                state["state"] = FlapState.LEAVING
                state["entered"] = now
                logger.debug("%s LEAVING 区域", drone_id)
                return False
            elif current == FlapState.LEAVING:
                if now - state["entered"] >= self.debounce_out:
                    state["state"] = FlapState.OUTSIDE
                    state["entered"] = now
                    logger.debug("%s → OUTSIDE (确认离开)", drone_id)
                    return False  # 清除状态但这不是"触发"告警
                return False
            elif current == FlapState.ENTERING:
                state["state"] = FlapState.OUTSIDE
                state["entered"] = now
                logger.debug("%s 离开 → 重置 OUTSIDE", drone_id)
                return False
            else:  # OUTSIDE
                return False

    def is_inside(self, drone_id: str) -> bool:
        s = self._drones.get(drone_id)
        return s is not None and s["state"] in (FlapState.INSIDE, FlapState.LEAVING)

    def clear(self, drone_id: str):
        self._drones.pop(drone_id, None)
