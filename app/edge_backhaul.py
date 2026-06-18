"""
边缘服务 C: 数据回传 (drone-backhaul.service)

职责: MQTT 连接 → outbox drain → 上行发布 → 下行配置监听
输入: outbox 表 (pending 消息) + MQTT 下行 topic
输出: MQTT broker + 电力线本地更新
依赖: core/mqtt_client.py, core/backhaul.py, core/beidou.py

用法:
  python -m app.edge_backhaul --config /etc/drone-rid/config.yaml
"""

import argparse
import json
import time

from core.service_common import (
    setup_syspath, load_edge_config, init_edge_database,
    setup_signal_handlers, get_device_name,
)

setup_syspath()
from logging_config import get_logger
from core.beidou import create_beidou
from core.backhaul import BackhaulManager

logger = get_logger("backhaul")


def parse_args():
    p = argparse.ArgumentParser(description="Drone RID 边缘回传服务")
    p.add_argument("--config", default="/etc/drone-rid/config.yaml")
    return p.parse_args()


def main():
    args = parse_args()
    config = load_edge_config(args.config)
    db = init_edge_database(config, args.config)
    device_name = get_device_name(config)

    # MQTT channel
    mqtt_channel = None
    mqtt_cfg = config.get("mqtt", {})

    def on_config(payload: dict):
        """云端推送电力线配置 → 更新本地 SQLite + PowerLineManager"""
        lines = payload.get("lines", [])
        version = payload.get("version", "")
        if lines:
            db.load_power_lines(lines)
            logger.info("电力线配置已更新: %d 条 (version=%s)", len(lines), version)
            if version:
                db.set_config_version(version)

    def on_broadcast(payload: dict):
        """处理云端广播命令"""
        cmd = payload.get("command", "")
        logger.info("收到广播命令: %s", cmd)

    if mqtt_cfg.get("enabled", False):
        from core.mqtt_client import MqttChannel
        broker = mqtt_cfg.get("broker", {})
        tls_cfg = mqtt_cfg.get("tls", {})

        def _get_config_version():
            return db.get_config_version()

        mqtt_channel = MqttChannel(
            broker_host=broker.get("host", "localhost"),
            broker_port=broker.get("port", 8883),
            device_name=device_name,
            ca_cert_path=tls_cfg.get("ca_cert", ""),
            client_cert_path=tls_cfg.get("client_cert", ""),
            client_key_path=tls_cfg.get("client_key", ""),
            keepalive=broker.get("keepalive", 60),
            reconnect_delay_min=broker.get("reconnect_delay_min", 1),
            reconnect_delay_max=broker.get("reconnect_delay_max", 120),
            on_config=on_config,
            on_broadcast=on_broadcast,
            get_config_version=_get_config_version,
        )
        logger.info("MQTT channel 已创建: %s:%d", broker.get("host"), broker.get("port"))

    # 北斗
    beidou = create_beidou(config)

    # 回传管理器
    backhaul = BackhaulManager(
        config, beidou, db,
        device_name=device_name,
        mqtt_channel=mqtt_channel,
    )

    backhaul.start()

    # 心跳变量
    heartbeat_interval = config.get("backhaul", {}).get("http", {}).get("heartbeat_interval", 30)
    last_heartbeat = 0

    running = setup_signal_handlers(stop_callback=backhaul.stop)
    logger.info("回传服务已启动 | device=%s | mqtt=%s",
                device_name, "enabled" if mqtt_channel else "disabled")

    while running[0]:
        now = time.time()

        # outbox drain (MQTT 在线时)
        backhaul.flush_if_needed()

        # 心跳
        if now - last_heartbeat >= heartbeat_interval:
            backhaul.send_heartbeat()
            last_heartbeat = now

        time.sleep(1)

    backhaul.stop()
    db.close()
    logger.info("回传服务已停止")


if __name__ == "__main__":
    main()
