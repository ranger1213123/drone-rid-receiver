"""
北斗/GPS 定位与短报文 (Beidou RDSS) 应急通信模块

功能:
  1. 定位 — NMEA 0183 语句解析 (GGA / RMC)，获取设备自身经纬度+高度
  2. 短报文 — Beidou RDSS 应急通信, RS232/RS485 串口
    - BD1 短报文: 最大 78 字节/条 (≈ 39 个中文)
    - BD3 区域短报文: 最大 ~1000 字节/条

指令格式: $CMD,params*XX\r\n  (XX = XOR 校验)
"""

import threading
import time
import math
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from logging_config import get_logger

logger = get_logger(__name__)

BD1_MAX_BYTES = 78
BD3_MAX_BYTES = 1000


# ── NMEA 0183 定位解析 ──

_NMEA_LAT_REGEX = re.compile(r'^(\d{2})(\d{2}\.\d+)$')
_NMEA_LON_REGEX = re.compile(r'^(\d{3})(\d{2}\.\d+)$')


def _parse_nmea_lat(value: str, hemisphere: str) -> Optional[float]:
    """将 NMEA 纬度字符串 (ddmm.mmmm + N/S) 转为十进制度"""
    if not value or not hemisphere:
        return None
    m = _NMEA_LAT_REGEX.match(value)
    if not m:
        return None
    deg = float(m.group(1)) + float(m.group(2)) / 60.0
    return -deg if hemisphere.upper() == 'S' else deg


def _parse_nmea_lon(value: str, hemisphere: str) -> Optional[float]:
    """将 NMEA 经度字符串 (dddmm.mmmm + E/W) 转为十进制度"""
    if not value or not hemisphere:
        return None
    m = _NMEA_LON_REGEX.match(value)
    if not m:
        return None
    deg = float(m.group(1)) + float(m.group(2)) / 60.0
    return -deg if hemisphere.upper() == 'W' else deg


def _xor_checksum(data: str) -> str:
    checksum = 0
    for c in data:
        checksum ^= ord(c)
    return f"{checksum:02X}"


def _verify_nmea_checksum(sentence: str) -> bool:
    """验证 NMEA 语句校验和"""
    if '*' not in sentence:
        return False
    body, checksum_str = sentence[1:].split('*', 1)
    expected = _xor_checksum(body)
    try:
        return expected.upper() == checksum_str.strip().upper()
    except Exception:
        return False


@dataclass
class DevicePosition:
    """设备自身位置 (从北斗/GPS 模块获取)"""
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0        # 海拔高度 (m, 大地高)
    fix_quality: int = 0         # 0=无定位, 1=GPS, 2=DGPS, 4=RTK固定, 5=RTK浮动
    satellites: int = 0          # 可见卫星数
    speed_knots: float = 0.0     # 地面速度 (节)
    track_angle: float = 0.0     # 航迹角 (度)
    timestamp: str = ""          # NMEA 时间戳 (UTC)
    last_update: float = 0.0     # 最后更新时间 (time.time())

    @property
    def has_fix(self) -> bool:
        return self.fix_quality > 0

    @property
    def age_seconds(self) -> float:
        if not self.last_update:
            return float('inf')
        return time.time() - self.last_update


def parse_nmea_sentence(sentence: str) -> Optional[DevicePosition]:
    """解析单条 NMEA 0183 语句，仅处理 GGA 和 RMC"""
    sentence = sentence.strip()
    if not sentence.startswith('$') or len(sentence) < 10:
        return None

    if not _verify_nmea_checksum(sentence):
        return None

    # 去掉 $ 和 *checksum
    body = sentence[1:sentence.index('*')]
    parts = body.split(',')
    if len(parts) < 3:
        return None

    talker_sentence = parts[0]
    # talker 前2字符为设备标识 (GP/BD/GN/GL/GA)
    if len(talker_sentence) < 5:
        return None
    msg_type = talker_sentence[2:]  # GGA, RMC, etc.

    pos = DevicePosition()

    if msg_type == 'GGA':
        # $--GGA,time,lat,N,lon,E,quality,sats,hdop,alt,M,geoid,M,age,ref*cs
        if len(parts) < 10:
            return None
        pos.timestamp = parts[1]
        pos.latitude = _parse_nmea_lat(parts[2], parts[3]) or 0.0
        pos.longitude = _parse_nmea_lon(parts[4], parts[5]) or 0.0
        try:
            pos.fix_quality = int(parts[6])
        except (ValueError, IndexError):
            pos.fix_quality = 0
        try:
            pos.satellites = int(parts[7])
        except (ValueError, IndexError):
            pos.satellites = 0
        try:
            pos.altitude = float(parts[9])
        except (ValueError, IndexError):
            pos.altitude = 0.0
        pos.last_update = time.time()
        return pos

    elif msg_type == 'RMC':
        # $--RMC,time,status,lat,N,lon,E,speed,track,date,mag,magE,mode*cs
        if len(parts) < 9:
            return None
        status = parts[2]
        if status.upper() != 'A':
            return None  # 无效数据
        pos.timestamp = parts[1]
        pos.latitude = _parse_nmea_lat(parts[3], parts[4]) or 0.0
        pos.longitude = _parse_nmea_lon(parts[5], parts[6]) or 0.0
        try:
            pos.speed_knots = float(parts[7])
        except (ValueError, IndexError):
            pos.speed_knots = 0.0
        try:
            pos.track_angle = float(parts[8])
        except (ValueError, IndexError):
            pos.track_angle = 0.0
        pos.last_update = time.time()
        # RMC 不含高度和 fix_quality，需要 GGA 补充
        return pos

    return None


