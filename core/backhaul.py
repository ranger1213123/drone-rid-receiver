"""
数据回传管理器 — 4G/有线双通道 + 北斗短报文应急

通道优先级:
  1. 4G/有线 (HTTP POST) — 常规数据回传
  2. 北斗短报文 — 4G/有线中断时，发送关键告警

工作模式:
  - 正常: 通过 HTTP 上报无人机数据到中心服务器
  - 降级: 4G/有线中断时，critical 级别告警通过北斗短报文发送
  - 恢复: 4G/有线恢复后，自动切回并补传积压数据
"""

import json
import threading
import time
import queue
from datetime import datetime
from typing import Optional, Callable

import requests

from logging_config import get_logger
from core.beidou import BeidouDevice, format_emergency_message
from core.sms_gateway import SMSGateway, create_sms_gateway

logger = get_logger(__name__)


class ChannelStatus:
    """通信通道状态"""
    OFFLINE = 0
    ONLINE = 1
    DEGRADED = 2  # 降级 (仅北斗可用)


class BackhaulManager:
    """数据回传管理器"""

    def __init__(self, config: dict, beidou: BeidouDevice,
                 device_name: str = 'NW-F1'):
        self._config = config
        self._beidou = beidou
        self._device_name = device_name
        self._lock = threading.Lock()

        # ── 通道状态 ──
        self._primary_status = ChannelStatus.OFFLINE
        self._beidou_status = ChannelStatus.OFFLINE
        self._active_channel = 'none'

        # ── SMS 网关 ──
        self._sms: SMSGateway = create_sms_gateway(config)
        sms_cfg = config.get('backhaul', {}).get('sms', {})
        self._sms_alert_phones = sms_cfg.get('alert_phones', [])
        self._sms_enabled = sms_cfg.get('enabled', False)

        # ── HTTP 配置 ──
        http_cfg = config.get('backhaul', {}).get('http', {})
        self._http_endpoint = http_cfg.get('endpoint', 'http://localhost:8080/api/report')
        self._http_timeout = http_cfg.get('timeout', 10)
        self._http_health_url = http_cfg.get('health_url', '')
        self._http_headers = http_cfg.get('headers', {'Content-Type': 'application/json'})
        self._retry_interval = http_cfg.get('retry_interval', 30)
        self._heartbeat_url = http_cfg.get('heartbeat_url', '')
        self._heartbeat_interval = http_cfg.get('heartbeat_interval', 30)

        # ── 设备自身位置 ──
        pos_cfg = config.get('position', {})
        self._position_source = pos_cfg.get('source', 'beidou')
        self._fallback_lat = pos_cfg.get('manual_lat', 0)
        self._fallback_lon = pos_cfg.get('manual_lon', 0)
        self._fallback_alt = pos_cfg.get('manual_alt', 0)
        self._device_location = config.get('backhaul', {}).get('device_location', '')

        # ── 消息队列 ──
        queue_cfg = config.get('backhaul', {}).get('queue', {})
        self._queue: queue.Queue = queue.Queue(maxsize=queue_cfg.get('max_size', 1000))
        self._emergency_queue: queue.Queue = queue.Queue(maxsize=200)

        # ── 北斗应急配置 ──
        bd_cfg = config.get('backhaul', {}).get('beidou', {})
        self._bd_emergency_receiver = bd_cfg.get('emergency_receiver_id', '')
        self._bd_min_level = bd_cfg.get('min_alert_level', 'critical')

        # ── 后台线程 ──
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._health_thread: Optional[threading.Thread] = None

        # ── 统计 ──
        self._stats = {
            'http_sent': 0, 'http_failed': 0,
            'beidou_sent': 0, 'beidou_failed': 0,
            'queued': 0, 'last_send_time': '',
        }

        # ── 告警回调 (供 UI 使用) ──
        self._alert_callback: Optional[Callable] = None

    # ── 生命周期 ──

    def start(self):
        if self._running:
            return
        self._running = True
        self._start_beidou()
        self._check_primary()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()
        logger.info(f"数据回传管理器已启动 | 主通道: {self._http_endpoint} | 应急: 北斗短报文")

    def stop(self):
        self._running = False
        for t in [self._thread, self._health_thread]:
            if t and t.is_alive():
                t.join(timeout=5)
        self._beidou.close()
        logger.info("数据回传管理器已停止")

    def _start_beidou(self):
        if self._beidou.open():
            self._beidou_status = ChannelStatus.ONLINE

    # ── 通道检测 ──

    def _check_primary(self) -> bool:
        """检测 4G/有线主通道是否可用"""
        url = self._http_health_url or self._http_endpoint
        try:
            resp = requests.head(url, timeout=5)
            ok = resp.status_code < 500
        except Exception:
            ok = False

        old = self._primary_status
        self._primary_status = ChannelStatus.ONLINE if ok else ChannelStatus.OFFLINE

        if ok:
            self._active_channel = '4g_wired'
            if old != ChannelStatus.ONLINE:
                logger.info("4G/有线通道已恢复")
        else:
            if self._beidou_status == ChannelStatus.ONLINE:
                self._active_channel = 'beidou'
            else:
                self._active_channel = 'none'
            if old != ChannelStatus.OFFLINE:
                logger.warning("4G/有线通道中断，切换至北斗应急模式")
        return ok

    @property
    def primary_online(self) -> bool:
        return self._primary_status == ChannelStatus.ONLINE

    @property
    def beidou_online(self) -> bool:
        return self._beidou_status == ChannelStatus.ONLINE

    @property
    def active_channel(self) -> str:
        return self._active_channel

    @property
    def channel_status(self) -> str:
        if self._primary_status == ChannelStatus.ONLINE:
            return '4g_wired'
        if self._beidou_status == ChannelStatus.ONLINE:
            return 'beidou_emergency'
        return 'offline'

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    def set_alert_callback(self, cb: Callable):
        self._alert_callback = cb

    def _get_device_position(self) -> tuple:
        """获取设备自身位置 (优先北斗/GPS, 回落配置文件)"""
        if self._position_source == 'beidou' and self._beidou:
            pos = self._beidou.get_position()
            if pos and pos.has_fix:
                return pos.latitude, pos.longitude, pos.altitude
        return self._fallback_lat, self._fallback_lon, self._fallback_alt

    def send_heartbeat(self) -> bool:
        """发送设备心跳 (含自身定位) 到中心服务器"""
        url = self._heartbeat_url or self._http_endpoint.replace('/api/report', '/api/heartbeat')
        dev_lat, dev_lon, dev_alt = self._get_device_position()
        payload = {
            'device': self._device_name,
            'device_lat': dev_lat,
            'device_lon': dev_lon,
            'device_alt': dev_alt,
            'location': self._device_location,
            'active_channel': self._active_channel,
            'timestamp': datetime.now().isoformat(),
        }
        try:
            resp = requests.post(url, json=payload, timeout=self._http_timeout)
            return resp.status_code < 500
        except Exception:
            return False

    # ── 数据上报 API ──

    def report_drone(self, drone_id: str, lat: float, lon: float, alt: float,
                     distance: float, line_name: str, status: str) -> bool:
        """上报无人机数据到中心服务器"""
        payload = {
            'device': self._device_name,
            'drone_id': drone_id,
            'latitude': lat, 'longitude': lon, 'altitude': alt,
            'distance_to_line': distance,
            'nearest_line': line_name,
            'status': status,
            'timestamp': datetime.now().isoformat(),
        }
        return self._send_http(payload)

    def report_alert(self, drone_id: str, level: str, distance: float,
                     line_name: str, lat: float, lon: float,
                     alt: float, drone_model: str = "",
                     takeoff_lat: float = None,
                     takeoff_lon: float = None) -> str:
        """上报告警事件

        Returns: 'http' | 'beidou' | 'queued' | 'dropped'
        """
        payload = {
            'device': self._device_name,
            'type': 'alert',
            'drone_id': drone_id,
            'level': level,
            'distance': distance,
            'nearest_line': line_name,
            'latitude': lat, 'longitude': lon, 'altitude': alt,
            'drone_model': drone_model,
            'takeoff_lat': takeoff_lat, 'takeoff_lon': takeoff_lon,
            'timestamp': datetime.now().isoformat(),
        }
        sent = self._send_http(payload)
        if sent:
            return 'http'

        # 4G/有线不通 → SMS (专责人员)
        if self._sms_enabled and self._sms_alert_phones:
            # 组装短信: SN + 机型 + 高度 + 距离 + 位置 + 起飞点
            model_part = f"机型:{drone_model} " if drone_model else ""
            alt_part = f"高度:{alt:.0f}m " if alt else ""
            sms_parts = [
                f"[{level.upper()}]无人机告警",
                f"SN:{drone_id}",
                f"{model_part}",
                f"距 {line_name} {distance:.0f}m",
                f"{alt_part}",
                f"位置:({lat:.5f},{lon:.5f})",
            ]
            if takeoff_lat is not None and takeoff_lon is not None:
                sms_parts.append(f"起飞:({takeoff_lat:.5f},{takeoff_lon:.5f})")
            sms_msg = " ".join(sms_parts)
            if self._sms.send(self._sms_alert_phones, sms_msg):
                logger.info("SMS 告警已发送")

        # SMS 不通 → 北斗应急 (仅 critical)
        if self._beidou_status == ChannelStatus.ONLINE:
            if self._should_use_beidou(level):
                msg = format_emergency_message(
                    self._device_name, drone_id, level, distance, line_name,
                    lat, lon, alt,
                )
                if self._beidou.send_message(self._bd_emergency_receiver, msg):
                    self._stats['beidou_sent'] += 1
                    return 'beidou'
                else:
                    self._stats['beidou_failed'] += 1

        # 都不可用 → 入队等待
        self._enqueue(payload)
        self._enqueue_emergency(payload)
        return 'queued'

    # ── 内部方法 ──

    def _send_http(self, payload: dict) -> bool:
        try:
            resp = requests.post(
                self._http_endpoint, json=payload,
                headers=self._http_headers,
                timeout=self._http_timeout,
            )
            ok = resp.status_code < 500
            if ok:
                self._stats['http_sent'] += 1
                self._stats['last_send_time'] = datetime.now().strftime('%H:%M:%S')
            else:
                self._stats['http_failed'] += 1
            return ok
        except Exception:
            self._stats['http_failed'] += 1
            self._primary_status = ChannelStatus.OFFLINE
            return False

    def _should_use_beidou(self, level: str) -> bool:
        levels = ['warning', 'severe', 'critical']
        min_idx = levels.index(self._bd_min_level) if self._bd_min_level in levels else 2
        cur_idx = levels.index(level) if level in levels else 0
        return cur_idx >= min_idx

    def _enqueue(self, payload: dict):
        try:
            self._queue.put_nowait(payload)
            self._stats['queued'] = max(self._stats['queued'], self._queue.qsize())
        except queue.Full:
            # 丢弃最旧的消息
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(payload)
            except queue.Full:
                pass

    def _enqueue_emergency(self, payload: dict):
        try:
            self._emergency_queue.put_nowait(payload)
        except queue.Full:
            try:
                self._emergency_queue.get_nowait()
                self._emergency_queue.put_nowait(payload)
            except queue.Full:
                pass

    def _flush_queue(self):
        """通道恢复后补传积压数据"""
        drained = 0
        # 优先发送紧急队列
        while not self._emergency_queue.empty():
            try:
                payload = self._emergency_queue.get_nowait()
                self._send_http(payload)
                drained += 1
            except queue.Empty:
                break

        while not self._queue.empty():
            try:
                payload = self._queue.get_nowait()
                self._send_http(payload)
                drained += 1
            except queue.Empty:
                break

        if drained > 0:
            logger.info("已补传 %d 条积压数据", drained)

    # ── 后台循环 ──

    def _run_loop(self):
        """主循环：处理积压队列"""
        while self._running:
            time.sleep(5)
            if self._primary_status == ChannelStatus.ONLINE:
                self._flush_queue()

    def _health_loop(self):
        """健康检测循环 (通道检测 + 设备心跳)"""
        check_interval = self._config.get('backhaul', {}).get('health_check_interval', 15)
        last_heartbeat = 0
        while self._running:
            time.sleep(min(check_interval, self._heartbeat_interval))
            was_offline = self._primary_status == ChannelStatus.OFFLINE
            self._check_primary()
            if was_offline and self._primary_status == ChannelStatus.ONLINE:
                if self._alert_callback:
                    self._alert_callback('info', '4G/有线通道已恢复，数据回传正常')

            # 检测北斗信号
            if self._beidou.connected:
                sig = self._beidou.check_signal()
                self._beidou.signal_strength = sig
                self._beidou_status = ChannelStatus.ONLINE if sig > 0 else ChannelStatus.OFFLINE

            # 设备心跳 (含自身定位)
            now = time.time()
            if now - last_heartbeat >= self._heartbeat_interval:
                self.send_heartbeat()
                last_heartbeat = now
