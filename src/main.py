#!/usr/bin/env python3
"""
无人机 RID 接收与电力线防碰撞系统 — 主程序

功能:
  - 接收无人机 Remote ID (RID) 广播 (BLE 或模拟)
  - 实时显示无人机位置
  - 计算与电力线的垂直距离
  - 三级告警 (200m / 100m / 50m) + 短信通知
  - 记录接近电力线的无人机轨迹到数据库

用法:
  python src/main.py                          # 默认 (模拟模式)
  python src/main.py --mode ble               # BLE 模式
  python src/main.py --mode wifi              # WiFi Beacon 模式 (需要 Npcap)
  python src/main.py --mode wifi -i Wi-Fi     # 指定WiFi网卡
  python src/main.py --config /path/config.yaml
"""

import asyncio
import signal
import sys
import os
import yaml
import argparse
from pathlib import Path

# 添加 src 到路径
sys.path.insert(0, str(Path(__file__).parent))

from db import Database
from rid_parser import ParsedRID, UA_TYPE_NAMES
from rid_receiver import BLE_RIDReceiver, MockRIDReceiver
from wifi_receiver import create_wifi_receiver
from powerline import PowerLineManager
from alert import AlertSystem, MockSMSBackend, TwilioSMSBackend, AliyunSMSBackend
from trajectory import TrajectoryRecorder
from display import Display, SimpleDisplay


