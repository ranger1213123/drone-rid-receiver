"""
告警防抖引擎 — 防止无人机在禁飞区边界反复进出导致的重复告警

职责: 仅管理 ENTER/LEAVE 转换的去抖, 不参与持续告警的速率控制。
持续告警的速率由 AlertSystem 的冷却机制 (Cooldown) 负责。

状态机:
  OUTSIDE → ENTERING(等待debounce_in秒) → INSIDE(放行-首次触发)
  INSIDE  → LEAVING(等待debounce_out秒) → OUTSIDE(放行-清除)

INSIDE 状态下持续在区域内: 返回 True, 交由冷却机制处理重触发
INSIDE 状态下级别升级:     返回 True, 交由 AlertSystem 绕过冷却立即触发
"""

import time
from enum import Enum, auto
from typing import Dict, Optional

from logging_config import get_logger

logger = get_logger(__name__)

LEVEL_RANK = {"warning": 1, "severe": 2, "critical": 3}


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
                 level: Optional[str] = None, now: Optional[float] = None) -> bool:
        """返回 True 表示应当继续进入冷却检查, False 表示防抖抑制

        - OUTSIDE → ENTERING: 抑制, 等待 debounce_in 秒确认
        - ENTERING → INSIDE:  放行 (首次触发)
        - INSIDE (持续):      放行 (冷却机制接管重触发)
        - INSIDE (级别升级):  放行 (调用方应绕过冷却)
        - INSIDE → LEAVING:   抑制, 等待 debounce_out 秒确认
        - LEAVING → OUTSIDE:  放行 (清除告警)
        """
        if now is None:
            now = time.time()

        state = self._drones.get(drone_id)
        if state is None:
            state = {"state": FlapState.OUTSIDE, "entered": now, "level": None}
            self._drones[drone_id] = state

        current = state["state"]
        old_level = state.get("level")

        if is_inside_zone:
            if current == FlapState.OUTSIDE:
                state["state"] = FlapState.ENTERING
                state["entered"] = now
                state["level"] = level
                logger.debug("%s ENTERING 区域 (level=%s)", drone_id, level)
                return False

            elif current == FlapState.ENTERING:
                if now - state["entered"] >= self.debounce_in:
                    state["state"] = FlapState.INSIDE
                    state["entered"] = now
                    state["level"] = level
                    logger.debug("%s → INSIDE (确认进入, level=%s)", drone_id, level)
                    return True
                # 级别变化但还在 debounce 期间, 更新级别即可
                if level and level != old_level:
                    state["level"] = level
                return False

            elif current == FlapState.LEAVING:
                # 退出期间重新进入, 回到 INSIDE (不发告警, 不重置冷却)
                state["state"] = FlapState.INSIDE
                state["entered"] = now
                state["level"] = level
                logger.debug("%s 重新进入 → INSIDE (level=%s)", drone_id, level)
                return False

            else:  # INSIDE
                # 持续在区域内: 放行, 让冷却机制决定是否重触发
                if level and old_level and LEVEL_RANK.get(level, 0) > LEVEL_RANK.get(old_level, 0):
                    logger.info("%s 级别升级: %s → %s", drone_id, old_level, level)
                state["level"] = level
                return True

        else:  # 不在告警区域内
            if current == FlapState.INSIDE:
                state["state"] = FlapState.LEAVING
                state["entered"] = now
                state["level"] = None
                logger.debug("%s LEAVING 区域", drone_id)
                return False

            elif current == FlapState.LEAVING:
                if now - state["entered"] >= self.debounce_out:
                    state["state"] = FlapState.OUTSIDE
                    state["entered"] = now
                    state["level"] = None
                    logger.debug("%s → OUTSIDE (确认离开)", drone_id)
                    return True  # 放行以清除告警状态
                return False

            elif current == FlapState.ENTERING:
                state["state"] = FlapState.OUTSIDE
                state["entered"] = now
                state["level"] = None
                logger.debug("%s 离开 → 重置 OUTSIDE", drone_id)
                return False

            else:  # OUTSIDE
                return False

    def is_inside(self, drone_id: str) -> bool:
        s = self._drones.get(drone_id)
        return s is not None and s["state"] in (FlapState.INSIDE, FlapState.LEAVING)

    def clear(self, drone_id: str):
        self._drones.pop(drone_id, None)