# ── Beidou 短报文 ──


@dataclass
class BeidouMessage:
    sender_id: str
    content: str
    timestamp: str = ""
    signal_strength: int = 0


def build_command(cmd: str, params: str) -> str:
    body = f"{cmd},{params}"
    return f"${body}*{_xor_checksum(body)}\r\n"


def build_sms(receiver_id: str, content: str, max_bytes: int = BD1_MAX_BYTES) -> str:
    """构建发送短报文指令，自动截断超长内容 (GBK 编码)"""
    content_bytes = content.encode('gbk', errors='replace')
    if len(content_bytes) > max_bytes:
        content_bytes = content_bytes[:max_bytes]
    hex_content = content_bytes.hex().upper()
    params = f"{len(content_bytes)},{receiver_id},{hex_content}"
    return build_command("$CCSMS", params)


class BeidouDevice:
    """北斗 RDSS 设备 — 串口通信 + NMEA 定位解析"""

    def __init__(self, port: str = 'COM1', baudrate: int = 115200,
                 card_id: str = '', timeout: float = 5.0):
        self.port = port
        self.baudrate = baudrate
        self.card_id = card_id
        self.timeout = timeout
        self._serial = None
        self._lock = threading.Lock()
        self.connected = False
        self.signal_strength = 0

        # ── 位置 ──
        self._position: Optional[DevicePosition] = None
        self._pos_lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_running = False

    def open(self) -> bool:
        try:
            import serial
            self._serial = serial.Serial(
                self.port, self.baudrate, timeout=self.timeout,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )
            self.connected = True
            self._start_reader()
            logger.info("北斗模块已连接: %s @ %d baud (定位+短报文)", self.port, self.baudrate)
            return True
        except ImportError:
            logger.info("pyserial 未安装，北斗使用模拟模式")
            self.connected = True
            return True
        except Exception as e:
            logger.error("北斗模块连接失败: %s", e)
            return False

    def close(self):
        self._stop_reader()
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        self.connected = False

    def send_message(self, receiver_id: str, content: str) -> bool:
        if not self.connected:
            return False
        cmd = build_sms(receiver_id, content)
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.write(cmd.encode('ascii'))
                    self._serial.flush()
                    resp = self._serial.readline()
                    logger.debug("北斗 TX: %s RX: %s", cmd.strip(),
                                 resp.decode('ascii', errors='replace').strip())
                except Exception as e:
                    logger.error("北斗发送失败: %s", e)
                    return False
        logger.info("北斗短报文已发送 → %s", receiver_id)
        return True

    def check_signal(self) -> int:
        return self.signal_strength

    def ping(self) -> bool:
        return self.connected

    # ── 位置接口 ──

    def get_position(self) -> Optional[DevicePosition]:
        """获取最新定位数据"""
        with self._pos_lock:
            return self._position

    def has_position_fix(self) -> bool:
        """是否有有效定位"""
        pos = self.get_position()
        return pos is not None and pos.has_fix

    # ── NMEA 后台读取 ──

    def _start_reader(self):
        """启动 NMEA 后台读取线程"""
        if self._serial is None:
            return
        self._reader_running = True
        self._reader_thread = threading.Thread(
            target=self._nmea_reader_loop, daemon=True,
        )
        self._reader_thread.start()
        logger.info("NMEA 定位读取已启动")

    def _stop_reader(self):
        self._reader_running = False
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=3)

    def _nmea_reader_loop(self):
        """后台持续读取 NMEA 语句并解析位置"""
        buf = ""
        while self._reader_running and self._serial and self._serial.is_open:
            try:
                chunk = self._serial.read(256)
                if not chunk:
                    continue
                buf += chunk.decode('ascii', errors='replace')

                # 按行分割
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line = line.strip()
                    if not line:
                        continue

                    pos = parse_nmea_sentence(line)
                    if pos is None:
                        continue

                    # GGA 有高度和 fix_quality，RMC 有速度
                    # 合并: GGA 提供主体，RMC 补充速度
                    with self._pos_lock:
                        if self._position is None:
                            self._position = pos
                        else:
                            if pos.altitude != 0 or pos.fix_quality != 0:
                                # GGA: 更新位置+高度+fix
                                self._position.latitude = pos.latitude
                                self._position.longitude = pos.longitude
                                self._position.altitude = pos.altitude
                                self._position.fix_quality = pos.fix_quality
                                self._position.satellites = pos.satellites
                                self._position.timestamp = pos.timestamp
                                self._position.last_update = pos.last_update
                            if pos.speed_knots != 0 or pos.track_angle != 0:
                                # RMC: 补充速度和航向
                                self._position.speed_knots = pos.speed_knots
                                self._position.track_angle = pos.track_angle

            except Exception as e:
                logger.debug("NMEA 读取异常: %s", e)
                time.sleep(1)


