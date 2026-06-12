#!/usr/bin/env python3
"""
无人机 RID 接收与电力线防碰撞系统 — GUI 版

用法:
  python app/gui_launcher.py
  python app/gui_launcher.py --config config/config.yaml
"""

import os
import sys
from pathlib import Path

# 添加项目根目录到路径
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from logging_config import get_logger

logger = get_logger(__name__)

from core.config import load_config as load_yaml_config


def get_base_path():
    """获取资源根目录（兼容 PyInstaller 打包和源码运行）"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包：资源在临时解压目录
        return Path(sys._MEIPASS)
    else:
        # 源码运行：项目根目录 = app/ 的父目录
        return SCRIPT_DIR.parent


def find_config(explicit_path=None):
    """
    查找配置文件，优先级:
    1. 用户显式指定的路径
    2. EXE 同目录下的 config/config.yaml（用户可自行放置）
    3. 打包内置的 config/config.yaml（PyInstaller datas）
    4. 源码目录下的 config/config.yaml
    """
    if explicit_path:
        return Path(explicit_path)

    # 如果是从 EXE 运行，优先查找 EXE 同目录的 config（用户可覆盖）
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).parent
        user_config = exe_dir / 'config' / 'config.yaml'
        if user_config.exists():
            return user_config

    # 回退到内置/源码配置
    base = get_base_path()
    return base / 'config' / 'config.yaml'


# 项目根目录（用于非配置文件的资源路径）
PROJECT_ROOT = get_base_path()

# ── 依赖检查 ──
def _safe_exit():
    """安全退出：GUI 环境下 sys.stdin 可能不可用"""
    try:
        input("Press Enter to exit...")
    except (RuntimeError, EOFError):
        pass
    sys.exit(1)


def check_deps():
    """检查必要依赖，给出友好提示"""
    missing = []
    try:
        import yaml
    except ImportError:
        missing.append("pyyaml  (pip install pyyaml)")

    try:
        import tkinter
    except ImportError:
        missing.append(
            "tkinter (Python GUI library). "
            "Reinstall Python and check 'tcl/tk and IDLE' option."
        )

    if missing:
        logger.error("缺少必要依赖:")
        for m in missing:
            logger.error("  - %s", m)
        _safe_exit()


def main():
    check_deps()

    import argparse

    parser = argparse.ArgumentParser(
        description="Drone RID Receiver - GUI"
    )
    parser.add_argument("--config", "-c", default=None, help="Config file path")
    args = parser.parse_args()

    # 配置文件路径
    config_path = find_config(args.config)

    if not config_path.exists():
        logger.error("配置文件未找到: %s", config_path)
        logger.error("工作目录: %s", os.getcwd())
        logger.error("项目根目录: %s", PROJECT_ROOT)
        logger.error("  (PyInstaller frozen: %s)", getattr(sys, 'frozen', False))
        if getattr(sys, 'frozen', False):
            logger.error("  MEIPASS: %s", sys._MEIPASS)
            logger.error("  EXE dir: %s", Path(sys.executable).parent)
        _safe_exit()

    config = load_yaml_config(str(config_path))

    # 补充绝对路径（config 中的相对路径相对于项目根）
    if not os.path.isabs(config.get("database", {}).get("path", "")):
        db_path = config.get("database", {}).get("path", "data/drone_rid.db")
        config["database"]["path"] = str(PROJECT_ROOT / db_path)

    if not os.path.isabs(config.get("power_lines_file", "")):
        pl_file = config.get("power_lines_file", "config/power_lines.yaml")
        config["power_lines_file"] = str(PROJECT_ROOT / pl_file)

    from display.gui.window import MainWindow

    try:
        app = MainWindow(config)
        app.mainloop()
    except Exception as e:
        logger.exception("致命错误: %s", e)
        _safe_exit()


if __name__ == "__main__":
    main()
