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

        self._client: Optional[mqtt.Client] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

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

        if self._buffer_size() >= self._batch_size:
            self._flush()

    # ── Buffer 逻辑 ──

    def _buffer_report(self, device_name: str, data: dict):
        now = datetime.now(timezone.utc)
        drone_id = data.get("drone_id", "")
        lat = data.get("latitude", 0)
        lon = data.get("longitude", 0)
        alt = data.get("altitude", 0)
        distance = data.get("distance_to_line")
        line_name = data.get("nearest_line", "")
        status = data.get("status", "active")

        self._buffer['devices'][device_name] = {
            'name': device_name, 'last_seen': now, 'status': 'online',
            'lat': lat, 'lon': lon, 'alt': alt,
        }
        if drone_id:
            key = (drone_id, device_name)
            self._buffer['drones'][key] = {
                'id': drone_id, 'device_name': device_name,
                'last_seen': now, 'last_lat': lat, 'last_lon': lon, 'last_alt': alt,
                'status': 'active',
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
        message = f"[{level}] {drone_id} 接近 {line_name} 距离{distance:.0f}m"

        self._buffer['devices'][device_name] = {
            'name': device_name, 'last_seen': now, 'status': 'online',
            'lat': lat, 'lon': lon, 'alt': alt,
        }
        if drone_id:
            key = (drone_id, device_name)
            self._buffer['drones'][key] = {
                'id': drone_id, 'device_name': device_name,
                'last_seen': now, 'last_lat': lat, 'last_lon': lon, 'last_alt': alt,
                'min_distance': distance, 'nearest_line': line_name,
                'status': level,
            }
        self._buffer['alerts'].append({
            'device_name': device_name, 'drone_id': drone_id,
            'timestamp': now, 'level': level, 'distance': distance,
            'line_name': line_name, 'message': message,
        })

    def _buffer_heartbeat(self, device_name: str, data: dict):
        now = datetime.now(timezone.utc)
        self._buffer['devices'][device_name] = {
            'name': device_name, 'last_seen': now, 'status': 'online',
            'lat': data.get('device_lat', 0),
            'lon': data.get('device_lon', 0),
            'alt': data.get('device_alt', 0),
        }

    def _buffer_status(self, device_name: str, data: str):
        if isinstance(data, str) and data in ("online", "offline"):
            self._buffer['devices'][device_name] = {
                'name': device_name, 'status': data,
            }

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
        with self._buffer_lock:
            devices = list(self._buffer['devices'].values())
            drones = list(self._buffer['drones'].values())
            status_updates = list(self._buffer['status_updates'])
            alerts = list(self._buffer['alerts'])
            self._buffer = {
                'devices': {}, 'drones': {},
                'status_updates': [], 'alerts': [],
            }

        if not any([devices, drones, status_updates, alerts]):
            return

        try:
            from app.server.models import get_session
            session = get_session()
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
                self.last_flush_time = datetime.now().isoformat()
                logger.debug("批量写入: devices=%d drones=%d alerts=%d",
                            len(devices), len(drones), len(alerts))
            except Exception as e:
                session.rollback()
                logger.error("批量写入失败: %s", e)
            finally:
                session.close()
        except Exception as e:
            logger.error("数据库连接失败: %s", e)

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
                'status': stmt.excluded.status,
            }
        )
        session.execute(stmt)

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
    from app.server.models import init_db
    init_db(database_url)
    logger.info("数据库已连接: %s", database_url.split("://")[0])

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
