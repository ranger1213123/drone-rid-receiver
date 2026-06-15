"""
RIDProtocol — 协议定义基类

每个协议实例封装:
  - 物理层标识 (BLE UUID, WiFi OUI)
  - 有效报文类型 / ID 类型
  - 消息包解析器
  - 每类报文的解码函数
"""

from typing import Callable, Optional, Set, Dict


class RIDProtocol:
    """RID 协议定义"""

    def __init__(
        self,
        name: str,
        ble_service_uuid: int,
        wifi_oui: bytes,
        pack_parser: Callable,
        message_decoders: Dict[int, Callable],
        valid_msg_types: Optional[Set[int]] = None,
        valid_id_types: Optional[Set[int]] = None,
        ble_app_code: int = 0,
        max_messages: int = 255,
        max_pack_size: int = 65535,
    ):
        self._name = name
        self._ble_service_uuid = ble_service_uuid
        self._wifi_oui = wifi_oui
        self._pack_parser = pack_parser
        self._message_decoders = message_decoders
        self._valid_msg_types = valid_msg_types or {0x0, 0x1, 0x2, 0x3, 0x4, 0x5}
        self._valid_id_types = valid_id_types or {0, 1, 2, 3, 4}
        self._ble_app_code = ble_app_code
        self._max_messages = max_messages
        self._max_pack_size = max_pack_size

    @property
    def name(self) -> str:
        return self._name

    @property
    def ble_service_uuid(self) -> int:
        return self._ble_service_uuid

    @property
    def wifi_oui(self) -> bytes:
        return self._wifi_oui

    @property
    def ble_app_code(self) -> int:
        return self._ble_app_code

    @property
    def max_messages(self) -> int:
        return self._max_messages

    @property
    def max_pack_size(self) -> int:
        return self._max_pack_size

    def get_ble_uuid_128(self) -> str:
        """128-bit BLE UUID (用于 bleak service_data 匹配)"""
        return f"0000{self._ble_service_uuid:04x}-0000-1000-8000-00805f9b34fb"

    def is_msg_type_valid(self, msg_type: int) -> bool:
        return msg_type in self._valid_msg_types

    def is_id_type_valid(self, id_type: int) -> bool:
        return id_type in self._valid_id_types

    def parse_message_pack(self, data: bytes, mac_address: str = "",
                           rssi: int = 0) -> "ParsedRID":
        """解析消息包 → 委托给协议特定的 pack_parser"""
        from .types import ParsedRID
        return self._pack_parser(data, mac_address, rssi)
