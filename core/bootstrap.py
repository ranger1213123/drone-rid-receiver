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
                   base_dir: Optional[str] = None) -> dict:
    """创建所有核心组件，返回 dict 供 Controller 使用。

    Args:
        config: 已加载的配置 dict (与 config_path 二选一)
        config_path: 配置文件路径 (与 config 二选一)
        base_dir: 解析相对路径的基准目录 (默认使用 config_path 所在目录)
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

    # ── 飞手推送 ──
    from core.pilot_notify import create_pilot_notifier
    pilot_notifier = create_pilot_notifier(config)

    # ── 北斗 + 数据回传 ──
    from core.beidou import create_beidou
    from core.backhaul import BackhaulManager, TokenManager
    beidou = create_beidou(config)
    device_name = config.get('backhaul', {}).get('device_name', 'NW-F1')

    token_manager = None
    auth_cfg = config.get('backhaul', {}).get('auth', {})
    if auth_cfg.get('enabled', False):
        token_url = auth_cfg.get('token_url', '')
        device_secret = auth_cfg.get('device_secret', '')
        if token_url and device_secret:
            token_manager = TokenManager(
                auth_url=token_url,
                device_name=device_name,
                device_secret=device_secret,
                expire_seconds=auth_cfg.get('expire_seconds', 86400),
            )

    backhaul = BackhaulManager(
        config, beidou, db,
        device_name=device_name,
        token_manager=token_manager,
    )

    # ── 数据处理管道 ──
    pipeline = RIDPipeline(
        db=db,
        pl_manager=pl_manager,
        alert_system=alert_system,
        trajectory_recorder=trajectory_recorder,
        thresholds=thresholds,
        raw_archive=raw_archive,
        pilot_notifier=pilot_notifier,
        backhaul=backhaul,
    )

    return {
        'config': config,
        'db': db,
        'pl_manager': pl_manager,
        'pl_file': pl_file,
        'alert_system': alert_system,
        'trajectory_recorder': trajectory_recorder,
        'raw_archive': raw_archive,
        'pilot_notifier': pilot_notifier,
        'beidou': beidou,
        'backhaul': backhaul,
        'pipeline': pipeline,
        'thresholds': thresholds,
    }
