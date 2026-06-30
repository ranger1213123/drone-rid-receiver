"""
实时数据推送桥接 — MQTT订阅 → 内存缓存 → WebSocket推送

云Web后端直接订阅EMQX的无人机位置/告警主题, 维护内存缓存,
通过Socket.IO推送给Leaflet前端。DB只负责历史查询, 不承担实时刷新压力。

用法:
    live_feed = LiveFeed(mqtt_config, socketio)
    live_feed.start()
    # /api/status 改为从 live_feed.get_active_drones() 读取
"""

import json
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

import paho.mqtt.client as mqtt

from logging_config import get_logger

logger = get_logger(__name__)

TOPIC_EDGE_REPORT = "drone/+/report"
TOPIC_EDGE_ALERT = "drone/+/alert"
TOPIC_EDGE_HEARTBEAT = "drone/+/heartbeat"


class LiveFeed:
    """MQTT → WebSocket 实时推送桥接

    - 订阅 EMQX 通配符 topic, 维护内存中的无人机位置缓存
    - 通过 Socket.IO 推送增量更新给前端, 替代 /api/status 轮询
    - 未配置 MQTT 时退化为纯缓存模式 (由外部数据源写入)
    """

    STALE_SECONDS = 120  # 超过此时间未更新的无人机视为离线

    def __init__(self, mqtt_config: Optional[dict] = None, socketio=None):
        self._mqtt_config = mqtt_config or {}
        self._socketio = socketio
        self._mqtt_client: Optional[mqtt.Client] = None
        self._lock = threading.Lock()
        self._running = False
        self._connected = False

        # 内存缓存
        self._drones: Dict[str, dict] = {}        # drone_id → latest state
        self._device_stats: Dict[str, dict] = {}   # device_name → heartbeat
        self._recent_alerts: List[dict] = []       # 最近告警 (最多 200 条)

    # ── 生命周期 ──

    def start(self):
        if self._running:
            return
        self._running = True

        if self._mqtt_config.get("enabled", False):
            self._start_mqtt()
        else:
            logger.info("LiveFeed: MQTT 未启用, 缓存仅由 API 写入")

    def stop(self):
        self._running = False
        if self._mqtt_client:
            self._mqtt_client.disconnect()
            self._mqtt_client = None

    # ── MQTT 订阅 ──

    def _start_mqtt(self):
        broker = self._mqtt_config.get("broker", {})
        tls_cfg = self._mqtt_config.get("tls", {})

        client_id = f"web-dashboard-{uuid.uuid4().hex[:8]}"
        self._mqtt_client = mqtt.Client(
            client_id=client_id, clean_session=True, protocol=mqtt.MQTTv311,
        )
        self._mqtt_client.on_connect = self._on_connect
        self._mqtt_client.on_message = self._on_message

        if tls_cfg.get("enabled", False):
            self._mqtt_client.tls_set(
                ca_certs=tls_cfg.get("ca_cert", ""),
                certfile=tls_cfg.get("client_cert", ""),
                keyfile=tls_cfg.get("client_key", ""),
            )

        host = broker.get("host", "localhost")
        port = broker.get("port", 8883)

        try:
            self._mqtt_client.connect(host, port, keepalive=60)
            self._mqtt_client.loop_start()
            logger.info("LiveFeed MQTT 连接中: %s:%d", host, port)
        except Exception as e:
            logger.warning("LiveFeed MQTT 连接失败: %s, 退化为轮询模式", e)
            self._mqtt_client = None

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            client.subscribe(TOPIC_EDGE_REPORT, qos=1)
            client.subscribe(TOPIC_EDGE_ALERT, qos=1)
            client.subscribe(TOPIC_EDGE_HEARTBEAT, qos=0)
            logger.info("LiveFeed MQTT 已连接, 订阅通配符主题")
        else:
            logger.warning("LiveFeed MQTT 连接失败: rc=%d", rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except json.JSONDecodeError:
            return

        topic = msg.topic

        if topic.endswith("/report"):
            self._handle_report(payload)
        elif topic.endswith("/alert"):
            self._handle_alert(payload)
        elif topic.endswith("/heartbeat"):
            self._handle_heartbeat(payload)

    # ── 消息处理 ──

    def _handle_report(self, payload: dict):
        drone_id = payload.get("drone_id", "")
        if not drone_id:
            return

        device = payload.get("device") or payload.get("dev_id", "")
        now = time.time()

        with self._lock:
            drone = self._drones.get(drone_id, {})
            drone.update({
                "id": drone_id,
                "last_lat": payload.get("latitude", drone.get("last_lat", 0)),
                "last_lon": payload.get("longitude", drone.get("last_lon", 0)),
                "last_alt": payload.get("altitude", drone.get("last_alt", 0)),
                "min_distance": payload.get("distance_to_line"),
                "nearest_line": payload.get("nearest_line", ""),
                "status": payload.get("status", drone.get("status", "active")),
                "device": device,
                "_updated": now,
            })
            self._drones[drone_id] = drone

        if self._socketio:
            self._socketio.emit("drone_update", {
                "drone_id": drone_id,
                "lat": drone.get("last_lat"),
                "lon": drone.get("last_lon"),
                "alt": drone.get("last_alt"),
                "distance": drone.get("min_distance"),
                "line": drone.get("nearest_line"),
                "status": drone.get("status"),
                "device_name": device,
            })

    def _handle_alert(self, payload: dict):
        drone_id = payload.get("drone_id", "")
        level = payload.get("level", "")
        distance = payload.get("distance", 0)
        line_name = payload.get("nearest_line", "")
        timestamp = payload.get("timestamp", "")
        device = payload.get("device") or payload.get("dev_id", "")

        alert = {
            "drone_id": drone_id,
            "level": level,
            "distance": distance,
            "line_name": line_name,
            "timestamp": timestamp,
            "device": device,
        }

        with self._lock:
            self._recent_alerts.insert(0, alert)
            if len(self._recent_alerts) > 200:
                self._recent_alerts = self._recent_alerts[:200]

        if self._socketio:
            self._socketio.emit("alert_update", alert)

    def _handle_heartbeat(self, payload: dict):
        device = payload.get("device") or payload.get("dev_id", "")
        if not device:
            return

        lat = payload.get("lat", 0) or 0
        lon = payload.get("lon", 0) or 0
        alt = payload.get("alt", 0) or 0

        with self._lock:
            self._device_stats[device] = {
                "channel": payload.get("active_channel") or "mqtt",
                "timestamp": payload.get("timestamp", ""),
                "lat": lat,
                "lon": lon,
                "alt": alt,
                "gps_valid": payload.get("gps_valid", False),
                "csq": payload.get("csq", ""),
                "count": payload.get("count", 0),
                "_updated": time.time(),
            }

        # 自动注册/更新站点 (GPS 有效时)
        if lat != 0 or lon != 0:
            try:
                from app.server.models import upsert_station
                upsert_station(name=device, lat=float(lat), lon=float(lon),
                               alt=float(alt), device_name=device)
                logger.info("站点自动注册: %s (%.6f, %.6f)", device, lat, lon)
            except Exception as e:
                logger.warning("站点自动注册失败 %s: %s", device, e)

    # ── 缓存读取 (替代 DB 查询) ──

    def get_active_drones(self) -> list:
        """返回活跃无人机列表, 替代 get_active_drones() DB 查询"""
        now = time.time()
        with self._lock:
            # 清理过期无人机
            stale_ids = [
                did for did, d in self._drones.items()
                if now - d.get("_updated", 0) > self.STALE_SECONDS
            ]
            for did in stale_ids:
                del self._drones[did]

            return list(self._drones.values())

    def get_recent_alerts(self, limit: int = 50) -> list:
        with self._lock:
            return self._recent_alerts[:limit]

    def get_device_stats(self) -> dict:
        with self._lock:
            return dict(self._device_stats)

    def get_mqtt_connected(self) -> bool:
        return self._connected

    # ── 外部写入 (非 MQTT 模式下由 pipeline 回调写入) ──

    def upsert_drone(self, drone_id: str, lat: float, lon: float,
                     alt: float = 0, distance: float = None,
                     line_name: str = "", status: str = "active",
                     device: str = "", **kwargs):
        """非 MQTT 模式下由 pipeline 主动写入缓存"""
        now = time.time()
        with self._lock:
            drone = self._drones.get(drone_id, {})
            drone.update({
                "id": drone_id,
                "last_lat": lat,
                "last_lon": lon,
                "last_alt": alt,
                "min_distance": distance,
                "nearest_line": line_name,
                "status": status,
                "device": device,
                "_updated": now,
            })
            self._drones[drone_id] = drone

        if self._socketio:
            self._socketio.emit("drone_update", {
                "drone_id": drone_id,
                "lat": lat, "lon": lon, "alt": alt,
                "distance": distance,
                "nearest_line": line_name,
                "status": status,
                "device_name": device,
            })
