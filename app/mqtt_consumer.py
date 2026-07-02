"""
MQTT Consumer — 独立云端服务 (K8s Deployment)

订阅所有边缘设备上行数据，批量写入 PostgreSQL。

特性:
  - 共享订阅 $share/consumer/drone/+/+ (K8s 多副本负载均衡)
  - 内存 buffer → 定时批量 flush (INSERT ON CONFLICT DO UPDATE)
  - 监听 config_sync → 版本号比对 → 按需推送电力线配置
  - 健康检查 HTTP 端点

环境变量:
  MQTT_BROKER_HOST, MQTT_BROKER_PORT
  MQTT_TLS_CA_CERT, MQTT_TLS_CLIENT_CERT, MQTT_TLS_CLIENT_KEY
  DATABASE_URL
  BATCH_SIZE (default 100), FLUSH_INTERVAL (default 1.0s)
"""

import json
import logging
import os
import sys
from pathlib import Path

# 确保项目根目录在 Python path 中 (兼容直接 python app/mqtt_consumer.py 运行)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt

from core.powerline import PowerLineManager
from core.cloud_alert import CloudAlertProcessor
from core.webhook_notifier import create_webhook_notifier, WebhookNotifier
from core.parser.astm import _MSG_LENGTHS, _ASTM_DECODERS
from core.parser.types import ParsedRID, MSG_BASIC_ID, MSG_LOCATION, MSG_SELF_ID, MSG_SYSTEM, MSG_OPERATOR_ID

from prometheus_client import Counter, Gauge, Histogram, generate_latest, CollectorRegistry

# ── Prometheus 指标 ──
METRICS_REGISTRY = CollectorRegistry()

messages_total = Counter(
    'drone_rid_messages_total', 'MQTT 消息总数',
    ['topic_type'], registry=METRICS_REGISTRY,
)
alerts_generated = Counter(
    'drone_rid_alerts_total', '云告警生成总数',
    ['level'], registry=METRICS_REGISTRY,
)
flushes_total = Counter(
    'drone_rid_flushes_total', '批量写入次数',
    registry=METRICS_REGISTRY,
)
write_errors_total = Counter(
    'drone_rid_write_errors_total', '批量写入失败次数',
    registry=METRICS_REGISTRY,
)
buffer_devices = Gauge(
    'drone_rid_buffer_devices', 'buffer 中设备数',
    registry=METRICS_REGISTRY,
)
buffer_drones = Gauge(
    'drone_rid_buffer_drones', 'buffer 中无人机数',
    registry=METRICS_REGISTRY,
)
buffer_alerts = Gauge(
    'drone_rid_buffer_alerts', 'buffer 中告警数',
    registry=METRICS_REGISTRY,
)
flush_latency = Histogram(
    'drone_rid_flush_latency_seconds', 'flush 耗时 (秒)',
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
    registry=METRICS_REGISTRY,
)
batch_write_latency = Histogram(
    'drone_rid_batch_write_seconds', 'DB 批量写入耗时 (秒)',
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
    registry=METRICS_REGISTRY,
)
pl_loaded = Gauge(
    'drone_rid_power_lines_loaded', '已加载电力线数量',
    registry=METRICS_REGISTRY,
)
pl_loaded.set(0)

# ── 简单 HTTP 健康检查 (不依赖 Flask) ──
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
)
logger = logging.getLogger("mqtt-consumer")


class HealthHandler(BaseHTTPRequestHandler):
    """GET /health → buffer 状态; POST /publish → 发布 MQTT 消息"""
    consumer_ref = None

    def do_GET(self):
        if self.path == "/health":
            status = {"status": "ok"}
            if self.consumer_ref:
                status["buffer_size"] = self.consumer_ref.buffer_size()
                status["last_flush"] = self.consumer_ref.last_flush_time
            self._json(200, status)
        elif self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(generate_latest(METRICS_REGISTRY))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/publish":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                topic = body.get("topic", "")
                payload = body.get("payload", {})
                qos = body.get("qos", 1)
                if not topic:
                    self._json(400, {"error": "missing topic"})
                    return
                if self.consumer_ref and self.consumer_ref._client:
                    info = self.consumer_ref._client.publish(
                        topic, json.dumps(payload), qos=qos,
                    )
                    if info.rc == mqtt.MQTT_ERR_SUCCESS:
                        self._json(200, {"status": "ok", "topic": topic})
                    else:
                        self._json(502, {"error": f"publish failed: rc={info.rc}"})
                else:
                    self._json(503, {"error": "MQTT not connected"})
            except Exception as e:
                self._json(500, {"error": str(e)})
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass  # suppress HTTP access logs


