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
from datetime import datetime
from typing import Optional, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from storage.database import Database

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


class TokenManager:
    """JWT Token 管理器 — 自动获取、缓存、过期刷新"""

    def __init__(self, auth_url: str, device_name: str, device_secret: str,
                 expire_seconds: int = 86400):
        self._auth_url = auth_url
        self._device_name = device_name
        self._device_secret = device_secret
        self._expire_seconds = expire_seconds
        self._token: Optional[str] = None
        self._expires_at: float = 0

    def get_token(self) -> Optional[str]:
        """获取有效 token，过期自动刷新 (提前 5 分钟)"""
        if not self._token or time.time() > self._expires_at - 300:
            self._refresh()
        return self._token

    def force_refresh(self):
        """强制刷新 token (供外部在收到 401 后调用)"""
        self._refresh()

    def _refresh(self):
        try:
            resp = requests.post(
                self._auth_url,
                json={
                    "device_name": self._device_name,
                    "device_secret": self._device_secret,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._token = data.get("access_token")
                expires_in = data.get("expires_in", self._expire_seconds)
                self._expires_at = time.time() + expires_in
                logger.info("JWT token 已刷新，有效期 %ds", expires_in)
            else:
                logger.warning("JWT token 获取失败: HTTP %d", resp.status_code)
        except Exception as e:
            logger.warning("JWT token 刷新异常: %s", e)

    @property
    def has_token(self) -> bool:
        return self._token is not None


class BackhaulManager:
    """数据回传管理器"""

    def __init__(self, config: dict, beidou: BeidouDevice,
                 db: "Database", device_name: str = 'NW-F1',
                 token_manager: Optional[TokenManager] = None,
                 pl_manager=None):
        self._config = config
        self._beidou = beidou
        self._db = db
        self._device_name = device_name
        self._lock = threading.Lock()
        self._pl_manager = pl_manager  # 电力线管理器 (用于云端同步)

        # ── 通道状态 ──
        self._primary_status = ChannelStatus.OFFLINE
        self._beidou_status = ChannelStatus.OFFLINE
        self._active_channel = 'none'

        # ── SMS 网关 ──
        self._sms: SMSGateway = create_sms_gateway(config)
        sms_cfg = config.get('backhaul', {}).get('sms', {})
        self._sms_alert_phones = sms_cfg.get('alert_phones', [])
        self._sms_enabled = sms_cfg.get('enabled', False)
        self._send_sms_from_edge = sms_cfg.get('send_from_edge', True)

        # ── HTTP 配置 ──
        http_cfg = config.get('backhaul', {}).get('http', {})
        self._http_endpoint = http_cfg.get('endpoint', 'http://localhost:8080/api/report')
        self._http_timeout = http_cfg.get('timeout', 10)
        self._http_health_url = http_cfg.get('health_url', '')
        self._http_headers = http_cfg.get('headers', {'Content-Type': 'application/json'})
        self._retry_interval = http_cfg.get('retry_interval', 30)
        self._heartbeat_url = http_cfg.get('heartbeat_url', '')
        self._heartbeat_interval = http_cfg.get('heartbeat_interval', 30)

        # ── JWT 认证 (支持依赖注入) ──
        self._token_manager = token_manager
        if self._token_manager:
            logger.info("JWT 认证已启用")

        # ── 设备自身位置 ──
        pos_cfg = config.get('position', {})
        self._position_source = pos_cfg.get('source', 'beidou')
        self._fallback_lat = pos_cfg.get('manual_lat', 0)
        self._fallback_lon = pos_cfg.get('manual_lon', 0)
        self._fallback_alt = pos_cfg.get('manual_alt', 0)
        self._device_location = config.get('backhaul', {}).get('device_location', '')

        # ── 消息队列 ── (使用 SQLite outbox 持久化)

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
            'last_send_time': '',
        }

        # ── 电力线同步配置 ──
        self._pl_sync_config = config.get('backhaul', {}).get('power_line_sync', {})
        self._pl_sync_enabled = self._pl_sync_config.get('enabled', False)
        self._pl_sync_interval = self._pl_sync_config.get('interval', 300)
        self._pl_sync_url = self._pl_sync_config.get('sync_url', '')

        # ── 告警回调 (供 UI 使用) ──
        self._alert_callback: Optional[Callable] = None

    # ── 生命周期 ──

    def start(self):
        if self._running:
            return
        self._running = True
        self._db.drain_outbox_on_start()
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
        s = dict(self._stats)
        s['queued'] = self._db.outbox_pending_count()
        return s

    @property
    def queue_size(self) -> int:
        return self._db.outbox_pending_count()

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
            headers = dict(self._http_headers)
            if self._token_manager and self._token_manager.has_token:
                token = self._token_manager.get_token()
                if token:
                    headers['Authorization'] = f'Bearer {token}'
            resp = requests.post(url, json=payload, headers=headers,
                                 timeout=self._http_timeout)
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

        # 4G/有线不通 → SMS (仅边缘设备，云侧统一发则跳过)
        if self._send_sms_from_edge and self._sms_enabled and self._sms_alert_phones:
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
        headers = dict(self._http_headers)
        if self._token_manager and self._token_manager.has_token:
            token = self._token_manager.get_token()
            if token:
                headers['Authorization'] = f'Bearer {token}'

        try:
            resp = requests.post(
                self._http_endpoint, json=payload,
                headers=headers,
                timeout=self._http_timeout,
            )
            if resp.status_code == 401 and self._token_manager:
                # Token 过期，尝试刷新后重试一次
                self._token_manager.force_refresh()
                token = self._token_manager.get_token()
                if token:
                    headers['Authorization'] = f'Bearer {token}'
                    resp = requests.post(
                        self._http_endpoint, json=payload,
                        headers=headers,
                        timeout=self._http_timeout,
                    )
                else:
                    self._stats['http_failed'] += 1
                    return False

            if resp.status_code == 401:
                # 无 token_manager 或 retry 后仍 401
                self._stats['http_failed'] += 1
                return False

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

    def flush_if_needed(self):
        """周期性冲洗出队 (供 headless 主循环调用)"""
        if self._primary_status == ChannelStatus.ONLINE and self._db.outbox_pending_count() > 0:
            self._flush_queue()

    def inject_gps(self, lat: float, lon: float, alt: float):
        """注入外部 GPS 数据 (来自接收器的 GPS 流)"""
        self._fallback_lat = lat
        self._fallback_lon = lon
        self._fallback_alt = alt

    def _should_use_beidou(self, level: str) -> bool:
        levels = ['warning', 'severe', 'critical']
        min_idx = levels.index(self._bd_min_level) if self._bd_min_level in levels else 2
        cur_idx = levels.index(level) if level in levels else 0
        return cur_idx >= min_idx

    def _enqueue(self, payload: dict):
        self._db.insert_outbox(payload, '/api/report')

    def fetch_power_lines(self) -> int:
        """从云服务器同步电力线配置，返回更新条数"""
        if not self._pl_sync_enabled or not self._pl_manager:
            return 0

        url = self._pl_sync_url
        if not url:
            url = self._http_endpoint.replace('/api/report', '/api/powerlines/sync')
            url += f'?device_name={self._device_name}'

        try:
            headers = dict(self._http_headers)
            if self._token_manager and self._token_manager.has_token:
                token = self._token_manager.get_token()
                if token:
                    headers['Authorization'] = f'Bearer {token}'
            resp = requests.get(url, headers=headers, timeout=self._http_timeout)
            if resp.status_code == 200:
                data = resp.json()
                lines = data.get('lines', [])
                version = data.get('version', '')
                count = data.get('count', len(lines))
                if lines:
                    self._pl_manager.load_from_list(lines)
                    self._db.load_power_lines(lines)
                    logger.info("电力线已同步: %d 条 (version=%s)", count, version)
                return count
            elif resp.status_code == 401 and self._token_manager:
                self._token_manager.force_refresh()
                return self.fetch_power_lines()
        except Exception as e:
            logger.warning("电力线同步失败: %s", e)
        return 0

    def _enqueue_emergency(self, payload: dict):
        self._db.insert_outbox(payload, '/api/report_alert', priority=1)

    def _flush_queue(self):
        """通道恢复后补传积压数据 (从 outbox 读取)"""
        drained = 0
        for msg in self._db.get_pending_outbox(limit=50):
            msg_id = msg["id"]
            target_path = msg["target_path"]
            try:
                payload = json.loads(msg["payload"])
            except json.JSONDecodeError:
                self._db.mark_outbox_dead(msg_id)
                continue

            # 使用对应端点发送
            try:
                headers = dict(self._http_headers)
                if self._token_manager and self._token_manager.has_token:
                    token = self._token_manager.get_token()
                    if token:
                        headers['Authorization'] = f'Bearer {token}'

                resp = requests.post(
                    self._http_endpoint.replace('/api/report', target_path),
                    json=payload,
                    headers=headers,
                    timeout=self._http_timeout,
                )
                if resp.status_code < 500:
                    self._db.mark_outbox_sent(msg_id)
                    self._stats['http_sent'] += 1
                    self._stats['last_send_time'] = datetime.now().strftime('%H:%M:%S')
                    drained += 1
                else:
                    self._db.mark_outbox_failed(msg_id, f"HTTP {resp.status_code}")
                    self._stats['http_failed'] += 1
            except Exception as e:
                self._db.mark_outbox_failed(msg_id, str(e))
                self._stats['http_failed'] += 1
                self._primary_status = ChannelStatus.OFFLINE
                break  # 网络不通，停止尝试

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
        """健康检测循环 (通道检测 + 设备心跳 + 电力线同步)"""
        check_interval = self._config.get('backhaul', {}).get('health_check_interval', 15)
        last_heartbeat = 0
        last_pl_sync = 0
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

            # 电力线同步 (边缘设备从云拉取)
            if self._pl_sync_enabled and now - last_pl_sync >= self._pl_sync_interval:
                self.fetch_power_lines()
                last_pl_sync = now
