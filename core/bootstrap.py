"""
核心组件工厂 — RIDController / WebController 共享的初始化逻辑
"""

import os
from typing import Optional

from logging_config import get_logger
from core.config import load_config
from core.parser import configure_protocol
from storage.database import Database
from core.powerline import PowerLineManager
from core.alert import AlertSystem
from core.trajectory import TrajectoryRecorder
from core.pipeline import RIDPipeline

logger = get_logger(__name__)


def _resolve_path(path: str, base_dir: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(base_dir, path))


def bootstrap_core(config: Optional[dict] = None, *,
                   config_path: Optional[str] = None,
                   base_dir: Optional[str] = None,
                   headless: bool = False) -> dict:
    """创建所有核心组件，返回 dict 供 Controller 使用。

    Args:
        config: 已加载的配置 dict (与 config_path 二选一)
        config_path: 配置文件路径 (与 config 二选一)
        base_dir: 解析相对路径的基准目录 (默认使用 config_path 所在目录)
        headless: 边缘设备模式 — 跳过本地电力线加载，由 cloud 同步
    """
    if config is None:
        if config_path is None:
            raise ValueError("config 或 config_path 必须提供一个")
        if base_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
        config = load_config(config_path)
    elif base_dir is None and config_path is not None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
    elif base_dir is None:
        base_dir = os.getcwd()

    configure_protocol(config)

    # ── 数据库 ──
    db_path = config.get("database", {}).get("path", "data/drone_rid.db")
    db_path = _resolve_path(db_path, base_dir)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    db = Database(db_path)

    # ── 电力线 ──
    pl_manager = PowerLineManager()
    pl_file = None  # headless 模式下为 None, 由 cloud 同步
    if headless:
        logger.info("Headless 模式: 电力线管理器初始化为空，等待云端同步")
    else:
        pl_file = config.get("power_lines_file", "config/power_lines.yaml")
        pl_file = _resolve_path(pl_file, base_dir)
        count = pl_manager.load_from_yaml(pl_file)
        logger.info("已加载 %d 条电力线段", count)

        pl_dicts = [
            {
                "name": l.name, "lat1": l.lat1, "lon1": l.lon1, "alt1": l.alt1,
                "lat2": l.lat2, "lon2": l.lon2, "alt2": l.alt2,
                "id": l.line_id,
            }
            for l in pl_manager.lines
        ]
        db.load_power_lines(pl_dicts)

    # ── 告警阈值 ──
    thresholds = config.get("thresholds", {
        "warning": 200, "severe": 100, "critical": 50
    })

    # ── 告警系统 (含防抖) ──
    af_cfg = config.get("anti_flapping", {})
    anti_flapping = None
    if af_cfg.get("enabled", False):
        from core.anti_flapping import AntiFlappingEngine
        anti_flapping = AntiFlappingEngine(
            debounce_in=af_cfg.get("debounce_in", 3),
            debounce_out=af_cfg.get("debounce_out", 10),
        )
    alert_system = AlertSystem(
        db=db,
        thresholds=thresholds,
        anti_flapping=anti_flapping,
    )

    # ── 轨迹记录器 ──
    traj_config = config.get("trajectory", {})
    trajectory_recorder = TrajectoryRecorder(
        db=db,
        min_interval=traj_config.get("min_interval", 2.0),
        max_points_per_drone=traj_config.get("max_points_per_drone", 1000),
    )

    # ── 原始报文存档 ──
    raw_archive = None
    if config.get("raw_archive", {}).get("enabled", True):
        from core.raw_archive import RawArchiveManager
        arc_cfg = config.get("raw_archive", {})
        raw_archive = RawArchiveManager(
            db=db,
            retention_days=arc_cfg.get("retention_days", 30),
            cleanup_interval=arc_cfg.get("cleanup_interval", 86400),
        )
        raw_archive.start()

    # ── 数据回传 ──
    from core.backhaul import BackhaulManager
    device_name = config.get('backhaul', {}).get('device_name', 'NW-F1')

    # MQTT channel (mTLS 认证, 替代 HTTP + JWT)
    mqtt_channel = None
    mqtt_cfg = config.get('mqtt', {})
    if mqtt_cfg.get('enabled', False):
        from core.mqtt_client import MqttChannel
        broker = mqtt_cfg.get('broker', {})
        tls_cfg = mqtt_cfg.get('tls', {})
        tls_enabled = tls_cfg.get('enabled', False)
        mqtt_channel = MqttChannel(
            broker_host=broker.get('host', 'localhost'),
            broker_port=broker.get('port', 8883),
            device_name=device_name,
            ca_cert_path=tls_cfg.get('ca_cert', '') if tls_enabled else '',
            client_cert_path=tls_cfg.get('client_cert', '') if tls_enabled else '',
            client_key_path=tls_cfg.get('client_key', '') if tls_enabled else '',
            keepalive=broker.get('keepalive', 60),
            reconnect_delay_min=broker.get('reconnect_delay_min', 1),
            reconnect_delay_max=broker.get('reconnect_delay_max', 120),
            get_config_version=db.get_config_version if db else None,
        )
        logger.info("MQTT channel 已创建: %s:%d", broker.get('host'), broker.get('port'))

    backhaul = BackhaulManager(
        config, db,
        device_name=device_name,
        mqtt_channel=mqtt_channel,
        pl_manager=pl_manager,
    )

    # ── 数据处理管道 ──
    pipeline = RIDPipeline(
        db=db,
        pl_manager=pl_manager,
        alert_system=alert_system,
        trajectory_recorder=trajectory_recorder,
        thresholds=thresholds,
        device_name=device_name,
        raw_archive=raw_archive,
    )

    return {
        'config': config,
        'db': db,
        'pl_manager': pl_manager,
        'pl_file': pl_file if not headless else None,
        'alert_system': alert_system,
        'trajectory_recorder': trajectory_recorder,
        'raw_archive': raw_archive,
        'backhaul': backhaul,
        'pipeline': pipeline,
        'thresholds': thresholds,
    }
