"""
RID 消息解析器 — 双协议支持

协议:
  - gb46750     GB 46750-2025 (默认) — BLE UUID 0xFFFF, 0x0F 打包
  - astm_f3411  ASTM F3411 — BLE UUID 0xFFFA, 简单拼接

用法:
  from core.parser import parse_rid_pack, ParsedRID, get_active_protocol
  result = parse_rid_pack(data, mac_address="...", rssi=-45)
  print(get_active_protocol().name)  # "gb46750"
"""

from typing import Optional

# ── 共享类型 & 常量 & 通用解码器 ──
from .types import (
    ODID_SERVICE_UUID,
    MSG_BASIC_ID, MSG_LOCATION, MSG_AUTH, MSG_SELF_ID,
    MSG_SYSTEM, MSG_OPERATOR_ID, MSG_PACK,
    ID_TYPE_NONE, ID_TYPE_SERIAL, ID_TYPE_CAA, ID_TYPE_UTM, ID_TYPE_SESSION,
    UA_TYPE_NONE, UA_TYPE_AEROPLANE, UA_TYPE_HELICOPTER,
    UA_TYPE_GYROPLANE, UA_TYPE_HYBRID, UA_TYPE_ORNITHOPTER,
    UA_TYPE_GLIDER, UA_TYPE_KITE, UA_TYPE_FREE_BALLOON,
    UA_TYPE_CAPTIVE_BALLOON, UA_TYPE_AIRSHIP, UA_TYPE_FREE_FALL,
    UA_TYPE_ROCKET, UA_TYPE_TETHERED, UA_TYPE_GROUND, UA_TYPE_OTHER,
    UA_TYPE_NAMES,
    LOC_STATUS_TIMESTAMP_VALID,
    BasicIDMessage, LocationMessage, SelfIDMessage,
    SystemMessage, OperatorIDMessage, ParsedRID,
    ReceiverType,
    parse_basic_id, parse_self_id,
)

# ── ASTM 专有解码器 (向后兼容: 旧的直接导入) ──
from .astm import (
    parse_location_astm,
    parse_system_astm,
    parse_operator_id,
)
# 别名: 保持旧代码中直接 import parse_location 的行为
parse_location = parse_location_astm
parse_system = parse_system_astm

# ── GB 46750 专有解码器 ──
from .gb46750 import (
    parse_location_gb,
    parse_system_gb,
)

# ── 协议基类 ──
from .base import RIDProtocol

# ── 协议注册表 ──
from .astm import PROTOCOL as _ASTM_PROTOCOL
from .gb46750 import PROTOCOL as _GB46750_PROTOCOL

_PROTOCOLS: dict[str, RIDProtocol] = {
    "astm_f3411": _ASTM_PROTOCOL,
    "gb46750": _GB46750_PROTOCOL,
}

_active_protocol: RIDProtocol = _GB46750_PROTOCOL


def get_active_protocol() -> RIDProtocol:
    """返回当前激活的协议实例"""
    return _active_protocol


def set_active_protocol(name: str) -> None:
    """切换激活协议

    Args:
        name: "gb46750" (默认) 或 "astm_f3411"
    """
    global _active_protocol
    if name not in _PROTOCOLS:
        raise ValueError(
            f"未知协议: {name!r}. 可选: {list(_PROTOCOLS.keys())}"
        )
    _active_protocol = _PROTOCOLS[name]


def configure_protocol(config: dict) -> None:
    """从配置字典设置协议 (读取 config["protocol"], 默认 gb46750)"""
    name = config.get("protocol", "gb46750")
    set_active_protocol(name)


def parse_rid_pack(
    data: bytes,
    mac_address: str = "",
    rssi: int = 0,
    protocol: Optional[str] = None,
) -> ParsedRID:
    """解析 RID 消息包 (使用当前激活协议)

    Args:
        data: 原始字节 (BLE Service Data 或 WiFi Beacon payload)
        mac_address: 源 MAC 地址
        rssi: 信号强度
        protocol: 覆盖协议 ("gb46750" / "astm_f3411"), None=使用激活协议

    Returns:
        ParsedRID 解析结果
    """
    if protocol is not None:
        proto = _PROTOCOLS[protocol]
    else:
        proto = _active_protocol
    return proto.parse_message_pack(data, mac_address, rssi)


def create_receiver(receiver_type: "ReceiverType", wifi_interface: str = None,
                    serial_device: str = "/dev/ttyUSB0", serial_baud: int = 115200,
                    drone_count: int = 6, update_interval: float = 1.0):
    """接收器工厂 — 根据 ReceiverType 创建对应的接收器实例

    返回的接收器具有 set_callback() 和 start()/stop() 方法。
    BLE/WiFi 依赖缺失时回退到模拟接收器。
    """
    if receiver_type == ReceiverType.SIMULATED:
        from receiver.simulated import create_simulated_receiver
        return create_simulated_receiver(callback=None, drone_count=drone_count,
                                         update_interval=update_interval)
    elif receiver_type == ReceiverType.WIFI:
        try:
            from receiver.wifi import create_wifi_receiver
            return create_wifi_receiver(callback=None, interface=wifi_interface)
        except (ImportError, OSError) as e:
            from logging_config import get_logger
            get_logger(__name__).warning("WiFi 接收器不可用 (%s), 回退到模拟模式", e)
            from receiver.simulated import create_simulated_receiver
            return create_simulated_receiver(callback=None, drone_count=drone_count,
                                             update_interval=update_interval)
    elif receiver_type == ReceiverType.SERIAL:
        try:
            from receiver.serial import create_serial_receiver
            return create_serial_receiver(callback=None, device=serial_device, baud=serial_baud)
        except (ImportError, OSError) as e:
            from logging_config import get_logger
            get_logger(__name__).warning("串口接收器不可用 (%s), 回退到模拟模式", e)
            from receiver.simulated import create_simulated_receiver
            return create_simulated_receiver(callback=None, drone_count=drone_count,
                                             update_interval=update_interval)
    else:  # BLE / AUTO
        try:
            from receiver.ble import BLE_RIDReceiver
            return BLE_RIDReceiver(callback=None, scan_duration=5.0)
        except ImportError as e:
            from logging_config import get_logger
            get_logger(__name__).warning("BLE 接收器不可用 (%s), 回退到模拟模式", e)
            from receiver.simulated import create_simulated_receiver
            return create_simulated_receiver(callback=None, drone_count=drone_count,
                                             update_interval=update_interval)


__all__ = [
    # types
    "BasicIDMessage", "LocationMessage", "SelfIDMessage",
    "SystemMessage", "OperatorIDMessage", "ParsedRID",
    "ReceiverType",
    # constants
    "ODID_SERVICE_UUID",
    "MSG_BASIC_ID", "MSG_LOCATION", "MSG_AUTH", "MSG_SELF_ID",
    "MSG_SYSTEM", "MSG_OPERATOR_ID", "MSG_PACK",
    "ID_TYPE_NONE", "ID_TYPE_SERIAL", "ID_TYPE_CAA", "ID_TYPE_UTM", "ID_TYPE_SESSION",
    "UA_TYPE_NAMES",
    "LOC_STATUS_TIMESTAMP_VALID",
    # decoders
    "parse_basic_id", "parse_location", "parse_system",
    "parse_self_id", "parse_operator_id",
    "parse_rid_pack",
    # protocol management
    "RIDProtocol", "get_active_protocol", "set_active_protocol", "configure_protocol",
    # receiver factory
    "create_receiver",
]
