"""
北斗短报文 (Beidou RDSS) 应急通信模块

协议: Beidou RDSS 短报文, RS232/RS485 串口
- BD1 短报文: 最大 78 字节/条 (≈ 39 个中文)
- BD3 区域短报文: 最大 ~1000 字节/条

指令格式: $CMD,params*XX\r\n  (XX = XOR 校验)
"""

import threading
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from logging_config import get_logger

logger = get_logger(__name__)

BD1_MAX_BYTES = 78
BD3_MAX_BYTES = 1000


@dataclass
class BeidouMessage:
    sender_id: str
    content: str
    timestamp: str = ""
    signal_strength: int = 0


def _xor_checksum(data: str) -> str:
    checksum = 0
    for c in data:
        checksum ^= ord(c)
    return f"{checksum:02X}"


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
    """北斗 RDSS 设备 — 串口通信"""

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

    def open(self) -> bool:
        try:
            import serial
            self._serial = serial.Serial(
                self.port, self.baudrate, timeout=self.timeout,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )
            self.connected = True
            logger.info("北斗模块已连接: %s @ %d baud", self.port, self.baudrate)
            return True
        except ImportError:
            logger.info("pyserial 未安装，北斗使用模拟模式")
            self.connected = True
            return True
        except Exception as e:
            logger.error("北斗模块连接失败: %s", e)
            return False

    def close(self):
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


class SimulatedBeidouDevice(BeidouDevice):
    """模拟北斗设备 — 开发/测试用"""

    def __init__(self, card_id: str = '0000000001'):
        super().__init__(port='SIM', baudrate=115200, card_id=card_id)
        self._sent_count = 0
        self._history: list[dict] = []
        self.signal_strength = 4

    def open(self) -> bool:
        self.connected = True
        logger.info("北斗模块(模拟) 已就绪 | 卡号: %s", self.card_id)
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


def create_beidou(config: dict) -> BeidouDevice:
    """根据配置创建北斗设备"""
    bd = config.get('beidou', {})
    mode = bd.get('mode', 'simulated')
    if mode == 'simulated':
        return SimulatedBeidouDevice(card_id=bd.get('card_id', '0000000001'))
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
