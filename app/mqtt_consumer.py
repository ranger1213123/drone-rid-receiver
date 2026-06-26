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
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt

from core.powerline import PowerLineManager
from core.cloud_alert import CloudAlertProcessor
from core.sms_gateway import create_sms_gateway

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

        # SMS 通知网关 (lazy init)
        self._sms_gateway = None
        self._sms_gateway_initialized = False
        self._sms_cooldown: dict = {}   # (station_name, drone_id, level) → timestamp
        self._sms_cooldown_sec = 300    # 同站点同无人机同等级 5 分钟内不重复发短信

    # ── 生命周期 ──

    def start(self):
        self._running = True

        client_id = f"consumer-{os.environ.get('HOSTNAME', 'unknown')}"
        self._client = mqtt.Client(
            client_id=client_id, clean_session=False, protocol=mqtt.MQTTv311,
        )
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)

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

        if self._buffer_size() >= self._batch_size:
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

    # ── SMS 通知 ──

    def _ensure_sms_gateway(self):
        """懒加载 SMS 网关，检查 DB 中 sms_enabled 开关"""
        if self._sms_gateway_initialized:
            return
        self._sms_gateway_initialized = True
        try:
            from app.server.models import get_setting
            sms_enabled = get_setting("sms_enabled", "false").lower() == "true"
        except Exception:
            sms_enabled = os.environ.get("SMS_ENABLED", "").lower() == "true"

        if not sms_enabled:
            logger.info("SMS 通知未启用 (sms_enabled=false)")
            return

        config = {
            "backhaul": {
                "sms": {
                    "enabled": True,
                    "provider": os.environ.get("SMS_PROVIDER", "simulated"),
                    "rate_limit_per_hour": int(os.environ.get("SMS_RATE_LIMIT_PER_HOUR", "10")),
                    "alibaba": {
                        "access_key": os.environ.get("SMS_ALIBABA_ACCESS_KEY", ""),
                        "access_secret": os.environ.get("SMS_ALIBABA_ACCESS_SECRET", ""),
                        "sign_name": os.environ.get("SMS_ALIBABA_SIGN_NAME", ""),
                        "template_code": os.environ.get("SMS_ALIBABA_TEMPLATE_CODE", ""),
                    },
                }
            }
        }
        try:
            self._sms_gateway = create_sms_gateway(config)
            logger.info("SMS 网关已初始化: provider=%s", config["backhaul"]["sms"]["provider"])
        except Exception as e:
            logger.error("SMS 网关初始化失败: %s", e)

    def _notify_station_personnel(self, station_name: str, alert: dict):
        """向站点负责人发送告警短信"""
        if not station_name:
            return

        self._ensure_sms_gateway()
        if self._sms_gateway is None:
            return

        drone_id = alert.get("drone_id", "")
        level = alert.get("level", "warning")
        distance = alert.get("distance", 0)
        line_name = alert.get("line_name", "")

        # SMS 去重: 同站点 + 同无人机 + 同等级在冷却期内不重复
        cooldown_key = (station_name, drone_id, level)
        now = time.time()
        last = self._sms_cooldown.get(cooldown_key, 0)
        if now - last < self._sms_cooldown_sec:
            return
        self._sms_cooldown[cooldown_key] = now

        try:
            from app.server.models import get_personnel_by_station
            personnel = get_personnel_by_station(station_name)
        except Exception as e:
            logger.warning("查询站点人员失败: station=%s, err=%s", station_name, e)
            return

        if not personnel:
            return

        phones = [p["phone"] for p in personnel if p.get("phone")]
        if not phones:
            return

        level_text = {"warning": "⚠️ 警告", "severe": "🚨 严重", "critical": "🔴 危急"}.get(level, level)
        message = (
            f"【无人机告警】{level_text}: 无人机 {drone_id} 接近 {line_name}，"
            f"距离 {distance:.0f}m，请立即处置。"
        )

        try:
            self._sms_gateway.send(phones, message)
            logger.info("SMS 通知已发送: station=%s phones=%d drone=%s level=%s",
                        station_name, len(phones), drone_id, level)
        except Exception as e:
            logger.error("SMS 发送失败: station=%s err=%s", station_name, e)

    def _notify_alerts_sms(self, alerts: list):
        """对一批告警按设备解析站点后发送 SMS"""
        self._ensure_device_tenant_map()

        # 周期性清理过期 SMS 冷却记录
        now = time.time()
        stale = [k for k, v in self._sms_cooldown.items() if now - v > self._sms_cooldown_sec * 2]
        for k in stale:
            self._sms_cooldown.pop(k, None)

        for alert in alerts:
            device_name = alert.get("device_name", "")
            ti = self._device_tenant.get(device_name, {})
            station_name = ti.get("station_name", "")
            if not station_name:
                continue
            try:
                self._notify_station_personnel(station_name, alert)
            except Exception as e:
                logger.error("SMS 通知异常: device=%s err=%s", device_name, e)

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

    def _buffer_raw(self, device_name: str, payload: dict):
        """处理 SDRTU raw 格式消息

        心跳 → 只更新设备在线状态
        数据 → 翻译为 report 格式, 注入距离计算 + 告警判定, 再复用 _buffer_report
        """
        now = datetime.now(timezone.utc)

        # 总是更新设备心跳
        dev_id = payload.get("devId", device_name)
        self._buffer['devices'][device_name] = {
            'name': device_name,
            'last_seen': now,
            'status': 'online',
        }

        report = self._translate_raw_to_report(device_name, payload)
        if report:
            # ── 白名单检查: 匹配的 SN 跳过告警 ──
            self._ensure_whitelist_loaded()
            if self._is_whitelisted(report["drone_id"]):
                # 仅更新位置, 不触发告警
                report["distance_to_line"] = None
                report["nearest_line"] = ""
                report["status"] = "active"
                self._buffer_report(device_name, report)
                return

            # ── 云端距离计算 + 告警 ──
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
                logger.debug("批量写入: devices=%d drones=%d alerts=%d",
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

        # SMS 通知 — DB 写入成功后才发送
        if db_ok and alerts:
            try:
                self._notify_alerts_sms(alerts)
            except Exception as e:
                logger.error("SMS 通知失败: %s", e)

        # 清理已不在线的无人机告警状态
        if drones:
            active_ids = {d['id'] for d in drones}
            self.alert_processor.cleanup_stale(active_ids)

        flushes_total.inc()
        flush_latency.observe(time.time() - _t0)

    def _batch_write_devices(self, session, devices: list):
        from app.server.models import Device
        from sqlalchemy.dialects.postgresql import insert as pg_insert

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

    def _batch_write_drones(self, session, drones: list):
        from app.server.models import Drone
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        merged = {}
        for d in drones:
            key = (d['id'], d['device_name'])
            if key not in merged:
                merged[key] = d
            else:
                merged[key].update({k: v for k, v in d.items() if v})

        stmt = pg_insert(Drone).values(list(merged.values()))
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
                'model': stmt.excluded.model,
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
