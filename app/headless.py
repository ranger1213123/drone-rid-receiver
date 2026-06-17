"""
Headless 边缘设备入口 — 杆塔无头模式

无 Flask / GUI，仅:
  - BLE/WiFi 接收 → 解析 → 距离计算 → 告警 → HTTP 回传云服务器
  - GPS 自定位 + 心跳上报
  - 电力线配置从云服务器定时轮询同步

用法:
  python -m app.headless --config config/edge.yaml
"""

import argparse
import os
import signal
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from logging_config import get_logger
from core.config import load_config
from core.bootstrap import bootstrap_core
from core.parser import create_receiver
from core.parser.types import ReceiverType

logger = get_logger("headless")


def parse_args():
    p = argparse.ArgumentParser(description="Drone RID 边缘设备 (headless)")
    p.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    p.add_argument("--mode", default="ble", choices=["ble", "wifi", "auto"])
    p.add_argument("--interface", default=None, help="WiFi 网卡名 (mode=wifi 时)")
    return p.parse_args()


def main():
    args = parse_args()
    config_path = os.path.abspath(args.config)
    config = load_config(config_path)
    base_dir = os.path.dirname(os.path.dirname(config_path))

    # Headless 模式引导
    core = bootstrap_core(config, config_path=config_path, base_dir=base_dir, headless=True)

    db = core["db"]
    pl_manager = core["pl_manager"]
    alert_system = core["alert_system"]
    pipeline = core["pipeline"]
    backhaul = core["backhaul"]

    # 启动回传
    backhaul.start()

    # 首次电力线同步 (如有云端配置)
    if backhaul.primary_online:
        count = backhaul.fetch_power_lines()
        if count > 0:
            logger.info("初始电力线同步: %d 条", count)

    # 创建接收器
    mode_str = args.mode
    if mode_str == "auto":
        # 自动检测: 先尝试 BLE, 无适配器则 WiFi
        mode_str = "ble"
    recv_mode = {"ble": ReceiverType.BLE, "wifi": ReceiverType.WIFI}
    receiver = create_receiver(
        recv_mode.get(mode_str, ReceiverType.BLE),
        wifi_interface=args.interface,
    )

    # 事件回调闭包
    def on_message(msg):
        """收到 RID 报文 → 管线处理"""
        pipeline.process(msg)
        # 周期性回传
        backhaul.flush_if_needed()

    def on_gps(gps_data):
        """GPS 定位更新 → 更新设备位置"""
        backhaul.inject_gps(gps_data.lat, gps_data.lon, gps_data.alt)

    receiver.set_callback("on_message", on_message)
    receiver.set_callback("on_gps", on_gps)

    # 启动接收
    receiver.start()
    logger.info("Headless 边缘设备已启动 | 模式: %s | 设备: %s", mode_str, backhaul._device_name)

    # 优雅退出
    running = True

    def shutdown(sig, frame):
        nonlocal running
        running = False
        logger.info("收到信号 %s，正在关闭...", sig)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while running:
            time.sleep(1)
    finally:
        receiver.stop()
        backhaul.stop()
        db.close()
        logger.info("边缘设备已关闭")


if __name__ == "__main__":
    main()
