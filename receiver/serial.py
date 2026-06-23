"""
ESP32 串口 RID 接收器 — 从 UART 读取 JSON 格式的 Remote ID 数据

ESP32 通过 /dev/ttyUSB0 (115200 baud) 发送两种 JSON 格式:

格式 1 (心跳):
  {"devId":"EXD001","count":86}

格式 2 (完整数据):
  {"devId":"EXD001","data":{"osid":"1581F8PJC245B0001KRC","RSSI":-72,
   "Op_Lat":30.61517,"Op_Lon":104.06742,"Op_Alt":469,
   "Heading":361,"Speed":0,"UAType":2,...}}

参考: rid_serial_receiver.py v0.7 (RSB-4221 实测通过)
"""

import asyncio
import json
import os
import subprocess
import threading
import time
from typing import Optional, Callable

from logging_config import get_logger
from core.parser import (
    ParsedRID, BasicIDMessage, LocationMessage,
    ID_TYPE_SERIAL, UA_TYPE_HELICOPTER,
)
from receiver.ble import RIDReceiver

logger = get_logger(__name__)

SERIAL_DEVICE = "/dev/ttyUSB0"
BAUD_RATE = 115200


def _configure_serial(device: str, baud: int) -> None:
    """通过 stty 配置串口参数"""
    try:
        with open(os.devnull, "w") as null:
            subprocess.call(
                ["stty", "-F", device, str(baud),
                 "cs8", "-cstopb", "-parenb", "raw", "-echo", "icrnl"],
                stdout=null, stderr=null, timeout=3,
            )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def _build_parsed_rid(data: dict) -> Optional[ParsedRID]:
    """从 ESP32 JSON 数据构造 ParsedRID

    支持格式 1 (心跳) 和格式 2 (完整数据)。
    格式 1 没有位置信息，但 transmitter 可能仍有变化需要记录。
    """
    dev_id = data.get("devId", "unknown")
    count = data.get("count", 0)
    inner = data.get("data")

    if inner and isinstance(inner, dict):
        # 格式 2: 完整数据包
        osid = inner.get("osid", "")
        drone_id = osid if osid else dev_id
        rssi = inner.get("RSSI", 0)
        ua_type = inner.get("UAType", UA_TYPE_HELICOPTER)

        basic_id = BasicIDMessage(
            id_type=ID_TYPE_SERIAL,
            ua_type=ua_type,
            uas_id=drone_id,
        )

        # 优先使用 operator 位置 (Op_Lat/Op_Lon/Op_Alt)
        op_lat = inner.get("Op_Lat", 0.0)
        op_lon = inner.get("Op_Lon", 0.0)
        op_alt = inner.get("Op_Alt", 0.0)

        # 备用: drone 自身 GPS (通常为 0)
        drone_lat = inner.get("Lat", 0.0)
        drone_lon = inner.get("Lon", 0.0)
        alt_geo = inner.get("AltGeo", -1000.0)
        alt_baro = inner.get("AltBaro", 0.0)
        height = inner.get("Height", 0.0)
        heading = inner.get("Heading", 0.0)
        speed = inner.get("Speed", 0.0)
        status = inner.get("Status", 0)
        ua_time = inner.get("UATime", 0)

        # 优先 operator coords，回退到 drone coords
        lat = op_lat if op_lat != 0.0 else drone_lat
        lon = op_lon if op_lon != 0.0 else drone_lon
        alt = op_alt if op_alt != 0.0 else (alt_geo if alt_geo != -1000.0 else 0.0)

        location = LocationMessage(
            status=status,
            latitude=lat,
            longitude=lon,
            altitude_geodetic=alt,
            altitude_pressure=float(alt_baro),
            height_agl=float(height),
            track_angle=float(heading),
            speed_horizontal=float(speed),
            timestamp=float(ua_time),
        )

        return ParsedRID(
            raw_data=json.dumps(data).encode(),
            mac_address=dev_id,
            rssi=rssi,
            basic_id=basic_id,
            location=location,
        )
    else:
        # 格式 1: 心跳 (无位置)
        basic_id = BasicIDMessage(
            id_type=ID_TYPE_SERIAL,
            ua_type=UA_TYPE_HELICOPTER,
            uas_id=dev_id,
        )
        return ParsedRID(
            raw_data=json.dumps(data).encode(),
            mac_address=dev_id,
            rssi=0,
            basic_id=basic_id,
        )


class SerialRIDReceiver(RIDReceiver):
    """串口 RID 接收器 — 从 ESP32 UART 读取 RID 数据

    用法:
        receiver = SerialRIDReceiver(callback, device="/dev/ttyUSB0", baud=115200)
        await receiver.start()
    """

    def __init__(self, callback: Callable[[ParsedRID], None],
                 device: str = SERIAL_DEVICE, baud: int = BAUD_RATE):
        super().__init__(callback)
        self.device = device
        self.baud = baud
        self._thread: Optional[threading.Thread] = None
        self._serial = None

    def _read_loop(self):
        """串口读取线程 (Windows: pyserial, Linux: open+stty)"""
        logger.info("串口接收线程启动: %s @ %d baud", self.device, self.baud)

        while self._running:
            try:
                if os.name == 'nt':
                    import serial
                    self._serial = serial.Serial(self.device, self.baud, timeout=1)
                    logger.info("串口已打开 (pyserial): %s", self.device)
                else:
                    self._serial = open(self.device, "rb", buffering=0)
                    _configure_serial(self.device, self.baud)

                buf = b""

                while self._running:
                    if os.name == 'nt':
                        byte = self._serial.read(1)
                    else:
                        byte = self._serial.read(1)
                    if not byte:
                        continue  # timeout, keep reading

                    if byte in (b"\n", b"\r"):
                        line = buf.strip()
                        buf = b""
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            parsed = _build_parsed_rid(data)
                            if parsed and parsed.drone_id:
                                try:
                                    self.callback(parsed)
                                except Exception:
                                    pass
                        except (ValueError, UnicodeDecodeError):
                            if len(line) > 2:
                                logger.debug("非 JSON 行: %s", repr(line[:80]))
                    else:
                        buf += byte
                        if len(buf) > 2048:
                            buf = b""

            except OSError as e:
                if self._running:
                    logger.error("串口异常: %s, 2s 后重试", e)
                    time.sleep(2)
            finally:
                if self._serial:
                    try:
                        self._serial.close()
                    except OSError:
                        pass
                    self._serial = None

        logger.info("串口接收线程结束")

    async def start(self):
        """启动串口接收"""
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        logger.info("串口 RID 接收器已启动")

    async def stop(self):
        """停止串口接收"""
        self._running = False
        if self._serial:
            try:
                self._serial.close()
            except OSError:
                pass
            self._serial = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None


def create_serial_receiver(callback: Callable[[ParsedRID], None],
                           device: str = SERIAL_DEVICE,
                           baud: int = BAUD_RATE) -> SerialRIDReceiver:
    """创建串口 RID 接收器

    device: 串口设备路径, 默认 /dev/ttyUSB0
    baud:   波特率, 默认 115200
    """
    return SerialRIDReceiver(callback, device=device, baud=baud)