class MqttConsumer:
    """独立 MQTT Consumer — 批量写入 PostgreSQL"""

    def __init__(self):
        self._broker_host = os.environ.get("MQTT_BROKER_HOST", "localhost")
        self._broker_port = int(os.environ.get("MQTT_BROKER_PORT", "8883"))
        self._broker_user = os.environ.get("MQTT_BROKER_USER", "")
        self._broker_pass = os.environ.get("MQTT_BROKER_PASS", "")
        self._ca_cert = os.environ.get("MQTT_TLS_CA_CERT", "")
        self._client_cert = os.environ.get("MQTT_TLS_CLIENT_CERT", "")
        self._client_key = os.environ.get("MQTT_TLS_CLIENT_KEY", "")
        self._batch_size = int(os.environ.get("BATCH_SIZE", "100"))
        self._flush_interval = float(os.environ.get("FLUSH_INTERVAL", "1.0"))

        # 内存 buffer
        self._buffer = {
            'devices': {},
            'drones': {},
            'status_updates': [],
            'alerts': [],
        }
        self._buffer_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self.last_flush_time = ""

        # 云端距离计算 + 告警
        self.pl_manager = PowerLineManager()
        self._pl_loaded = False
        self.alert_processor = CloudAlertProcessor(
            thresholds={"warning": 200, "severe": 100, "critical": 50},
            cooldown=30.0,
        )

        self._client: Optional[mqtt.Client] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # device → tenant 映射 (lazy load from DB)
        self._device_tenant: dict = {}
        self._device_tenant_loaded = False

        # 企业微信 Webhook 通知 — 按 URL 缓存实例
        self._webhook_cache: dict = {}

    # ── 生命周期 ──

    def start(self):
        self._running = True

        client_id = f"consumer-{os.environ.get('HOSTNAME', 'unknown')}"
        self._client = mqtt.Client(
            client_id=client_id, clean_session=False, protocol=mqtt.MQTTv311,
        )
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)

        # 用户名密码认证
        if self._broker_user:
            self._client.username_pw_set(self._broker_user, self._broker_pass)

        # mTLS
        if self._ca_cert and self._client_cert and self._client_key:
            self._client.tls_set(
                ca_certs=self._ca_cert,
                certfile=self._client_cert,
                keyfile=self._client_key,
            )

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        self._client.connect(self._broker_host, self._broker_port, keepalive=60)
        self._client.loop_start()

        # 预加载电力线 (避免首次 _buffer_raw 时在锁内查询 DB)
        self._ensure_pl_loaded()

        # 定时 flush 线程
        self._thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._thread.start()

        logger.info("MQTT Consumer 已启动: %s:%d (%s)",
                    self._broker_host, self._broker_port,
                    "mTLS" if self._ca_cert else "plain")

    def stop(self):
        self._running = False
        self._flush()  # flush remaining data
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("MQTT Consumer 已停止")

    # ── MQTT 回调 ──

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT 已连接")
            # 共享订阅 — K8s 多副本负载均衡
            client.subscribe("$share/consumer/drone/+/report", qos=1)
            client.subscribe("$share/consumer/drone/+/alert", qos=2)
            client.subscribe("$share/consumer/drone/+/heartbeat", qos=0)
            client.subscribe("$share/consumer/drone/+/status", qos=1)
            client.subscribe("$share/consumer/drone/+/raw", qos=1)   # DevelopLink SDRTU 透传
            client.subscribe("drone/+/config_sync", qos=1)  # 非共享，每个 consumer 都可回复
            # 普通订阅兜底 (非 K8s 环境 / broker 不支持共享订阅)
            client.subscribe("drone/+/raw", qos=1)
        else:
            logger.warning("MQTT 连接失败: rc=%d", rc)

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        # 去掉 $share/consumer/ 前缀
        if topic.startswith("$share/consumer/"):
            # format: $share/consumer/drone/{device}/{type}
            clean = topic[len("$share/consumer/"):]
        else:
            clean = topic

        parts = clean.split("/")
        if len(parts) < 3:
            return
        device_name = parts[1]
        msg_type = parts[2]

        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("JSON 解析失败: topic=%s", topic)
            return

        try:
            with self._buffer_lock:
                if msg_type == "report":
                    self._buffer_report(device_name, payload)
                elif msg_type == "alert":
                    self._buffer_alert(device_name, payload)
                elif msg_type == "heartbeat":
                    self._buffer_heartbeat(device_name, payload)
                elif msg_type == "status":
                    self._buffer_status(device_name, payload)
                elif msg_type == "config_sync":
                    self._handle_config_sync(device_name, payload)
                elif msg_type == "raw":
                    self._buffer_raw(device_name, payload)
        except Exception as e:
            logger.error("消息处理失败: topic=%s err=%s", topic, e)
            return

        # 指标: 按 topic 类型计数 + buffer 容量
        messages_total.labels(topic_type=msg_type).inc()
        buffer_devices.set(len(self._buffer['devices']))
        buffer_drones.set(len(self._buffer['drones']))
        buffer_alerts.set(len(self._buffer['alerts']))

        if self.buffer_size() >= self._batch_size:
            self._flush()

    # ── Buffer 逻辑 ──

    def _ensure_device_tenant_map(self):
        """懒加载 device_name → {tenant_id, station_name} 映射"""
        if self._device_tenant_loaded:
            return
        try:
            from app.server.models import get_session, Station
            sess = get_session()
            try:
                stations = sess.query(Station).filter(
                    Station.device_name.isnot(None),
                    Station.device_name != "",
                ).all()
                for s in stations:
                    self._device_tenant[s.device_name] = {
                        "tenant_id": s.tenant_id,
                        "station_name": s.name,
                        "webhook_url": s.webhook_url or "",
                    }
                self._device_tenant_loaded = True
                logger.info("Device→Tenant 映射已加载: %d 条", len(self._device_tenant))
            finally:
                sess.close()
        except Exception as e:
            logger.warning("加载 Device→Tenant 映射失败: %s", e)

    def _get_device_tenant_info(self, device_name: str) -> dict:
        """获取设备的 tenant_id 和 station_name"""
        self._ensure_device_tenant_map()
        return self._device_tenant.get(device_name, {})

    # ── 企业微信 Webhook 通知 ──

    def _get_station_webhook(self, station_name: str) -> str:
        """获取站点的 Webhook URL，优先站点级，兜底全局设置"""
        url = ""
        self._ensure_device_tenant_map()
        for dev_name, info in self._device_tenant.items():
            if info.get("station_name") == station_name:
                url = info.get("webhook_url", "")
                break
        if not url:
            try:
                from app.server.models import get_setting
                url = get_setting("webhook_url", "")
            except Exception:
                pass
        if not url:
            url = os.environ.get("WEBHOOK_URL", "")
        return url

    # Webhook 实例缓存 (按 URL 复用)
    _webhook_cache: dict = {}

    def _notify_webhook(self, station_name: str, alert: dict):
        """通过企业微信机器人发送告警通知 — 站点级 URL 优先"""
        if not station_name:
            return

        webhook_url = self._get_station_webhook(station_name)
        if not webhook_url:
            return

        # 复用缓存实例
        webhook = self._webhook_cache.get(webhook_url)
        if webhook is None:
            webhook = create_webhook_notifier(webhook_url)
            self._webhook_cache[webhook_url] = webhook

        drone_id = alert.get("drone_id", "")
        level = alert.get("level", "warning")
        distance = alert.get("distance", 0)
        line_name = alert.get("line_name", "")
        lat = alert.get("latitude", 0) or 0
        lon = alert.get("longitude", 0) or 0

        try:
            webhook.send_alert(
                station_name=station_name,
                drone_id=drone_id,
                level=level,
                distance=distance,
                line_name=line_name,
                lat=lat,
                lon=lon,
            )
        except Exception as e:
            logger.error("企微通知异常: device=%s err=%s", alert.get("device_name", ""), e)

    def _notify_alerts_webhook(self, alerts: list):
        """对一批告警按设备解析站点后发送企微通知"""
        self._ensure_device_tenant_map()

        for alert in alerts:
            device_name = alert.get("device_name", "")
            ti = self._device_tenant.get(device_name, {})
            station_name = ti.get("station_name", "")
            if not station_name:
                continue
            try:
                self._notify_webhook(station_name, alert)
            except Exception as e:
                logger.error("企微通知异常: device=%s err=%s", device_name, e)

    def _buffer_report(self, device_name: str, data: dict):
        now = datetime.now(timezone.utc)
        drone_id = data.get("drone_id", "")
        lat = data.get("latitude", 0)
        lon = data.get("longitude", 0)
        alt = data.get("altitude", 0)
        distance = data.get("distance_to_line")
        line_name = data.get("nearest_line", "")
        status = data.get("status", "active")

        # 云端兜底: 若边缘未附带最近电力线但坐标有效, 重新计算
        nearby_lines_json = ""
        if lat and lon and self.pl_manager and self.pl_manager.lines:
            try:
                if not line_name:
                    line, dist = self.pl_manager.find_nearest_line(lat, lon, alt)
                    if line:
                        distance = dist
                        line_name = line.name
                # 计算所有 300m 范围内的电力线 (用于轨迹记录)
                all_nearby = self.pl_manager.find_all_within(lat, lon, alt, 300.0)
                if all_nearby:
                    nearby_lines_json = json.dumps(
                        [{"line": l.name, "dist": round(d, 1)} for l, d in all_nearby],
                        ensure_ascii=False
                    )
            except Exception:
                pass

        ti = self._get_device_tenant_info(device_name)
        tenant_id = ti.get("tenant_id")
        station_name = ti.get("station_name", "")

        self._buffer['devices'][device_name] = {
            'name': device_name, 'last_seen': now, 'status': 'online',
            'lat': lat, 'lon': lon, 'alt': alt,
            'station_name': station_name, 'tenant_id': tenant_id,
        }
        if drone_id:
            key = (drone_id, device_name)
            existing = self._buffer['drones'].get(key, {})
            h_agl = data.get('height_agl')
            max_agl = h_agl
            if h_agl is not None:
                prev_max_agl = existing.get('max_alt_agl')
                if prev_max_agl is not None and prev_max_agl > h_agl:
                    max_agl = prev_max_agl
            else:
                max_agl = existing.get('max_alt_agl')
            max_asl = alt
            prev_max_asl = existing.get('max_alt_asl')
            if prev_max_asl is not None and prev_max_asl > alt:
                max_asl = prev_max_asl
            self._buffer['drones'][key] = {
                'id': drone_id, 'device_name': device_name,
                'last_seen': now, 'last_lat': lat, 'last_lon': lon, 'last_alt': alt,
                'last_speed': data.get('speed', 0),
                'last_heading': data.get('heading', 0),
                'rssi': data.get('rssi', 0),
                'status_code': data.get('status_code', 0),
                'height_agl': h_agl,
                'model': data.get('model', '') or existing.get('model', ''),
                'max_alt_agl': max_agl,
                'max_alt_asl': max_asl,
                'min_distance': distance,
                'nearest_line': line_name,
                'nearby_lines': nearby_lines_json,
                'status': status,
                'tenant_id': tenant_id,
            }
            if distance is not None:
                self._buffer['status_updates'].append(
                    (device_name, drone_id, distance, line_name, status)
                )

    def _buffer_alert(self, device_name: str, data: dict):
        now = datetime.now(timezone.utc)
        drone_id = data.get("drone_id", "")
        level = data.get("level", "warning")
        distance = data.get("distance", 0)
        line_name = data.get("nearest_line", "")
        lat = data.get("latitude", 0)
        lon = data.get("longitude", 0)
        alt = data.get("altitude", 0)

        # 白名单检查: 匹配 SN 的无人机不产生告警
        self._ensure_whitelist_loaded()
        if self._is_whitelisted(drone_id):
            return

        message = f"[{level}] {drone_id} 接近 {line_name} 距离{distance:.0f}m"

        ti = self._get_device_tenant_info(device_name)
        tenant_id = ti.get("tenant_id")
        station_name = ti.get("station_name", "")

        self._buffer['devices'][device_name] = {
            'name': device_name, 'last_seen': now, 'status': 'online',
            'lat': lat, 'lon': lon, 'alt': alt,
            'station_name': station_name, 'tenant_id': tenant_id,
        }
        if drone_id:
            key = (drone_id, device_name)
            self._buffer['drones'][key] = {
                'id': drone_id, 'device_name': device_name,
                'last_seen': now, 'last_lat': lat, 'last_lon': lon, 'last_alt': alt,
                'last_speed': 0, 'last_heading': 0,
                'rssi': 0, 'status_code': 0, 'height_agl': None,
                'model': data.get('model', ''),
                'min_distance': distance, 'nearest_line': line_name,
                'status': level,
                'tenant_id': tenant_id,
            }
        self._buffer['alerts'].append({
            'device_name': device_name, 'drone_id': drone_id,
            'timestamp': now, 'level': level, 'distance': distance,
            'line_name': line_name, 'message': message,
        })

    def _buffer_heartbeat(self, device_name: str, data: dict):
        now = datetime.now(timezone.utc)
        ti = self._get_device_tenant_info(device_name)
        self._buffer['devices'][device_name] = {
            'name': device_name, 'last_seen': now, 'status': 'online',
            'lat': data.get('device_lat', 0),
            'lon': data.get('device_lon', 0),
            'alt': data.get('device_alt', 0),
            'station_name': ti.get("station_name", ""),
            'tenant_id': ti.get("tenant_id"),
        }

    def _buffer_status(self, device_name: str, data: str):
        if isinstance(data, str) and data in ("online", "offline"):
            ti = self._get_device_tenant_info(device_name)
            self._buffer['devices'][device_name] = {
                'name': device_name, 'status': data,
                'station_name': ti.get("station_name", ""),
                'tenant_id': ti.get("tenant_id"),
            }

    # ── SDRTU Raw Format (DevelopLink / ESP32 透传) ──

    def _translate_raw_to_report(self, device_name: str, payload: dict) -> Optional[dict]:
        """将 ESP32 原始 JSON 翻译为内部 report 格式

        输入:
          心跳: {"devId":"EXD001","count":86}
          数据: {"devId":"EXD001","data":{"osid":"1581F...","RSSI":-72,
                 "Op_Lat":30.61517,"Op_Lon":104.06742,"Op_Alt":469,
                 "Lat":0,"Lon":0,"AltGeo":-1000,"Heading":361,"Speed":0,
                 "UAType":2,"Status":0,"UATime":0}}

        返回 report dict 或 None (心跳/无效数据)
        """
        inner = payload.get("data")
        if not isinstance(inner, dict):
            return None

        osid = (inner.get("osid") or "").strip()
        if not osid:
            return None

        # 位置: Op_Lat/Op_Lon/Op_Alt 优先, 回退到 drone GPS
        op_lat = inner.get("Op_Lat", 0.0) or 0.0
        op_lon = inner.get("Op_Lon", 0.0) or 0.0
        op_alt = inner.get("Op_Alt", 0.0) or 0.0
        drone_lat = inner.get("Lat", 0.0) or 0.0
        drone_lon = inner.get("Lon", 0.0) or 0.0
        alt_geo = inner.get("AltGeo", -1000.0)
        if alt_geo is None:
            alt_geo = -1000.0

        lat = op_lat if op_lat != 0.0 else drone_lat
        lon = op_lon if op_lon != 0.0 else drone_lon
        alt = op_alt if op_alt != 0.0 else (alt_geo if alt_geo != -1000.0 else 0.0)

        heading = inner.get("Heading", 0) or 0
        if heading > 360:
            heading = 0

        return {
            "drone_id": osid,
            "latitude": lat,
            "longitude": lon,
            "altitude": alt,
            "distance_to_line": None,      # 由云端距离计算填充
            "nearest_line": "",
            "status": "active",
            "device": device_name,
            "rssi": inner.get("RSSI", 0) or 0,
            "heading": heading,
            "speed": inner.get("Speed", 0) or 0,
            "status_code": inner.get("Status", 0) or 0,
            "height_agl": inner.get("Height"),
            "model": inner.get("Model", "") or "",
        }

    def _ensure_pl_loaded(self):
        """延迟加载电力线数据 (需 DB 就绪后调用)"""
        if self._pl_loaded:
            return
        try:
            from app.server.models import get_power_lines, close_db
            lines = get_power_lines()
            if lines:
                self.pl_manager.load_from_list(lines)
                pl_loaded.set(len(lines))
                logger.info("电力线加载完成: %d 条", len(lines))
        except Exception as e:
            logger.warning("电力线加载失败: %s", e)
        finally:
            try:
                from app.server.models import close_db
                close_db()
            except Exception:
                pass
        self._pl_loaded = True

    def _ensure_whitelist_loaded(self):
        if hasattr(self, '_whitelist_loaded') and self._whitelist_loaded:
            return
        self._whitelist_entries = []
        try:
            from app.server.models import get_whitelist, close_db
            self._whitelist_entries = get_whitelist()
        except Exception as e:
            logger.warning("白名单加载失败: %s", e)
        finally:
            try:
                close_db()
            except Exception:
                pass
        self._whitelist_loaded = True

    def _is_whitelisted(self, drone_id: str) -> bool:
        if not drone_id:
            return False
        for w in self._whitelist_entries:
            if w.get("match_mode") == "prefix":
                if drone_id.startswith(w["sn"]):
                    return True
            else:
                if drone_id == w["sn"]:
                    return True
        return False

    # ── SDRTU BLE Raw (hex 编码 ASTM F3411 二进制) ──

    def _try_parse_ble_packet(self, data: bytes, start: int):
        """从 data[start+2] 开始尝试解析 ASTM 消息 (跳过 counter+version 头).

        SDRTU 收到的 BLE 数据首字节高位常被置位 (如 0x80),
        所以不依赖 _parse_astm_pack 的 counter 范围检查, 直接按消息结构解析。

        遇到重复的 Basic ID 或 Location 消息时停止 (下一个 BLE 包边界)。
        返回 (ParsedRID, bytes_consumed)
        """
        result = ParsedRID()
        offset = start + 2
        msg_count = 0
        seen_types = set()

        while offset < len(data) - 1:
            header = data[offset]
            msg_type = header & 0x0F
            msg_len = _MSG_LENGTHS.get(msg_type, 0)

            if msg_len == 0 or msg_type > 5:
                break
            # 遇到重复的 Basic ID 或 Location → 下一个 BLE 包, 停止
            if msg_type in (MSG_BASIC_ID, MSG_LOCATION) and msg_type in seen_types:
                break
            if offset + 1 + msg_len > len(data):
                break

            payload = data[offset + 1: offset + 1 + msg_len]
            decoder = _ASTM_DECODERS.get(msg_type)

            if decoder and len(payload) >= 1:
                try:
                    parsed = decoder(payload)
                    if msg_type == MSG_BASIC_ID:
                        result.basic_id = parsed
                    elif msg_type == MSG_LOCATION:
                        result.location = parsed
                    elif msg_type == MSG_SELF_ID:
                        result.self_id = parsed
                    elif msg_type == MSG_SYSTEM:
                        result.system = parsed
                    elif msg_type == MSG_OPERATOR_ID:
                        result.operator_id = parsed
                except Exception:
                    pass

            seen_types.add(msg_type)
            offset += 1 + msg_len
            msg_count += 1

        consumed = offset - start if msg_count > 0 else 0
        return result, consumed

    def _extract_embedded_json(self, data: bytes, device_name: str) -> dict:
        """从混合二进制流中提取 ESP32 嵌入的 JSON 心跳."""
        import re
        try:
            text = data.decode('ascii', errors='ignore')
            for match in re.finditer(r'\{[^{}]*\}', text):
                try:
                    obj = json.loads(match.group())
                    if obj.get("devId"):
                        logger.debug("Embedded ESP32 heartbeat: dev=%s count=%s",
                                     obj.get("devId"), obj.get("count", 0))
                        return obj
                except (json.JSONDecodeError, ValueError):
                    pass
        except Exception:
            pass
        return {}

    def _validate_rid_result(self, drone_id: str, lat: float, lon: float) -> bool:
        """校验解析结果是否合理，过滤随机字节误匹配"""
        if not drone_id or len(drone_id) < 4:
            return False
        # drone_id 应以 ASCII 字母/数字为主
        ascii_count = sum(1 for c in drone_id if c.isascii() and (c.isalnum() or c in '-_'))
        if ascii_count < len(drone_id) * 0.8:
            return False
        # 坐标合理范围 (WGS-84 中国区域 ± 宽松边界)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return False
        if lat == 0.0 and lon == 0.0:
            return False
        return True

    def _translate_ble_raw_to_reports(self, device_name: str, payload: dict) -> list:
        """将 SDRTU hex 编码的 BLE 二进制解码为 report dict 列表.

        输入: {"dev_id":"EXD001","raw_hex":"80c0e0...","len":8192,"count":N,"type":"ble_raw"}
        输出: [report, ...]  每个 report 对应一架无人机的单次位置
        """
        hex_str = payload.get("raw_hex", "")
        if not hex_str:
            return []

        try:
            raw_bytes = bytes.fromhex(hex_str)
        except ValueError:
            logger.warning("Hex decode failed for device=%s", device_name)
            return []

        reports = []
        seen_ids = set()
        i = 0
        n = len(raw_bytes)

        while i < n - 4:
            # 版本号在 byte 1 低 4 位, ASTM F3411 协议版本 0-2
            version = raw_bytes[i + 1] & 0x0F if i + 1 < n else 0xFF
            if version > 3:
                i += 1
                continue
            # 第一个消息类型必须在 0-5 (进一步过滤随机字节)
            first_msg_type = raw_bytes[i + 2] & 0x0F if i + 2 < n else 0xFF
            if first_msg_type > 5:
                i += 1
                continue

            parsed, consumed = self._try_parse_ble_packet(raw_bytes, i)
            if parsed.drone_id and parsed.has_location:
                drone_id = parsed.drone_id
                loc = parsed.location
                if not self._validate_rid_result(drone_id, loc.latitude, loc.longitude):
                    i += 1
                    continue
                if drone_id not in seen_ids:
                    seen_ids.add(drone_id)
                    loc = parsed.location
                    alt = loc.altitude_geodetic if loc.altitude_geodetic != 0 else loc.altitude_pressure
                    reports.append({
                        "drone_id": drone_id,
                        "latitude": loc.latitude,
                        "longitude": loc.longitude,
                        "altitude": alt if alt != 0 else 0.0,
                        "distance_to_line": None,
                        "nearest_line": "",
                        "status": "active",
                        "device": device_name,
                        "rssi": 0,
                        "heading": getattr(loc, 'track_angle', 0) or 0,
                        "speed": loc.speed_horizontal,
                        "status_code": loc.status,
                        "height_agl": loc.height_agl,
                        "model": parsed.drone_model,
                    })
                i += max(consumed, 2)  # 跳过已解析的完整报文, 最少前进 1 字节
            else:
                i += 1

        return reports

    def _buffer_raw(self, device_name: str, payload: dict):
        """处理 SDRTU raw 格式消息

        type="ble_raw" → hex 解码 + ASTM F3411 解析 → report 管线
        type=其他/无 → 透传 ESP32 JSON 格式 (兼容旧版)
        """
        now = datetime.now(timezone.utc)

        # 总是更新设备心跳
        dev_id = payload.get("devId", payload.get("dev_id", device_name))
        device_entry = {
            'name': device_name,
            'last_seen': now,
            'status': 'online',
        }

        # 从 ESP32 payload 提取设备 GPS (Op_Lat/Op_Lon/Op_Alt)
        inner = payload.get("data")
        if isinstance(inner, dict):
            op_lat = inner.get("Op_Lat", 0) or 0
            op_lon = inner.get("Op_Lon", 0) or 0
            op_alt = inner.get("Op_Alt", 0) or 0
            device_entry.update({'lat': op_lat, 'lon': op_lon, 'alt': op_alt})

        # 如果已有设备 GPS (来自 heartbeat), 保留旧值
        existing = self._buffer['devices'].get(device_name, {})
        if not device_entry.get('lat') and not device_entry.get('lon'):
            for f in ('lat', 'lon', 'alt'):
                if existing.get(f):
                    device_entry[f] = existing[f]

        self._buffer['devices'][device_name] = device_entry

        # 根据 type 字段选择解析器
        if payload.get("type") == "ble_raw":
            # 提取嵌入的 JSON 心跳 (ESP32 interleaved)
            embedded = {}
            try:
                hex_str = payload.get("raw_hex", "")
                if hex_str:
                    embedded = self._extract_embedded_json(bytes.fromhex(hex_str), device_name)
            except Exception:
                pass
            reports = self._translate_ble_raw_to_reports(device_name, payload)
            if reports:
                logger.info("BLE raw: %d drones parsed, device=%s", len(reports), device_name)
            for report in reports:
                self._process_report(device_name, report)
            return

        report = self._translate_raw_to_report(device_name, payload)
        if report:
            self._process_report(device_name, report)

    def _process_report(self, device_name: str, report: dict):
        """共享管线: 白名单检查 → 距离计算 → 告警判定 → 写入 buffer"""
        self._ensure_whitelist_loaded()
        if self._is_whitelisted(report["drone_id"]):
            report["distance_to_line"] = None
            report["nearest_line"] = ""
            report["status"] = "active"
            self._buffer_report(device_name, report)
            return

        self._ensure_pl_loaded()
        if self.pl_manager and self.pl_manager.lines:
            line, dist = self.pl_manager.find_nearest_line(
                report["latitude"], report["longitude"], report["altitude"]
            )
            if line:
                report["distance_to_line"] = dist
                report["nearest_line"] = line.name
                level = self.alert_processor.process(
                    drone_id=report["drone_id"],
                    distance=dist,
                    line_name=line.name,
                    line_id=line.line_id,
                    drone_alt=report["altitude"],
                    drone_lat=report["latitude"],
                    drone_lon=report["longitude"],
                    device_name=device_name,
                )
                if level:
                    report["status"] = level
        self._buffer_report(device_name, report)

    def _handle_config_sync(self, device_name: str, payload: dict):
        """设备重连后版本号比对，按需推送电力线配置"""
        device_version = payload.get("config_version", "")
        try:
            from app.server.models import get_power_lines
            lines = get_power_lines(device_name=device_name)
            max_updated = max(
                (l.get("updated_at", "") for l in lines if l.get("updated_at")),
                default=""
            )
            if max_updated and max_updated != device_version:
                sync_payload = {
                    "lines": lines, "version": max_updated, "count": len(lines),
                }
                self._client.publish(
                    f"cmd/{device_name}/config",
                    json.dumps(sync_payload), qos=1,
                )
                logger.info("配置推送: %s (v%s → v%s)", device_name, device_version, max_updated)
        except Exception as e:
            logger.error("config_sync 处理失败: %s", e)
        finally:
            try:
                from app.server.models import close_db
                close_db()
            except Exception:
                pass

    def buffer_size(self) -> int:
        """当前 buffer 中的总条目数"""
        return (
            len(self._buffer['devices']) +
            len(self._buffer['drones']) +
            len(self._buffer['status_updates']) +
            len(self._buffer['alerts'])
        )

    # ── 批量写入 ──

    def _flush_loop(self):
        while self._running:
            time.sleep(self._flush_interval)
            self._flush()

    def _flush(self):
        """批量写入数据库，带锁防止并发 flush 导致告警重复"""
        if not self._flush_lock.acquire(blocking=False):
            return  # 另一个 flush 正在执行，跳过本轮
        try:
            self._flush_impl()
        finally:
            self._flush_lock.release()

    def _flush_impl(self):
        _t0 = time.time()

        with self._buffer_lock:
            devices = list(self._buffer['devices'].values())
            drones = list(self._buffer['drones'].values())
            status_updates = list(self._buffer['status_updates'])
            alerts = list(self._buffer['alerts'])

        # 从 CloudAlertProcessor 提取告警
        cloud_alerts = self.alert_processor.drain_alerts()
        if cloud_alerts:
            alerts.extend(cloud_alerts)
            for a in cloud_alerts:
                alerts_generated.labels(level=a['level']).inc()

        if not any([devices, drones, status_updates, alerts]):
            flushes_total.inc()
            return

        db_ok = False
        try:
            from app.server.models import get_session
            session = get_session()
            _db_t0 = time.time()
            try:
                if devices:
                    self._batch_write_devices(session, devices)
                if drones:
                    self._batch_write_drones(session, drones)
                if status_updates:
                    self._batch_write_status(session, status_updates)
                if alerts:
                    self._batch_write_alerts(session, alerts)
                session.commit()
                db_ok = True
                batch_write_latency.observe(time.time() - _db_t0)
                self.last_flush_time = datetime.now().isoformat()
                logger.info("批量写入: devices=%d drones=%d alerts=%d",
                            len(devices), len(drones), len(alerts))
            except Exception as e:
                session.rollback()
                write_errors_total.inc()
                logger.error("批量写入失败: %s", e)
            finally:
                session.close()
        except Exception as e:
            write_errors_total.inc()
            logger.error("数据库连接失败: %s", e)

        if db_ok:
            # 写入成功后才清空 buffer
            with self._buffer_lock:
                self._buffer['devices'] = {k: v for k, v in self._buffer['devices'].items() if k not in {d['name'] for d in devices}}
                self._buffer['drones'] = {k: v for k, v in self._buffer['drones'].items() if k not in {(d['id'], d['device_name']) for d in drones}}
                self._buffer['status_updates'] = self._buffer['status_updates'][len(status_updates):]
                self._buffer['alerts'] = self._buffer['alerts'][len(alerts):]
            # 更新 buffer gauges
            buffer_devices.set(len(self._buffer['devices']))
            buffer_drones.set(len(self._buffer['drones']))
            buffer_alerts.set(len(self._buffer['alerts']))

        # 企微通知 — DB 写入成功后才发送
        if db_ok and alerts:
            try:
                self._notify_alerts_webhook(alerts)
            except Exception as e:
                logger.error("企微通知失败: %s", e)

        # 清理已不在线的无人机告警状态
        if drones:
            active_ids = {d['id'] for d in drones}
            self.alert_processor.cleanup_stale(active_ids)

        flushes_total.inc()
        flush_latency.observe(time.time() - _t0)

    def _batch_write_devices(self, session, devices: list):
        from app.server.models import Device, DeviceSecret
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        import secrets

        # 去重 (同设备取最新的)
        merged = {}
        for d in devices:
            name = d['name']
            if name not in merged:
                merged[name] = d
            else:
                merged[name].update({k: v for k, v in d.items() if v})

        stmt = pg_insert(Device).values(list(merged.values()))
        stmt = stmt.on_conflict_do_update(
            index_elements=['name'],
            set_={
                'last_seen': stmt.excluded.last_seen,
                'lat': stmt.excluded.lat,
                'lon': stmt.excluded.lon,
                'alt': stmt.excluded.alt,
                'status': 'online',
                'station_name': stmt.excluded.station_name,
                'tenant_id': stmt.excluded.tenant_id,
            }
        )
        session.execute(stmt)

        # 自动配给: 新设备首次出现时, 自动创建 device_secrets 记录
        existing = {
            row[0] for row in
            session.query(DeviceSecret.device_name).filter(
                DeviceSecret.device_name.in_(list(merged.keys()))
            ).all()
        }
        from datetime import datetime, timezone
        for name in merged:
            if name not in existing:
                session.add(DeviceSecret(
                    device_name=name,
                    device_secret=secrets.token_hex(24),
                    created_at=datetime.now(timezone.utc),
                ))
                logger.info("自动配给设备: %s", name)

    def _batch_write_drones(self, session, drones: list):
        from app.server.models import Drone
        from sqlalchemy import func
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        merged = {}
        for d in drones:
            key = (d['id'], d['device_name'])
            if key not in merged:
                merged[key] = d
            else:
                merged[key].update({k: v for k, v in d.items() if v})

        # Drone 表没有 nearby_lines 列 (该字段仅写入 DronePosition 历史)
        drone_values = []
        for d in merged.values():
            d_clean = {k: v for k, v in d.items() if k != 'nearby_lines'}
            drone_values.append(d_clean)

        stmt = pg_insert(Drone).values(drone_values)
        stmt = stmt.on_conflict_do_update(
            index_elements=['id', 'device_name'],
            set_={
                'last_seen': stmt.excluded.last_seen,
                'last_lat': stmt.excluded.last_lat,
                'last_lon': stmt.excluded.last_lon,
                'last_alt': stmt.excluded.last_alt,
                'last_speed': stmt.excluded.last_speed,
                'last_heading': stmt.excluded.last_heading,
                'rssi': stmt.excluded.rssi,
                'status_code': stmt.excluded.status_code,
                'height_agl': stmt.excluded.height_agl,
                'model': func.coalesce(func.nullif(stmt.excluded.model, ''), Drone.model),
                'max_alt_agl': stmt.excluded.max_alt_agl,
                'max_alt_asl': stmt.excluded.max_alt_asl,
                'nearest_line': stmt.excluded.nearest_line,
                'min_distance': stmt.excluded.min_distance,
                'status': stmt.excluded.status,
                'tenant_id': stmt.excluded.tenant_id,
            }
        )
        session.execute(stmt)

        # 写入位置历史 (轨迹)
        from app.server.models import DronePosition
        now = datetime.now(timezone.utc)
        positions = []
        for d in merged.values():
            positions.append(DronePosition(
                drone_id=d['id'],
                device_name=d.get('device_name', ''),
                lat=d.get('last_lat', 0) or 0,
                lon=d.get('last_lon', 0) or 0,
                alt=d.get('last_alt', 0) or 0,
                distance_to_line=d.get('min_distance'),
                nearest_line=d.get('nearest_line', ''),
                nearby_lines=d.get('nearby_lines', ''),
                timestamp=d.get('last_seen', now),
            ))
        if positions:
            session.add_all(positions)

    def _batch_write_status(self, session, updates: list):
        """更新无人机距离/线路/状态"""
        for device_name, drone_id, distance, line_name, status in updates:
            from app.server.models import Drone
            drone = session.get(Drone, (drone_id, device_name))
            if drone:
                if drone.min_distance is None or distance < drone.min_distance:
                    drone.min_distance = distance
                drone.nearest_line = line_name
                drone.status = status

    def _batch_write_alerts(self, session, alerts: list):
        from app.server.models import Alert
        for a in alerts:
            session.add(Alert(**a))


# ── 入口 ──

def main():
    # 初始化数据库连接 (复用 models.py 的引擎)
    database_url = os.environ.get("DATABASE_URL", "sqlite:///data/center.db")
    pool_size = int(os.environ.get("DB_POOL_SIZE", "5"))
    pool_overflow = int(os.environ.get("DB_POOL_OVERFLOW", "10"))
    pool_timeout = int(os.environ.get("DB_POOL_TIMEOUT", "30"))
    from app.server.models import init_db
    init_db(database_url, pool_size=pool_size, pool_overflow=pool_overflow,
            pool_timeout=pool_timeout)
    logger.info("数据库已连接: %s (pool=%d+%d, timeout=%ds)",
                database_url.split("://")[0], pool_size, pool_overflow, pool_timeout)

    consumer = MqttConsumer()
    consumer.start()

    # HTTP 健康检查
    health_port = int(os.environ.get("HEALTH_PORT", "8080"))
    HealthHandler.consumer_ref = consumer
    httpd = HTTPServer(("0.0.0.0", health_port), HealthHandler)
    health_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    health_thread.start()
    logger.info("健康检查端点: http://0.0.0.0:%d/health", health_port)

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        consumer.stop()
        httpd.shutdown()


if __name__ == "__main__":
    main()
