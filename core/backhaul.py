"""
数据回传管理器 — MQTT 主通道 + SQLite Outbox 持久化

策略:
  1. MQTT (mTLS) — 常规数据回传 + 心跳 + 配置同步
  2. SQLite Outbox — MQTT 中断时持久化积压，重连后自动补传

杆塔设备有稳定供电和 4G/5G 网络，MQTT 失败即网络抖动，
不应降级到 SMS/北斗，直接本地持久化等待重试即可。
"""

import json
import threading
import time
from datetime import datetime
from typing import Optional, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from storage.database import Database

from logging_config import get_logger

logger = get_logger(__name__)


class BackhaulManager:
    """数据回传管理器 — MQTT + Outbox"""

    def __init__(self, config: dict, db: "Database",
                 device_name: str = 'NW-F1',
                 mqtt_channel=None, pl_manager=None):
        self._config = config
        self._db = db
        self._device_name = device_name
        self._mqtt = mqtt_channel
        self._pl_manager = pl_manager
        self._lock = threading.Lock()

        # ── 设备自身位置 (配置固定坐标, 杆塔位置不变) ──
        pos_cfg = config.get('position', {})
        self._device_lat = pos_cfg.get('manual_lat', 0)
        self._device_lon = pos_cfg.get('manual_lon', 0)
        self._device_alt = pos_cfg.get('manual_alt', 0)

        # ── 心跳 ──
        self._heartbeat_interval = config.get('backhaul', {}).get('http', {}).get('heartbeat_interval', 30)

        # ── 后台线程 ──
        self._running = False
        self._health_thread: Optional[threading.Thread] = None
        self._last_heartbeat = 0

        # ── 统计 ──
        self._stats = {
            'mqtt_sent': 0, 'mqtt_failed': 0,
            'last_send_time': '',
        }

    # ── 生命周期 ──

    def start(self):
        if self._running:
            return
        self._running = True
        self._db.drain_outbox_on_start()

        if self._mqtt:
            self._mqtt.start()
            logger.info("数据回传管理器已启动 | MQTT + Outbox")
        else:
            logger.warning("MQTT 未启用, 数据仅写入 outbox")

        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()

    def stop(self):
        self._running = False
        if self._health_thread and self._health_thread.is_alive():
            self._health_thread.join(timeout=5)
        if self._mqtt:
            self._mqtt.stop()
        logger.info("数据回传管理器已停止")

    # ── 属性 ──

    @property
    def mqtt_connected(self) -> bool:
        return self._mqtt is not None and self._mqtt.connected

    @property
    def active_channel(self) -> str:
        return 'mqtt' if self.mqtt_connected else 'offline'

    @property
    def channel_status(self) -> str:
        return self.active_channel

    @property
    def primary_online(self) -> bool:
        return self.mqtt_connected

    @property
    def beidou_online(self) -> bool:
        return False  # 不再使用北斗

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
        """上报告警 → MQTT (QoS 2), 失败入 outbox 等待重试

        Returns: 'mqtt' | 'queued'
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
        return 'mqtt' if self._send_via_mqtt("alert", payload, qos=2, priority=1) else 'queued'

    def send_heartbeat(self) -> bool:
        """发送设备心跳 → MQTT QoS 0"""
        payload = {
            'device': self._device_name,
            'device_lat': self._device_lat,
            'device_lon': self._device_lon,
            'device_alt': self._device_alt,
            'active_channel': self.active_channel,
            'timestamp': datetime.now().isoformat(),
        }
        return self._send_via_mqtt("heartbeat", payload, qos=0)

    # ── 出队 (Outbox drain, MQTT 重连后触发) ──

    def flush_if_needed(self):
        """MQTT 在线时排空 outbox 积压 (原子认领: pending → sending)"""
        if not self.mqtt_connected:
            return
        drained = 0
        for msg in self._db.claim_pending_outbox(limit=100):
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
                # MQTT 不通, 回退 sending → pending 等待下次重试
                self._db.mark_outbox_failed(msg["id"], "mqtt publish failed")
                break

        if drained > 0:
            logger.info("MQTT outbox drain: %d 条已补传", drained)

    # ── 电力线同步 ──

    def fetch_power_lines(self) -> int:
        """HTTP 轮询云端电力线配置 (MQTT 离线时的降级方案)"""
        sync_cfg = self._config.get("backhaul", {}).get("power_line_sync", {})
        if not sync_cfg.get("enabled", False):
            return 0
        sync_url = sync_cfg.get("sync_url", "").strip()
        if not sync_url:
            logger.debug("未配置 power_line_sync.sync_url，跳过")
            return 0

        try:
            import urllib.request
            import urllib.error
            params = {}
            if self._device_name:
                params["device_name"] = self._device_name
            if params:
                from urllib.parse import urlencode
                sync_url = f"{sync_url}?{urlencode(params)}"
            req = urllib.request.Request(sync_url, method="GET")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            logger.warning("电力线同步失败 (网络): %s", e)
            return 0
        except json.JSONDecodeError as e:
            logger.warning("电力线同步失败 (JSON 解析): %s", e)
            return 0
        except Exception as e:
            logger.warning("电力线同步失败: %s", e)
            return 0

        lines = data.get("lines", [])
        version = data.get("version", "")
        if not lines:
            return 0

        self._db.load_power_lines(lines)
        if version:
            self._db.set_config_version(version)
        if self._pl_manager:
            self._pl_manager.load_from_list(lines)
        logger.info("电力线同步完成: %d 条 (version=%s)", len(lines), version or "-")
        return len(lines)

    # ── 内部方法 ──

    def _send_via_mqtt(self, topic_suffix: str, payload: dict, qos: int, priority: int = 0) -> bool:
        if self._mqtt and self._mqtt.publish(topic_suffix, payload, qos=qos):
            self._stats['mqtt_sent'] += 1
            self._stats['last_send_time'] = datetime.now().strftime('%H:%M:%S')
            return True
        # MQTT 不通 → 入 outbox, 等待重试
        self._db.insert_outbox(payload, target_path=topic_suffix,
                               topic_suffix=topic_suffix, priority=priority)
        self._stats['mqtt_failed'] += 1
        return False

    # ── 后台健康循环 ──

    def _health_loop(self):
        """心跳 + outbox drain (drain 间隔自适应: 有积压时 1s, 空闲时 5s)"""
        _last_dead_check = 0
        while self._running:
            # drain 优先: 有积压时快速排空, 空闲时降低频率
            queue_size = self._db.outbox_pending_count()
            drain_interval = 1 if queue_size > 0 else 5
            time.sleep(drain_interval)
            now = time.time()

            if now - self._last_heartbeat >= self._heartbeat_interval:
                self.send_heartbeat()
                self._last_heartbeat = now

            self.flush_if_needed()

            # 死信检测 (每 60s 检查一次, 避免频繁查询)
            if now - _last_dead_check >= 60:
                _last_dead_check = now
                dead_count = self._db.dead_letter_count()
                if dead_count > 0:
                    logger.warning("Outbox 死信: %d 条消息无法发送, 请检查网络或 MQTT broker", dead_count)
