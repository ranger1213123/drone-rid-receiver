#!/usr/bin/env python3
"""
无人机 RID 接收与电力线防碰撞系统 — 主程序

功能:
  - 接收无人机 Remote ID (RID) 广播 (BLE 或 WiFi)
  - 实时显示无人机位置
  - 计算与电力线的垂直距离
  - 三级告警 (200m / 100m / 50m)
  - 记录接近电力线的无人机轨迹到数据库

用法:
  python app/cli.py                          # 默认 (BLE 模式)
  python app/cli.py --mode ble               # BLE 模式
  python app/cli.py --mode wifi              # WiFi Beacon 模式 (需要 Npcap)
  python app/cli.py --mode wifi -i Wi-Fi     # 指定WiFi网卡
  python app/cli.py --config /path/config.yaml
"""

import asyncio
import signal
import sys
import os
import argparse
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logging_config import get_logger
from core.config import load_config
from core.parser import ParsedRID
from receiver.ble import BLE_RIDReceiver
from receiver.wifi import create_wifi_receiver
from display.terminal import LogDisplay, SimpleDisplay

logger = get_logger(__name__)


class RIDController:
    """RID 系统主控制器 - 协调所有模块"""

    def __init__(self, config: dict):
        self.config = config

        # 使用共享工厂初始化所有核心组件
        from core.bootstrap import bootstrap_core
        base_dir = os.path.dirname(os.path.abspath(
            config.get('_config_path') or __file__))
        core = bootstrap_core(config=config, base_dir=base_dir)

        self.db = core['db']
        self.pl_manager = core['pl_manager']
        self.alert = core['alert_system']
        self.trajectory_recorder = core['trajectory_recorder']
        self.pipeline = core['pipeline']
        self.backhaul = core['backhaul']
        thresholds = core['thresholds']

        # 初始化显示 (TTY → 事件日志行, 管道 → 纯文本)
        display_config = config.get("display", {})
        if sys.stdout.isatty():
            self.display = LogDisplay(thresholds=thresholds)
        else:
            self.display = SimpleDisplay(thresholds=thresholds)

        self.display_interval = display_config.get("update_interval", 1.0)

        # 过时无人机清理配置
        self.stale_timeout = config.get("stale_timeout", 120)

        # 运行状态
        self._running = False
        self._display_task = None

    def on_rid_received(self, parsed: ParsedRID):
        """RID 数据回调 — 委托给 Pipeline 处理"""
        result = self.pipeline.process(parsed)
        if result is not None and result.alert_level:
            self.display.add_alert(
                result.drone_id, result.alert_level,
                result.distance,
                result.nearest_line.name if result.nearest_line else "?",
            )

    async def _display_loop(self):
        """显示刷新循环"""
        while self._running:
            try:
                drones = self.db.get_active_drones()
                # 附加最近电力线名称
                for d in drones:
                    lid = d.get("nearest_line_id")
                    if lid:
                        for line in self.pl_manager.lines:
                            if line.line_id == lid:
                                d["line_name"] = line.name
                                break
                alert_drones = dict(self.alert._drone_level)
                self.display.refresh(drones, alert_drones)
            except Exception as e:
                logger.error("显示刷新失败: %s", e)

            await asyncio.sleep(self.display_interval)

    async def _stale_drone_cleanup(self):
        """清理长时间未更新的无人机"""
        from datetime import datetime, timezone, timedelta
        while self._running:
            try:
                cutoff = (datetime.now(timezone.utc) - timedelta(seconds=self.stale_timeout)).isoformat()
                drones = self.db.get_active_drones()
                for d in drones:
                    last_seen = d.get("last_seen", "")
                    if last_seen and last_seen < cutoff:
                        self.db.mark_gone(d["id"])
                        self.trajectory_recorder.stop_tracking(d["id"])
                        if d["id"] in self.alert._drone_level:
                            del self.alert._drone_level[d["id"]]
                        logger.info("%s 已离线 (最后出现: %s)", d["id"], last_seen[:19])
            except Exception as e:
                logger.error("清理离线无人机失败: %s", e)
            await asyncio.sleep(30)

    async def run(self, receiver):
        """主循环"""
        self._running = True

        # 启动数据回传 (含设备心跳+定位)
        self.backhaul.start()

        # 启动显示刷新任务
        self._display_task = asyncio.create_task(self._display_loop())
        cleanup_task = asyncio.create_task(self._stale_drone_cleanup())

        logger.info("无人机 RID 接收系统已启动")
        logger.info("电力线段数: %d", len(self.pl_manager.lines))
        logger.info("告警阈值: ≤%sm 警告, ≤%sm 严重, ≤%sm 驱离",
                    self.alert.thresholds['warning'],
                    self.alert.thresholds['severe'],
                    self.alert.thresholds['critical'])
        logger.info("按 Ctrl+C 停止")

        try:
            await receiver.start()
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            await receiver.stop()
            self._display_task.cancel()
            cleanup_task.cancel()
            try:
                await self._display_task
                await cleanup_task
            except asyncio.CancelledError:
                pass

            # 清理
            self.backhaul.stop()
            if self.pipeline.raw_archive:
                self.pipeline.raw_archive.stop()
            self.db.close()
            logger.info("系统已安全停止")


def main():
    parser = argparse.ArgumentParser(
        description="无人机 RID 接收与电力线防碰撞监控系统"
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="配置文件路径 (默认: config/config.yaml)"
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["ble", "wifi", "simulated"],
        default="simulated",
        help="接收模式: simulated (模拟演示) / ble (真实BLE) / wifi (WiFi Beacon)"
    )
    parser.add_argument(
        "--scan-duration",
        type=float,
        default=30.0,
        help="BLE 扫描器重建间隔 (秒, 0=持续不重建)"
    )
    parser.add_argument(
        "--wifi-interface", "-i",
        default=None,
        help="WiFi 网卡接口名称 (如 Wi-Fi, wlan0)"
    )
    args = parser.parse_args()

    # 加载配置
    config_path = args.config
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), "..", "config", "config.yaml")
    config = load_config(config_path)

    # 创建控制器
    controller = RIDController(config)

    # 创建接收器
    if args.mode == "simulated":
        from receiver.simulated import create_simulated_receiver
        receiver = create_simulated_receiver(
            callback=controller.on_rid_received,
            pl_manager=controller.pl_manager,
            drone_count=6,
            update_interval=1.0,
        )
    elif args.mode == "wifi":
        receiver = create_wifi_receiver(
            callback=controller.on_rid_received,
            interface=args.wifi_interface,
        )
    else:
        receiver = BLE_RIDReceiver(
            callback=controller.on_rid_received,
            scan_duration=args.scan_duration,
        )

    # 处理 Ctrl+C
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown():
        logger.info("正在停止系统...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            # Windows 不支持 add_signal_handler
            signal.signal(sig, lambda s, f: shutdown())

    try:
        loop.run_until_complete(controller.run(receiver))
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