class SimulatedBeidouDevice(BeidouDevice):
    """模拟北斗设备 — 开发/测试用 (含模拟定位)"""

    def __init__(self, card_id: str = '0000000001',
                 sim_lat: float = 30.0, sim_lon: float = 120.0, sim_alt: float = 50.0):
        super().__init__(port='SIM', baudrate=115200, card_id=card_id)
        self._sent_count = 0
        self._history: list[dict] = []
        self.signal_strength = 4

        # 模拟定位
        self._position = DevicePosition(
            latitude=sim_lat,
            longitude=sim_lon,
            altitude=sim_alt,
            fix_quality=1,       # 模拟 GPS 固定解
            satellites=12,
            timestamp=datetime.now(timezone.utc).strftime('%H%M%S'),
            last_update=time.time(),
        )

    def open(self) -> bool:
        self.connected = True
        p = self._position
        logger.info("北斗模块(模拟) 已就绪 | 卡号: %s | 定位: %.5f,%.5f H%.0fm",
                     self.card_id, p.latitude, p.longitude, p.altitude)
        return True

    def close(self):
        self.connected = False

    def send_message(self, receiver_id: str, content: str) -> bool:
        if not self.connected:
            return False
        self._sent_count += 1
        entry = {
            'seq': self._sent_count,
            'receiver': receiver_id,
            'content': content,
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'bytes': len(content.encode('gbk', errors='replace')),
        }
        self._history.append(entry)
        if len(self._history) > 500:
            self._history = self._history[-200:]
        preview = content[:60] + ('...' if len(content) > 60 else '')
        logger.info("[北斗模拟] #%d → %s: %s", self._sent_count, receiver_id, preview)
        return True

    @property
    def history(self):
        return self._history

    def ping(self) -> bool:
        return True

    def get_position(self) -> Optional[DevicePosition]:
        """模拟模式下始终返回预设位置 (更新时间戳)"""
        if self._position:
            self._position.last_update = time.time()
        return self._position

    def has_position_fix(self) -> bool:
        return True


def create_beidou(config: dict) -> BeidouDevice:
    """根据配置创建北斗设备 (含定位功能)"""
    bd = config.get('beidou', {})
    mode = bd.get('mode', 'simulated')

    if mode == 'simulated':
        # 从配置读取模拟位置 (默认杭州富阳附近)
        pos_cfg = config.get('position', {})
        return SimulatedBeidouDevice(
            card_id=bd.get('card_id', '0000000001'),
            sim_lat=pos_cfg.get('manual_lat', 30.0),
            sim_lon=pos_cfg.get('manual_lon', 120.0),
            sim_alt=pos_cfg.get('manual_alt', 50.0),
        )

    return BeidouDevice(
        port=bd.get('port', 'COM1'),
        baudrate=bd.get('baudrate', 115200),
        card_id=bd.get('card_id', ''),
        timeout=bd.get('timeout', 5.0),
    )


def format_emergency_message(device_name: str, drone_id: str, level: str,
                             distance: float, line_name: str,
                             lat: float, lon: float, alt: float) -> str:
    """格式化北斗应急短报文 (精简版，适应 78 字节限制)"""
    level_cn = {'critical': '危险', 'severe': '严重', 'warning': '警告'}
    return (
        f"[{level_cn.get(level, level)}]{device_name} "
        f"无人机{drone_id}距{line_name}{distance:.0f}m "
        f"({lat:.4f},{lon:.4f} H{alt:.0f}m)"
    )
