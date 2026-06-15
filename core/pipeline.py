"""
RID 数据处理管道 — 封装从 RID 广播到告警的完整处理链路

所有入口 (CLI / GUI / Web) 共享同一个 Pipeline，消除重复的业务逻辑。
"""

from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from storage.database import Database
from core.powerline import PowerLineManager, PowerLineSegment
from core.alert import AlertSystem
from core.trajectory import TrajectoryRecorder
from core.parser import ParsedRID, get_active_protocol

if TYPE_CHECKING:
    from core.backhaul import BackhaulManager
    from core.raw_archive import RawArchiveManager
    from core.pilot_notify import PilotNotifier
    from core.airspace import CompositeAirspaceSource


@dataclass
class PipelineResult:
    """单次 RID 处理的结果，供上游 (UI/日志) 使用"""
    drone_id: str
    latitude: float
    longitude: float
    altitude: float
    nearest_line: Optional[PowerLineSegment]
    distance: float
    status: str           # active / warning / severe / critical
    alert_level: Optional[str]   # 触发的告警级别 (被去重则为 None)


class RIDPipeline:
    """RID 数据处理管道 — 所有入口共享的核心"""

    def __init__(
        self,
        db: Database,
        pl_manager: PowerLineManager,
        alert_system: AlertSystem,
        trajectory_recorder: TrajectoryRecorder,
        thresholds: dict,
        backhaul: "BackhaulManager" = None,
        raw_archive: "RawArchiveManager" = None,
        airspace_manager: "CompositeAirspaceSource" = None,
        pilot_notifier: "PilotNotifier" = None,
    ):
        self.db = db
        self.pl_manager = pl_manager
        self.alert = alert_system
        self.trajectory = trajectory_recorder
        self.thresholds = thresholds
        self.backhaul = backhaul
        self.raw_archive = raw_archive
        self.airspace_manager = airspace_manager
        self.pilot_notifier = pilot_notifier

    def process(self, parsed: ParsedRID) -> Optional[PipelineResult]:
        """
        处理一条 RID 广播数据

        0. 存档原始报文 (防篡改证据留存)
        1. 更新数据库无人机状态
        2. 计算与最近电力线的垂直距离
        3. 判断告警级别并触发告警/短信
        4. 记录/停止轨迹
        5. 飞手推送 + 数据回传

        返回 PipelineResult 或 None (数据无效/无电力线)
        """
        # 0. 存档原始报文 (先存档后解析, 确保不丢失数据)
        if self.raw_archive and parsed.raw_data:
            self.raw_archive.archive(
                drone_id=parsed.drone_id or "unknown",
                raw_data=parsed.raw_data,
                protocol=get_active_protocol().name,
                msg_type="pack",
                mac=parsed.mac_address,
                rssi=parsed.rssi,
            )

        drone_id = parsed.drone_id
        if not drone_id or not parsed.location:
            return None

        loc = parsed.location

        # 1. 更新数据库
        self.db.upsert_drone(
            drone_id=drone_id,
            lat=loc.latitude,
            lon=loc.longitude,
            alt=loc.altitude_geodetic,
            speed=loc.speed_horizontal,
        )

        # 2. 计算最近电力线距离
        nearest_line, distance = self.pl_manager.find_nearest_line(
            loc.latitude, loc.longitude, loc.altitude_geodetic
        )

        if nearest_line is None:
            return None

        # 3. 判断状态
        status = "active"
        if distance <= self.thresholds.get("critical", 50):
            status = "critical"
        elif distance <= self.thresholds.get("severe", 100):
            status = "severe"
        elif distance <= self.thresholds.get("warning", 200):
            status = "warning"

        self.db.update_drone_distance(
            drone_id=drone_id,
            distance=distance,
            line_id=nearest_line.line_id,
            status=status,
        )

        # 4. 告警 + 轨迹
        alert_level = None
        if distance <= self.thresholds.get("warning", 200):
            alert_level = self.alert.process(
                drone_id=drone_id,
                distance=distance,
                line_name=nearest_line.name,
                line_id=nearest_line.line_id,
                drone_alt=loc.altitude_geodetic,
                drone_lat=loc.latitude,
                drone_lon=loc.longitude,
            )
            self.trajectory.record(
                drone_id=drone_id,
                lat=loc.latitude,
                lon=loc.longitude,
                alt=loc.altitude_geodetic,
                distance=distance,
                line_id=nearest_line.line_id,
            )
        else:
            self.trajectory.stop_tracking(drone_id)

        # 5. 飞手推送 (仅当告警触发时)
        if alert_level and self.pilot_notifier:
            action = "立即返航" if alert_level == "critical" else "请尽快离开禁飞区"
            self.pilot_notifier.notify(
                drone_id=drone_id,
                alert_level=alert_level,
                message=f"[{alert_level}] 无人机 {drone_id} 接近 {nearest_line.name} "
                        f"距离 {distance:.0f}m — {action}",
            )

        # 6. 数据回传 (4G/有线 → SMS → 北斗应急降级)
        if self.backhaul:
            self.backhaul.report_drone(
                drone_id=drone_id,
                lat=loc.latitude, lon=loc.longitude, alt=loc.altitude_geodetic,
                distance=distance, line_name=nearest_line.name, status=status,
            )
            if alert_level:
                channel = self.backhaul.report_alert(
                    drone_id=drone_id, level=alert_level, distance=distance,
                    line_name=nearest_line.name,
                    lat=loc.latitude, lon=loc.longitude, alt=loc.altitude_geodetic,
                )

        return PipelineResult(
            drone_id=drone_id,
            latitude=loc.latitude,
            longitude=loc.longitude,
            altitude=loc.altitude_geodetic,
            nearest_line=nearest_line,
            distance=distance,
            status=status,
            alert_level=alert_level,
        )