class RIDController:
    """RID 系统主控制器 - 协调所有模块"""

    def __init__(self, config: dict):
        self.config = config

        # 初始化数据库
        db_path = config.get("database", {}).get("path", "data/drone_rid.db")
        if not os.path.isabs(db_path):
            db_path = os.path.join(os.path.dirname(__file__), "..", db_path)
        self.db = Database(db_path)

        # 加载电力线
        self.pl_manager = PowerLineManager()
        pl_file = config.get("power_lines_file", "config/power_lines.yaml")
        if not os.path.isabs(pl_file):
            pl_file = os.path.join(os.path.dirname(__file__), "..", pl_file)
        count = self.pl_manager.load_from_yaml(pl_file)
        print(f"[初始化] 已加载 {count} 条电力线段")
        # 同步到数据库
        pl_dicts = [
            {
                "name": l.name, "lat1": l.lat1, "lon1": l.lon1, "alt1": l.alt1,
                "lat2": l.lat2, "lon2": l.lon2, "alt2": l.alt2,
                "id": l.line_id,
            }
            for l in self.pl_manager.lines
        ]
        self.db.load_power_lines(pl_dicts)

        # 初始化短信后端
        sms_config = config.get("sms", {})
        backend_type = sms_config.get("backend", "mock")
        sms_backend = self._create_sms_backend(backend_type, sms_config)
        print(f"[初始化] 短信后端: {backend_type}")

        # 初始化告警系统
        thresholds = config.get("thresholds", {
            "warning": 200, "severe": 100, "critical": 50
        })
        self.alert = AlertSystem(
            db=self.db,
            sms_backend=sms_backend,
            thresholds=thresholds,
            alert_contacts=config.get("alert_contacts", []),
            pilot_phones=config.get("pilot_phones", {}),
        )

        # 初始化轨迹记录器
        traj_config = config.get("trajectory", {})
        self.trajectory_recorder = TrajectoryRecorder(
            db=self.db,
            min_interval=traj_config.get("min_interval", 2.0),
            max_points_per_drone=traj_config.get("max_points_per_drone", 1000),
        )

        # 初始化显示
        display_config = config.get("display", {})
        self.display = Display(thresholds=thresholds) if sys.stdout.isatty() \
            else SimpleDisplay(thresholds=thresholds)

        self.display_interval = display_config.get("update_interval", 1.0)

        # 过时无人机清理配置
        self.stale_timeout = config.get("stale_timeout", 120)

        # 运行状态
        self._running = False
        self._display_task = None

    def _create_sms_backend(self, backend_type: str, sms_config: dict):
        """创建短信后端实例"""
        if backend_type == "twilio":
            tw = sms_config.get("twilio", {})
            return TwilioSMSBackend(
                account_sid=tw.get("account_sid", ""),
                auth_token=tw.get("auth_token", ""),
                from_number=tw.get("from_number", ""),
            )
        elif backend_type == "aliyun":
            al = sms_config.get("aliyun", {})
            templates = {
                "warning": al.get("template_code_warning", ""),
                "severe": al.get("template_code_severe", ""),
                "critical": al.get("template_code_critical", ""),
            }
            return AliyunSMSBackend(
                access_key_id=al.get("access_key_id", ""),
                access_key_secret=al.get("access_key_secret", ""),
                sign_name=al.get("sign_name", ""),
                template_codes=templates,
            )
        else:
            return MockSMSBackend()

    def on_rid_received(self, parsed: ParsedRID):
        """
        RID 数据回调 - 核心处理逻辑

        每次收到无人机广播时触发:
        1. 更新数据库中的无人机状态
        2. 计算与电力线的垂直距离
        3. 判断告警级别并发送短信
        4. 记录轨迹
        5. (显示由独立协程刷新)
        """
        drone_id = parsed.drone_id
        if not drone_id or not parsed.location:
            return

        loc = parsed.location
        ua_type = UA_TYPE_NAMES.get(
            parsed.basic_id.ua_type if parsed.basic_id else 0, "未知"
        )

        # 1. 更新数据库
        self.db.upsert_drone(
            drone_id=drone_id,
            lat=loc.latitude,
            lon=loc.longitude,
            alt=loc.altitude_geodetic,
            speed=loc.speed_horizontal,
            heading=0,
        )

        # 2. 计算距离最近的电力线
        nearest_line, distance = self.pl_manager.find_nearest_line(
            loc.latitude, loc.longitude, loc.altitude_geodetic
        )

        if nearest_line is None:
            return  # 没有电力线数据

        # 3. 更新无人机距离状态
        status = "active"
        if distance <= self.alert.thresholds.get("critical", 50):
            status = "critical"
        elif distance <= self.alert.thresholds.get("severe", 100):
            status = "severe"
        elif distance <= self.alert.thresholds.get("warning", 200):
            status = "warning"

        self.db.update_drone_distance(
            drone_id=drone_id,
            distance=distance,
            line_id=nearest_line.line_id,
            status=status,
        )

        # 4. 告警处理
        if distance <= self.alert.thresholds.get("warning", 200):
            self.alert.process(
                drone_id=drone_id,
                distance=distance,
                line_name=nearest_line.name,
                line_id=nearest_line.line_id,
                drone_alt=loc.altitude_geodetic,
                drone_lat=loc.latitude,
                drone_lon=loc.longitude,
            )

            # 5. 记录轨迹
            self.trajectory_recorder.record(
                drone_id=drone_id,
                lat=loc.latitude,
                lon=loc.longitude,
                alt=loc.altitude_geodetic,
                distance=distance,
                line_id=nearest_line.line_id,
            )
        else:
            # 距离恢复正常，停止追踪
            self.trajectory_recorder.stop_tracking(drone_id)

    async def _display_loop(self):
        """显示刷新循环"""
        while self._running:
            try:
                drones = self.db.get_active_drones()
                alert_drones = dict(self.alert._drone_level)
                self.display.refresh(drones, alert_drones)
            except Exception as e:
                print(f"[显示] 刷新错误: {e}")

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
                        print(f"[清理] {d['id']} 已离线 (最后出现: {last_seen[:19]})")
            except Exception as e:
                print(f"[清理] 错误: {e}")
            await asyncio.sleep(30)

    async def run(self, receiver):
        """主循环"""
        self._running = True

        # 启动显示刷新任务
        self._display_task = asyncio.create_task(self._display_loop())
        cleanup_task = asyncio.create_task(self._stale_drone_cleanup())

        print("\n[系统] 无人机 RID 接收系统已启动")
        print(f"[系统] 电力线段数: {len(self.pl_manager.lines)}")
        print(f"[系统] 告警阈值: "
              f"≤{self.alert.thresholds['warning']}m 警告, "
              f"≤{self.alert.thresholds['severe']}m 严重, "
              f"≤{self.alert.thresholds['critical']}m 驱离")
        print("[系统] 按 Ctrl+C 停止\n")

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
            self.db.close()
            print("\n[系统] 已安全停止")


def load_config(config_path: str) -> dict:
    """加载 YAML 配置文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


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
        choices=["ble", "mock", "wifi"],
        default="mock",
        help="接收模式: ble (真实BLE) / wifi (WiFi Beacon) / mock (模拟)"
    )
    parser.add_argument(
        "--scan-duration",
        type=float,
        default=5.0,
        help="BLE 扫描持续时间 (秒)"
    )
    parser.add_argument(
        "--mock-interval",
        type=float,
        default=1.0,
        help="模拟数据生成间隔 (秒)"
    )
    parser.add_argument(
        "--mock-drones",
        type=int,
        default=3,
        help="模拟无人机数量"
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
    if args.mode == "ble":
        receiver = BLE_RIDReceiver(
            callback=controller.on_rid_received,
            scan_duration=args.scan_duration,
        )
    elif args.mode == "wifi":
        receiver = create_wifi_receiver(
            callback=controller.on_rid_received,
            interface=args.wifi_interface,
            prefer_scapy=True,
        )
    else:
        receiver = MockRIDReceiver(
            callback=controller.on_rid_received,
            interval=args.mock_interval,
            num_drones=args.mock_drones,
        )

    # 处理 Ctrl+C
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown():
        print("\n[系统] 正在停止...")
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
