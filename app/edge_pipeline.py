"""
边缘服务 B: 数据处理管道 (drone-pipeline.service)

职责: 读取 raw_packets → 协议解析 → 3D距离计算 → 告警判定 → 写入 outbox
输入: raw_packets 表 (未处理行)
输出: outbox 表 + 轨迹数据
依赖: core/parser/, core/powerline, core/alert, core/trajectory, core/pipeline

用法:
  python -m app.edge_pipeline --config /etc/drone-rid/config.yaml
"""

import argparse
import time

from core.service_common import (
    setup_syspath, load_edge_config, init_edge_database,
    init_receiver_database,
    setup_signal_handlers, get_device_name,
)

setup_syspath()
from logging_config import get_logger
from core.powerline import PowerLineManager
from core.alert import AlertSystem
from core.trajectory import TrajectoryRecorder
from core.pipeline import RIDPipeline
from core.parser import get_active_protocol

logger = get_logger("pipeline")


def parse_args():
    p = argparse.ArgumentParser(description="Drone RID 边缘管道服务")
    p.add_argument("--config", default="/etc/drone-rid/config.yaml")
    return p.parse_args()


def main():
    args = parse_args()
    config = load_edge_config(args.config)

    # 两个独立数据库: receiver.db (raw_packets) + main.db (outbox/drones/...)
    receiver_db = init_receiver_database(config, args.config)
    db = init_edge_database(config, args.config)
    device_name = get_device_name(config)

    # 电力线 — 从主数据库加载 (由 backhaul 服务写入)
    pl_manager = PowerLineManager()
    pl_dicts = db.get_power_lines()
    if pl_dicts:
        pl_manager.load_from_list(pl_dicts)
        logger.info("从主数据库加载 %d 条电力线", len(pl_dicts))
    else:
        logger.warning("本地无电力线数据，等待 backhaul 服务同步")

    # 告警参数
    thresholds = config.get("thresholds", {"warning": 200, "severe": 100, "critical": 50})
    af_cfg = config.get("anti_flapping", {})
    anti_flapping = None
    if af_cfg.get("enabled", False):
        from core.anti_flapping import AntiFlappingEngine
        anti_flapping = AntiFlappingEngine(
            debounce_in=af_cfg.get("debounce_in", 3),
            debounce_out=af_cfg.get("debounce_out", 10),
        )
    alert_system = AlertSystem(db=db, thresholds=thresholds, anti_flapping=anti_flapping)

    # 轨迹记录器
    traj_config = config.get("trajectory", {})
    trajectory = TrajectoryRecorder(
        db=db,
        min_interval=traj_config.get("min_interval", 2.0),
        max_points_per_drone=traj_config.get("max_points_per_drone", 1000),
    )

    # 管道
    pipeline = RIDPipeline(
        db=db, pl_manager=pl_manager,
        alert_system=alert_system,
        trajectory_recorder=trajectory,
        thresholds=thresholds,
        device_name=device_name,
    )

    # 协议初始化
    from core.parser import configure_protocol
    configure_protocol(config)

    logger.info("管道服务已启动 | device=%s", device_name)

    running = setup_signal_handlers()
    while running[0]:
        packets = receiver_db.get_unprocessed_packets(limit=50)
        for pkt in packets:
            try:
                payload = pkt["payload"]
                if isinstance(payload, bytes):
                    parsed = get_active_protocol().parse(payload)
                else:
                    parsed = get_active_protocol().parse(payload.encode() if isinstance(payload, str) else payload)

                if parsed:
                    pipeline.process(parsed)
                receiver_db.mark_packet_processed(pkt["id"])
            except Exception as e:
                logger.error("管道处理失败 (pkt=%d): %s", pkt["id"], e)
                receiver_db.mark_packet_processed(pkt["id"])

        if not packets:
            time.sleep(0.1)

    receiver_db.close()
    db.close()
    logger.info("管道服务已停止")


if __name__ == "__main__":
    main()
