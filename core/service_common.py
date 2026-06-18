"""
边缘三服务的共享工具 — 配置加载 / DB 初始化 / 信号处理 / 日志

receiver / pipeline / backhaul 三个独立 systemd 服务共享此模块。
"""

import os
import signal
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent.parent

from logging_config import get_logger

logger = get_logger(__name__)


def setup_syspath():
    """确保项目根目录在 sys.path 中"""
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))


def load_edge_config(config_path: str) -> dict:
    """加载边缘配置文件"""
    from core.config import load_config
    config_path = os.path.abspath(config_path)
    return load_config(config_path)


def _resolve_db_path(config: dict, config_path: str, key: str, fallback: str) -> str:
    """将配置中的相对数据库路径解析为绝对路径"""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
    db_path = config.get("database", {}).get(key, fallback)
    if not os.path.isabs(db_path):
        db_path = os.path.normpath(os.path.join(base_dir, db_path))
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return db_path


def init_edge_database(config: dict, config_path: str) -> "Database":
    """初始化边缘主 SQLite 数据库 (WAL 模式) — pipeline + backhaul 共享"""
    from storage.database import Database
    db_path = _resolve_db_path(config, config_path, "path", "data/drone_rid.db")
    db = Database(db_path)
    logger.info("主数据库已打开: %s", db_path)
    return db


def init_receiver_database(config: dict, config_path: str) -> "Database":
    """初始化接收器专用 SQLite 数据库 (receiver 独占写, pipeline 只读 raw_packets)"""
    from storage.database import Database
    db_path = _resolve_db_path(config, config_path, "receiver_path", "data/receiver.db")
    db = Database(db_path)
    logger.info("接收器数据库已打开: %s", db_path)
    return db


def setup_signal_handlers(stop_callback=None):
    """注册 SIGINT / SIGTERM 信号处理器，返回 running 标志"""
    running = [True]  # mutable container

    def _handler(signum, frame):
        sig_name = signal.Signals(signum).name
        logger.info("收到信号 %s, 准备退出...", sig_name)
        running[0] = False
        if stop_callback:
            stop_callback()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    return running


def get_device_name(config: dict) -> str:
    """从配置中提取设备名称"""
    return config.get("backhaul", {}).get("device_name", "NW-F1")


def get_base_dir(config_path: str) -> str:
    """返回配置文件所在目录 (用于解析相对路径)"""
    return os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
