"""
RID 数据处理管道 — 封装从 RID 广播到告警的完整处理链路

所有入口 (CLI / GUI / Web) 共享同一个 Pipeline，消除重复的业务逻辑。
"""

import json
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from logging_config import get_logger
from storage.database import Database
from core.powerline import PowerLineManager, PowerLineSegment
from core.alert import AlertSystem
from core.trajectory import TrajectoryRecorder
from core.parser import ParsedRID, get_active_protocol

logger = get_logger(__name__)

if TYPE_CHECKING:
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
        device_name: str = "",
        raw_archive: "RawArchiveManager" = None,
        airspace_manager: "CompositeAirspaceSource" = None,
        pilot_notifier: "PilotNotifier" = None,
    ):
        self.db = db
        self.pl_manager = pl_manager
        self.alert = alert_system
        self.trajectory = trajectory_recorder
        self.thresholds = thresholds
        self.device_name = device_name
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

        # 提取机型 + 起飞位
        ua_type = parsed.basic_id.ua_type if parsed.basic_id else 0
        takeoff_lat = parsed.takeoff_lat
        takeoff_lon = parsed.takeoff_lon
        op_lat = parsed.system.operator_lat if parsed.system else None
        op_lon = parsed.system.operator_lon if parsed.system else None

        # 1. 更新数据库
        self.db.upsert_drone(
            drone_id=drone_id,
            lat=loc.latitude,
            lon=loc.longitude,
            alt=loc.altitude_geodetic,
            speed=loc.speed_horizontal,
            heading=getattr(loc, 'track_angle', 0) or 0,
            ua_type=ua_type,
            takeoff_lat=takeoff_lat,
            takeoff_lon=takeoff_lon,
            operator_lat=op_lat if op_lat != 0 else None,
            operator_lon=op_lon if op_lon != 0 else None,
        )

        # 2. 计算最近电力线距离
        nearest_line, distance = self.pl_manager.find_nearest_line(
            loc.latitude, loc.longitude, loc.altitude_geodetic
        )

        if nearest_line is None:
            # 无电力线数据时不丢弃无人机位置 — 仍入库, 标记为 "unmonitored"
            self.db.upsert_drone(
                drone_id=drone_id,
                lat=loc.latitude,
                lon=loc.longitude,
                alt=loc.altitude_geodetic,
                speed=loc.speed_horizontal,
                heading=getattr(loc, 'track_angle', 0) or 0,
                ua_type=ua_type,
                takeoff_lat=takeoff_lat,
                takeoff_lon=takeoff_lon,
                operator_lat=op_lat if op_lat != 0 else None,
                operator_lon=op_lon if op_lon != 0 else None,
            )
            return PipelineResult(
                drone_id=drone_id,
                latitude=loc.latitude,
                longitude=loc.longitude,
                altitude=loc.altitude_geodetic,
                nearest_line=None,
                distance=-1,
                status="unmonitored",
                alert_level=None,
            )

        # 3. 判断状态
        status = "active"
        if distance <= self.thresholds.get("critical", 50):
            status = "critical"
        elif distance <= self.thresholds.get("severe", 100):
            status = "severe"
        elif distance <= self.thresholds.get("warning", 200):
            status = "warning"

        # 3a. 空域检查 (禁飞区 / 管制空域)
        in_airspace = None
        if self.airspace_manager:
            try:
                from core.airspace import check_airspace_violation
                zones = self.airspace_manager.fetch()
                if zones:
                    in_airspace = check_airspace_violation(
                        loc.latitude, loc.longitude, loc.altitude_geodetic, zones
                    )
                    if in_airspace:
                        logger.info("无人机 %s 位于空域 [%s] (%s)",
                                    drone_id, in_airspace.name, in_airspace.zone_type)
            except Exception as e:
                logger.warning("空域检查失败: %s", e)

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

        # 6. 数据回传 — 写入 outbox (由 backhaul 服务读取并通过 MQTT 上传)
        drone_model = parsed.drone_model
        if self.device_name:
            from datetime import datetime as dt_module
            now = dt_module.now().isoformat()
            report_payload = {
                "device": self.device_name,
                "drone_id": drone_id,
                "latitude": loc.latitude, "longitude": loc.longitude,
                "altitude": loc.altitude_geodetic,
                "distance_to_line": distance,
                "nearest_line": nearest_line.name, "status": status,
                "drone_model": drone_model,
                "ua_type": ua_type,
                "takeoff_lat": takeoff_lat, "takeoff_lon": takeoff_lon,
                "operator_lat": op_lat, "operator_lon": op_lon,
                "timestamp": now,
            }
            self.db.insert_outbox(report_payload, "/api/report", topic_suffix="report")
            if alert_level:
                alert_payload = {
                    "device": self.device_name,
                    "type": "alert",
                    "drone_id": drone_id, "level": alert_level,
                    "distance": distance, "nearest_line": nearest_line.name,
                    "latitude": loc.latitude, "longitude": loc.longitude,
                    "altitude": loc.altitude_geodetic,
                    "drone_model": drone_model,
                    "takeoff_lat": takeoff_lat, "takeoff_lon": takeoff_lon,
                    "timestamp": now,
                }
                self.db.insert_outbox(alert_payload, "/api/report_alert",
                                      topic_suffix="alert", priority=1)

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
