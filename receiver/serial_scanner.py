"""
串口扫描器 — 跨平台端口枚举与 ESP32 自动检测

用法:
    from receiver.serial_scanner import auto_detect, scan_ports, list_serial_ports
    port = auto_detect()  # 自动查找第一个 ESP32 设备
    results = scan_ports()  # 扫描所有端口并返回匹配列表
"""

import json
import time
from dataclasses import dataclass
from typing import List, Optional

from logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class ScanResult:
    """单次探测结果"""
    port: str           # "COM4" / "/dev/ttyUSB0"
    baud: int           # 115200
    dev_id: str         # "EXD001"
    sample: dict        # 第一条有效 JSON


def list_serial_ports() -> List[str]:
    """枚举所有可用串口 (跨平台)

    使用 pyserial 的 serial.tools.list_ports.comports()。
    Linux 上额外补充 glob(/dev/ttyUSB*/ACM*/ttyS*) 防止漏检。
    """
    ports = []
    try:
        import serial.tools.list_ports as list_ports
        for info in list_ports.comports():
            ports.append(info.device)
    except ImportError:
        pass

    # Linux 补充 glob (有些情况下 pyserial 不会报告所有端口)
    import os
    import glob as _glob
    extra_patterns = ["/dev/ttyUSB*", "/dev/ttyACM*"]
    if os.name != "nt":
        for pat in extra_patterns:
            ports.extend(_glob.glob(pat))

    # 去重 + 排序
    seen = set()
    result = []
    for p in ports:
        pn = os.path.normpath(p)
        if pn not in seen:
            seen.add(pn)
            result.append(p)
    result.sort()
    return result


def probe_port(port: str, baud: int = 115200, timeout: float = 2.0) -> Optional[ScanResult]:
    """探测单个串口是否连接了 ESP32 RID 设备

    打开端口, 读取数据直到发现包含 devId 字段的有效 JSON 行,
    或者超时。探测期间不产生副作用 (端口用完即关)。

    Returns: ScanResult 或 None (未发现/不可读)
    """
    try:
        import serial
        ser = serial.Serial(port, baud, timeout=1)
    except ImportError:
        logger.warning("pyserial 未安装, 无法探测串口")
        return None
    except (serial.SerialException, PermissionError, OSError) as e:
        logger.debug("端口 %s 打开失败: %s", port, e)
        return None

    deadline = time.monotonic() + timeout
    buf = b""

    try:
        while time.monotonic() < deadline:
            try:
                byte = ser.read(1)
            except (serial.SerialException, OSError):
                break
            if not byte:
                continue

            if byte in (b"\n", b"\r"):
                line = buf.strip()
                buf = b""
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict) and "devId" in data:
                        return ScanResult(
                            port=port,
                            baud=baud,
                            dev_id=str(data["devId"]),
                            sample=data,
                        )
                except (ValueError, UnicodeDecodeError):
                    continue
            else:
                buf += byte
                if len(buf) > 4096:
                    buf = b""
    finally:
        try:
            ser.close()
        except (serial.SerialException, OSError):
            pass

    return None


def scan_ports(baud: int = 115200, probe_timeout: float = 2.0,
               ports: Optional[List[str]] = None) -> List[ScanResult]:
    """扫描所有可用串口, 返回检测到的 ESP32 设备列表

    ports: 指定端口列表 (None = 自动枚举)
    """
    if ports is None:
        ports = list_serial_ports()

    if not ports:
        logger.warning("未发现任何可用串口")
        return []

    logger.info("扫描 %d 个串口 ...", len(ports))
    results = []
    for port in ports:
        logger.debug("探测: %s", port)
        result = probe_port(port, baud=baud, timeout=probe_timeout)
        if result:
            logger.info("发现 ESP32: %s (devId=%s)", result.port, result.dev_id)
            results.append(result)

    if not results:
        logger.warning("在 %d 个串口中未发现 ESP32 设备", len(ports))

    return results


def auto_detect(baud: int = 115200, probe_timeout: float = 2.0) -> Optional[str]:
    """自动检测并返回第一个 ESP32 设备端口, 未找到返回 None"""
    results = scan_ports(baud=baud, probe_timeout=probe_timeout)
    if results:
        return results[0].port
    return None
