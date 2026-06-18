"""
MQTT 客户端封装 — 边缘设备侧 (mTLS 认证)

MqttChannel 负责:
  - mTLS 连接 EMQX broker
  - 上行: publish data/alert/heartbeat
  - 下行: subscribe cmd/{device}/config, cmd/broadcast
  - LWT 遗嘱消息: drone/{device}/status = "offline"
  - 重连后 config_sync: 上报本地配置版本号
"""

import json
import logging
import threading
import uuid
from typing import Callable, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


def _expand_topic(template: str, device_name: str) -> str:
    return template.replace("{device_name}", device_name)


class MqttChannel:
    """边缘设备 MQTT 通信通道 (mTLS)"""

    def __init__(
        self,
        broker_host: str,
        broker_port: int,
        device_name: str,
        ca_cert_path: str,
        client_cert_path: str,
        client_key_path: str,
        keepalive: int = 60,
        reconnect_delay_min: int = 1,
        reconnect_delay_max: int = 120,
        on_config: Callable[[dict], None] = None,
        on_broadcast: Callable[[dict], None] = None,
        get_config_version: Callable[[], str] = None,
    ):
        self._device_name = device_name
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._keepalive = keepalive
        self._ca_cert = ca_cert_path
        self._client_cert = client_cert_path
        self._client_key = client_key_path
        self._on_config = on_config
        self._on_broadcast = on_broadcast
        self._get_config_version = get_config_version

        self._client: Optional[mqtt.Client] = None
        self._connected = False
        self._running = False

        client_id = f"{device_name}-{uuid.uuid4().hex[:8]}"
        self._client = mqtt.Client(
            client_id=client_id, clean_session=False, protocol=mqtt.MQTTv311,
        )
        self._client.reconnect_delay_set(
            min_delay=reconnect_delay_min, max_delay=reconnect_delay_max,
        )

        # LWT 遗嘱消息
        lwt_topic = _expand_topic("drone/{device_name}/status", device_name)
        self._client.will_set(lwt_topic, payload="offline", qos=1, retain=True)

        # mTLS
        self._client.tls_set(
            ca_certs=self._ca_cert,
            certfile=self._client_cert,
            keyfile=self._client_key,
        )

        # 回调
        self._client.on_connect = self._on_connect_cb
        self._client.on_disconnect = self._on_disconnect_cb
        self._client.on_message = self._on_message_cb

        # 统计
        self._stats = {"mqtt_sent": 0, "mqtt_failed": 0, "last_send_time": ""}

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    # ── 生命周期 ──

    def start(self):
        self._running = True
        try:
            self._client.connect(self._broker_host, self._broker_port, self._keepalive)
        except Exception as e:
            logger.warning("MQTT 初始连接失败 (%s), paho 将自动重试", e)
        self._client.loop_start()
        logger.info("MQTT channel 已启动: %s:%d (mTLS)", self._broker_host, self._broker_port)

    def stop(self):
        """优雅关闭 — 发布下线状态"""
        self._running = False
        if self._client:
            offline_topic = _expand_topic("drone/{device_name}/status", self._device_name)
            try:
                self._client.publish(offline_topic, "offline", qos=1, retain=True)
            except Exception:
                pass
            self._client.loop_stop()
            self._client.disconnect()
        logger.info("MQTT channel 已停止")

    # ── MQTT 回调 ──

    def _on_connect_cb(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            logger.info("MQTT 已连接 (mTLS), client=%s", client._client_id)

            # 发布上线状态
            online_topic = _expand_topic("drone/{device_name}/status", self._device_name)
            client.publish(online_topic, "online", qos=1, retain=True)

            # 订阅下行 topic
            config_topic = _expand_topic("cmd/{device_name}/config", self._device_name)
            client.subscribe(config_topic, qos=1)
            client.subscribe("cmd/broadcast", qos=1)

            # 上报当前配置版本号 (触发云端比对)
            version = ""
            if self._get_config_version:
                version = self._get_config_version() or ""
            sync_topic = _expand_topic("drone/{device_name}/config_sync", self._device_name)
            client.publish(sync_topic, json.dumps({"config_version": version}), qos=1)
        else:
            self._connected = False
            logger.warning("MQTT 连接失败: rc=%d", rc)

    def _on_disconnect_cb(self, client, userdata, rc):
        self._connected = False
        if rc == 0:
            logger.info("MQTT 正常断开")
        else:
            logger.warning("MQTT 意外断开 (rc=%d), paho 将自动重连", rc)

    def _on_message_cb(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("MQTT 下行消息 JSON 无效: topic=%s", msg.topic)
            return

        if msg.topic == "cmd/broadcast" and self._on_broadcast:
            self._on_broadcast(payload)
        elif msg.topic.startswith("cmd/") and msg.topic.endswith("/config") and self._on_config:
            self._on_config(payload)

    # ── 上行发布 ──

    def publish(self, topic_suffix: str, payload: dict, qos: int = 1) -> bool:
        """发布上行消息

        Args:
            topic_suffix: "report" | "alert" | "heartbeat" (自动展开为 drone/{device}/{suffix})
            payload: 消息体
            qos: QoS 级别
        Returns: True if published, False otherwise
        """
        topic = _expand_topic(f"drone/{{device_name}}/{topic_suffix}", self._device_name)
        try:
            info = self._client.publish(topic, json.dumps(payload), qos=qos)
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                self._stats["mqtt_sent"] += 1
                from datetime import datetime
                self._stats["last_send_time"] = datetime.now().strftime("%H:%M:%S")
                return True
            else:
                self._stats["mqtt_failed"] += 1
                logger.debug("MQTT publish 失败: rc=%d topic=%s", info.rc, topic)
                return False
        except Exception as e:
            self._stats["mqtt_failed"] += 1
            logger.warning("MQTT publish 异常: %s topic=%s", e, topic)
            return False
