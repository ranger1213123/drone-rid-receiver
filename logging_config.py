"""
统一日志模块 — 结构化日志，支持控制台 + 文件轮转

用法:
    from logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("系统启动")
    logger.warning("距离过近", extra={"drone_id": did, "distance": dist})
"""

import logging
import logging.handlers
import sys
import os
from pathlib import Path
from typing import Optional


# 日志格式
CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_root_logger: Optional[logging.Logger] = None


def setup_logging(
    level: str = "INFO",
    log_dir: str = "logs",
    console: bool = True,
    file_rotation: bool = True,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
) -> logging.Logger:
    """初始化全局日志配置"""
    global _root_logger

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    if console:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(CONSOLE_FORMAT, DATE_FORMAT))
        root.addHandler(handler)

    if file_rotation:
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "drone_rid.log")
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(FILE_FORMAT, DATE_FORMAT))
        root.addHandler(handler)

    # 抑制第三方库的 DEBUG 日志
    logging.getLogger("scapy").setLevel(logging.WARNING)
    logging.getLogger("bleak").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _root_logger = root
    return root


def get_logger(name: str) -> logging.Logger:
    """获取模块级 logger"""
    if _root_logger is None:
        setup_logging()
    return logging.getLogger(name)
