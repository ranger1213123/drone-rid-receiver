"""
数据回传管理器 — MQTT 主通道 + 北斗短报文应急

通道优先级:
  1. MQTT (mTLS) — 常规数据回传 + 心跳 + 配置同步
  2. SMS — MQTT 中断时发送告警短信 (可选)
  3. 北斗短报文 — MQTT 中断时发送 critical 告警
  4. SQLite Outbox — 所有通道中断时持久化积压，MQTT 重连后补传

已废除: HTTP POST 回传、JWT TokenManager、指数退避重试
"""

import json
import threading
import time
from datetime import datetime
from typing import Optional, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from storage.database import Database

from logging_config import get_logger
from core.beidou import BeidouDevice, format_emergency_message
from core.sms_gateway import SMSGateway, create_sms_gateway

logger = get_logger(__name__)


class ChannelStatus:
    OFFLINE = 0
    ONLINE = 1
    DEGRADED = 2


class BackhaulManager:
    """数据回传管理器 — MQTT + 北斗双通道"""

    def __init__(self, config: dict, beidou: BeidouDevice,
                 db: "Database", device_name: str = 'NW-F1',
                 mqtt_channel=None, pl_manager=None):
        self._config = config
        self._beidou = beidou
        self._db = db
        self._device_name = device_name
        self._mqtt = mqtt_channel  # MqttChannel 实例 (由 bootstrap 注入)
        self._pl_manager = pl_manager
        self._lock = threading.Lock()

        # ── SMS 网关 ──
        self._sms: SMSGateway = create_sms_gateway(config)
        sms_cfg = config.get('backhaul', {}).get('sms', {})
        self._sms_alert_phones = sms_cfg.get('alert_phones', [])
        self._sms_enabled = sms_cfg.get('enabled', False)
        self._send_sms_from_edge = sms_cfg.get('send_from_edge', True)

        # ── 北斗应急配置 ──
        bd_cfg = config.get('backhaul', {}).get('beidou', {})
        self._bd_emergency_receiver = bd_cfg.get('emergency_receiver_id', '')
        self._bd_min_level = bd_cfg.get('min_alert_level', 'critical')

        # ── 设备自身位置 ──
        pos_cfg = config.get('position', {})
        self._position_source = pos_cfg.get('source', 'beidou')
        self._fallback_lat = pos_cfg.get('manual_lat', 0)
        self._fallback_lon = pos_cfg.get('manual_lon', 0)
        self._fallback_alt = pos_cfg.get('manual_alt', 0)

        # ── 心跳 ──
        self._heartbeat_interval = config.get('backhaul', {}).get('http', {}).get('heartbeat_interval', 30)

        # ── 后台线程 ──
        self._running = False
        self._health_thread: Optional[threading.Thread] = None
        self._last_heartbeat = 0

        # ── 告警回调 ──
        self._alert_callback: Optional[Callable] = None

        # ── 统计 ──
        self._stats = {
            'mqtt_sent': 0, 'mqtt_failed': 0,
            'beidou_sent': 0, 'beidou_failed': 0,
            'last_send_time': '',
        }

    # ── 生命周期 ──

    def start(self):
        if self._running:
            return
        self._running = True
        self._db.drain_outbox_on_start()
        self._start_beidou()

        if self._mqtt:
            self._mqtt.start()
            logger.info("数据回传管理器已启动 | MQTT + 北斗")

        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()

    def stop(self):
        self._running = False
        if self._health_thread and self._health_thread.is_alive():
            self._health_thread.join(timeout=5)
        if self._mqtt:
            self._mqtt.stop()
        self._beidou.close()
        logger.info("数据回传管理器已停止")

    def _start_beidou(self):
        if self._beidou.open():
            logger.info("北斗短报文已就绪")

    # ── 属性 ──

    @property
    def mqtt_connected(self) -> bool:
        return self._mqtt is not None and self._mqtt.connected

    @property
    def beidou_online(self) -> bool:
        return self._beidou.connected

    @property
    def active_channel(self) -> str:
        if self.mqtt_connected:
            return 'mqtt'
        if self._beidou.connected:
            return 'beidou_emergency'
        return 'offline'

    @property
    def channel_status(self) -> str:
        return self.active_channel

    @property
    def primary_online(self) -> bool:
        return self.mqtt_connected

    @property
    def stats(self) -> dict:
        s = dict(self._stats)
        s['queued'] = self._db.outbox_pending_count()
        if self._mqtt:
            s.update(self._mqtt.stats)
        return s

    @property
    def queue_size(self) -> int:
        return self._db.outbox_pending_count()

    def set_alert_callback(self, cb: Callable):
        self._alert_callback = cb

    # ── 设备定位 ──

    def _get_device_position(self) -> tuple:
        if self._position_source == 'beidou' and self._beidou:
            pos = self._beidou.get_position()
            if pos and pos.has_fix:
                return pos.latitude, pos.longitude, pos.altitude
        return self._fallback_lat, self._fallback_lon, self._fallback_alt

    def inject_gps(self, lat: float, lon: float, alt: float):
        self._fallback_lat = lat
        self._fallback_lon = lon
        self._fallback_alt = alt

    # ── 上行 API ──

    def report_drone(self, drone_id: str, lat: float, lon: float, alt: float,
                     distance: float, line_name: str, status: str) -> bool:
        """上报无人机数据 → MQTT, 失败入 outbox"""
        payload = {
            'device': self._device_name,
            'drone_id': drone_id,
            'latitude': lat, 'longitude': lon, 'altitude': alt,
            'distance_to_line': distance,
            'nearest_line': line_name, 'status': status,
            'timestamp': datetime.now().isoformat(),
        }
        return self._send_via_mqtt("report", payload, qos=1)

    def report_alert(self, drone_id: str, level: str, distance: float,
                     line_name: str, lat: float, lon: float,
                     alt: float, drone_model: str = "",
                     takeoff_lat: float = None,
                     takeoff_lon: float = None) -> str:
        """上报告警 → MQTT (QoS 2), 失败降级 SMS → 北斗 → outbox

        Returns: 'mqtt' | 'sms' | 'beidou' | 'queued' | 'dropped'
        """
        payload = {
            'device': self._device_name,
            'type': 'alert',
            'drone_id': drone_id, 'level': level,
            'distance': distance, 'nearest_line': line_name,
            'latitude': lat, 'longitude': lon, 'altitude': alt,
            'drone_model': drone_model,
            'takeoff_lat': takeoff_lat, 'takeoff_lon': takeoff_lon,
            'timestamp': datetime.now().isoformat(),
        }
        if self._send_via_mqtt("alert", payload, qos=2):
            return 'mqtt'

        # SMS 降级
        if self._send_sms_from_edge and self._sms_enabled and self._sms_alert_phones:
            model_part = f"机型:{drone_model} " if drone_model else ""
            alt_part = f"高度:{alt:.0f}m " if alt else ""
            sms_msg = (
                f"[{level.upper()}]无人机告警 SN:{drone_id} "
                f"{model_part}距 {line_name} {distance:.0f}m "
                f"{alt_part}位置:({lat:.5f},{lon:.5f})"
            )
            if takeoff_lat is not None and takeoff_lon is not None:
                sms_msg += f" 起飞:({takeoff_lat:.5f},{takeoff_lon:.5f})"
            if self._sms.send(self._sms_alert_phones, sms_msg):
                logger.info("SMS 告警已发送")
                return 'sms'

        # 北斗降级 (仅 critical)
        if self._beidou.connected and self._should_use_beidou(level):
            msg = format_emergency_message(
                self._device_name, drone_id, level, distance, line_name,
                lat, lon, alt,
            )
            if self._beidou.send_message(self._bd_emergency_receiver, msg):
                self._stats['beidou_sent'] += 1
                return 'beidou'
            else:
                self._stats['beidou_failed'] += 1

        # Outbox 持久化
        self._db.insert_outbox(payload, target_path="alert", topic_suffix="alert", priority=1)
        return 'queued'

    def send_heartbeat(self) -> bool:
        """发送设备心跳 → MQTT QoS 0"""
        dev_lat, dev_lon, dev_alt = self._get_device_position()
        payload = {
            'device': self._device_name,
            'device_lat': dev_lat, 'device_lon': dev_lon, 'device_alt': dev_alt,
            'active_channel': self.active_channel,
            'timestamp': datetime.now().isoformat(),
        }
        return self._send_via_mqtt("heartbeat", payload, qos=0)

    # ── 出队 (Outbox drain, MQTT 重连后触发) ──

    def flush_if_needed(self):
        """MQTT 在线时排空 outbox 积压"""
        if not self.mqtt_connected:
            return
        drained = 0
        for msg in self._db.get_pending_outbox(limit=100):
            try:
                payload = json.loads(msg["payload"])
            except json.JSONDecodeError:
                self._db.mark_outbox_dead(msg["id"])
                continue

            topic_suffix = msg.get("topic_suffix", "") or msg.get("target_path", "").replace("/api/", "")
            if not topic_suffix:
                topic_suffix = "report"

            sent = self._mqtt.publish(topic_suffix, payload, qos=1)
            if sent:
                self._db.mark_outbox_sent(msg["id"])
                drained += 1
            else:
                break  # MQTT 不通，停止 drain

        if drained > 0:
            logger.info("MQTT outbox drain: %d 条已补传", drained)

    # ── 电力线同步 ──

    def fetch_power_lines(self) -> int:
        """MQTT 模式下电力线由云端推送 (no-op)；保留接口兼容 HTTP 过渡期"""
        return 0

    # ── 内部方法 ──

    def _send_via_mqtt(self, topic_suffix: str, payload: dict, qos: int) -> bool:
        if self._mqtt and self._mqtt.publish(topic_suffix, payload, qos=qos):
            self._stats['mqtt_sent'] += 1
            self._stats['last_send_time'] = datetime.now().strftime('%H:%M:%S')
            return True
        # MQTT 不通 → 入 outbox
        self._db.insert_outbox(payload, target_path=topic_suffix,
                               topic_suffix=topic_suffix, priority=0)
        return False

    def _should_use_beidou(self, level: str) -> bool:
        levels = ['warning', 'severe', 'critical']
        min_idx = levels.index(self._bd_min_level) if self._bd_min_level in levels else 2
        cur_idx = levels.index(level) if level in levels else 0
        return cur_idx >= min_idx

    # ── 后台健康循环 ──

    def _health_loop(self):
        """健康检测: 心跳 + 北斗信号监控 + outbox drain"""
        while self._running:
            time.sleep(min(self._heartbeat_interval, 15))
            now = time.time()

            # 心跳
            if now - self._last_heartbeat >= self._heartbeat_interval:
                self.send_heartbeat()
                self._last_heartbeat = now

            # outbox drain
            self.flush_if_needed()

            # 北斗信号
            if self._beidou.connected:
                sig = self._beidou.check_signal()
                self._beidou.signal_strength = sig
