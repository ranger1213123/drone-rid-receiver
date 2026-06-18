"""
边缘服务 A: 信号接收 (drone-receiver.service)

职责: BLE/WiFi 信号捕获 → 写入 raw_packets 表
输出: raw_packets 表 (SQLite)
依赖: bleak (BLE) / scapy (WiFi), storage/database.py

用法:
  python -m app.edge_receiver --config /etc/drone-rid/config.yaml --mode ble
"""

import argparse
import os
import time

from core.service_common import (
    setup_syspath, load_edge_config, init_receiver_database,
    setup_signal_handlers,
)

setup_syspath()
from logging_config import get_logger

logger = get_logger("receiver")


def parse_args():
    p = argparse.ArgumentParser(description="Drone RID 边缘接收服务")
    p.add_argument("--config", default="/etc/drone-rid/config.yaml")
    p.add_argument("--mode", default="ble", choices=["ble", "wifi", "auto"])
    p.add_argument("--interface", default=None, help="WiFi 网卡名 (mode=wifi)")
    return p.parse_args()


def main():
    args = parse_args()
    config = load_edge_config(args.config)
    db = init_receiver_database(config, args.config)

    # 初始化接收器
    from core.parser import create_receiver
    from core.parser.types import ReceiverType

    receiver_type = {"ble": ReceiverType.BLE, "wifi": ReceiverType.WIFI, "auto": ReceiverType.AUTO}
    receiver = create_receiver(config, receiver_type=receiver_type[args.mode],
                               interface=args.interface)

    def on_raw_packet(packet_bytes, rssi=None, timestamp=None):
        try:
            source = args.mode if args.mode != "auto" else (
                "wifi" if receiver_type == ReceiverType.WIFI else "ble")
            db.insert_raw_packet(
                payload=packet_bytes if isinstance(packet_bytes, bytes) else str(packet_bytes).encode(),
                rssi=rssi,
                source_type=source,
            )
        except Exception as e:
            logger.error("raw_packet 写入失败: %s", e)

    receiver.set_callback("on_message", lambda msg: on_raw_packet(
        getattr(msg, 'raw_data', b''),
        getattr(msg, 'rssi', None),
    ))

    # GPS 回调 (如果有)
    if hasattr(receiver, 'set_callback'):
        try:
            receiver.set_callback("on_gps", lambda lat, lon, alt: None)
        except Exception:
            pass

    receiver.start()
    logger.info("接收服务已启动 | mode=%s", args.mode)

    running = setup_signal_handlers()
    while running[0]:
        time.sleep(1)

    receiver.stop()
    db.close()
    logger.info("接收服务已停止")


if __name__ == "__main__":
    main()
