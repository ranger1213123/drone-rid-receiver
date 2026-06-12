"""
轨迹记录模块 - 记录接近电力线的无人机飞行轨迹

仅在无人机距离最近的电力线 ≤200m 时记录轨迹点。
采用去重策略: 同一无人机在 min_interval 秒内不重复记录。
"""

import time
from datetime import datetime
from typing import Dict

from db import Database


class TrajectoryRecorder:
    """
    轨迹记录器

    追踪所有接近电力线的无人机 (距离 ≤200m)，
    将它们的飞行位置按时间序列记录到数据库。
    """

    def __init__(self, db: Database,
                 min_interval: float = 2.0,
                 max_points_per_drone: int = 1000):
        """
        min_interval: 同一无人机轨迹点的最小记录间隔 (秒)
        max_points_per_drone: 每个无人机最多保留的轨迹点数
        """
        self.db = db
        self.min_interval = min_interval
        self.max_points = max_points_per_drone

        # 内存中的最后记录时间: {drone_id: timestamp}
        self._last_record_time: Dict[str, float] = {}

        # 每个无人机的点数计数器 (从数据库初始化)
        self._point_counts: Dict[str, int] = {}

    def record(self, drone_id: str, lat: float, lon: float, alt: float,
               distance: float, line_id: int) -> bool:
        """
        记录一个轨迹点

        返回: True 表示已记录, False 表示被去重跳过
        """
        # 去重检查
        now = time.time()
        last = self._last_record_time.get(drone_id, 0)
        if now - last < self.min_interval:
            return False

        # 记录到数据库
        self.db.add_trajectory_point(
            drone_id=drone_id,
            lat=lat, lon=lon, alt=alt,
            distance=distance,
            line_id=line_id,
        )

        # 更新状态
        self._last_record_time[drone_id] = now
        count = self._point_counts.get(drone_id, 0) + 1
        self._point_counts[drone_id] = count

        # 检查是否需要裁剪旧数据
        if count >= self.max_points * 1.2:
            self._prune(drone_id)

        # 第一次开始记录时输出日志
        if count <= 2:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[轨迹] {ts} {drone_id} 进入监控区域 (距离={distance:.1f}m)")

        return True

    def _prune(self, drone_id: str):
        """裁剪过旧的轨迹点"""
        # 保留最近的 max_points 个点
        try:
            self.db.conn.execute("""
                DELETE FROM trajectories
                WHERE drone_id = ? AND id NOT IN (
                    SELECT id FROM trajectories
                    WHERE drone_id = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )
            """, (drone_id, drone_id, self.max_points))
            self.db.conn.commit()
            self._point_counts[drone_id] = self.max_points
            print(f"[轨迹] {drone_id} 轨迹已裁剪 (保留 {self.max_points} 点)")
        except Exception as e:
            print(f"[轨迹] 裁剪失败: {e}")

    def get_trajectory_summary(self, drone_id: str) -> str:
        """获取轨迹摘要信息"""
        points = self.db.get_trajectory(drone_id, limit=500)
        if not points:
            return f"{drone_id}: 无轨迹数据"

        first = points[-1]
        last = points[0]
        return (
            f"{drone_id}: {len(points)} 点, "
            f"{first['timestamp'][:19]} → {last['timestamp'][:19]}, "
            f"最近距离: {min(p['distance_to_line'] for p in points):.1f}m"
        )

    def stop_tracking(self, drone_id: str):
        """停止追踪某无人机 (已离开或距离恢复正常)"""
        if drone_id in self._last_record_time:
            del self._last_record_time[drone_id]
