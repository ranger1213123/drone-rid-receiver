"""
空域数据源 — 从多种来源加载禁飞区/管制空域数据

支持:
  - YAML 手动配置 (电力线等)
  - UOM 平台动态获取 (MH/T 4053-2022 标准接口, CAAC 官方禁飞区)
  - 组合源 (多源合并)

UOM 接入: 需在 https://uom.caac.gov.cn 注册并申请接口权限, 获取 appId + appKey
"""

import hashlib
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

import requests

from logging_config import get_logger

logger = get_logger(__name__)


def _uom_sign(app_key: str, timestamp: str, biz_content: str) -> str:
    raw = f"appKey={app_key}&timestamp={timestamp}&bizContent={biz_content}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


@dataclass
class AirspaceZone:
    zone_id: str
    name: str
    zone_type: str          # no_fly / restricted / warning / power_line
    vertices: list          # [(lat, lon), ...] 多边形顶点
    altitude_floor: float   # 底部海拔 (m)
    altitude_ceiling: float # 顶部海拔 (m)
    source: str = ""        # yaml / uom
    effective_start: str = ""
    effective_end: str = ""


class AirspaceSource(ABC):
    @abstractmethod
    def fetch(self) -> List[AirspaceZone]:
        ...

    @property
    @abstractmethod
    def source_name(self) -> str:
        ...


class YAMLAirspaceSource(AirspaceSource):
    """从 YAML 加载电力线作为空域"""

    def __init__(self, yaml_path: str):
        self.yaml_path = yaml_path

    @property
    def source_name(self) -> str:
        return "yaml"

    def fetch(self) -> List[AirspaceZone]:
        import yaml
        if not os.path.exists(self.yaml_path):
            logger.warning("空域 YAML 文件不存在: %s", self.yaml_path)
            return []

        with open(self.yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        lines = data.get("power_lines", []) if isinstance(data, dict) else []
        zones = []
        for line in lines:
            center_lat = (line["lat1"] + line["lat2"]) / 2
            center_lon = (line["lon1"] + line["lon2"]) / 2
            dlat = 0.001
            dlon = 0.0013
            vertices = [
                (center_lat + dlat, center_lon - dlon),
                (center_lat + dlat, center_lon + dlon),
                (center_lat - dlat, center_lon + dlon),
                (center_lat - dlat, center_lon - dlon),
            ]
            zones.append(AirspaceZone(
                zone_id=f"pl-{line.get('name', 'unknown')}",
                name=line.get("name", "电力线"),
                zone_type="power_line",
                vertices=vertices,
                altitude_floor=min(line["alt1"], line["alt2"]) - 50,
                altitude_ceiling=max(line["alt1"], line["alt2"]) + 50,
                source="yaml",
            ))
        return zones


class UOMAirspaceSource(AirspaceSource):
    """UOM 平台空域数据源 (MH/T 4053-2022)

    接口: 适飞空域推送接口 (UOM → 运行控制系统)
    报文结构:
      请求: {appId, format, charset, signType, sign, timestamp, version, bizContent}
      响应: {msg, code, sign, timestamp, data: {coor, updateTime}}

    空域数据使用网格编码 (Gridcode) 表示, 需解码为多边形顶点。
    """

    def __init__(self, app_id: str = "", app_key: str = "",
                 base_url: str = "https://uom.caac.gov.cn/api",
                 cache_ttl: int = 3600, cache_path: str = "data/uom_cache.json"):
        self.app_id = app_id or os.environ.get("UOM_APP_ID", "")
        self.app_key = app_key or os.environ.get("UOM_APP_KEY", "")
        self.base_url = base_url
        self.cache_ttl = cache_ttl
        self.cache_path = cache_path

    @property
    def source_name(self) -> str:
        return "uom"

    def fetch(self) -> List[AirspaceZone]:
        cached = self._load_cache()
        if cached is not None:
            return cached

        zones = self._call_api()
        if zones is not None:
            self._save_cache(zones)
            return zones

        logger.warning("UOM 数据获取失败，无可用空域数据")
        return []

    def _call_api(self) -> Optional[List[AirspaceZone]]:
        if not self.app_id or not self.app_key:
            logger.info("UOM appId/appKey 未配置, 跳过 UOM 数据获取")
            return []

        biz_content = json.dumps({"queryType": "all"})
        ts = time.strftime("%Y%m%d%H%M%S", time.gmtime())

        payload = {
            "appId": self.app_id,
            "format": "JSON",
            "charset": "UTF-8",
            "signType": "md5",
            "sign": _uom_sign(self.app_key, ts, biz_content),
            "timestamp": ts,
            "version": "1.0",
            "bizContent": biz_content,
        }

        try:
            resp = requests.post(
                f"{self.base_url}/airspace/query",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            raw = resp.json()
            if raw.get("code") != 1:
                logger.warning("UOM API 返回错误: %s", raw.get("msg", ""))
                return None
            return self._parse_uom_response(raw)
        except requests.exceptions.RequestException as e:
            logger.error("UOM API 请求失败: %s", e)
            return None

    def _parse_uom_response(self, raw: dict) -> List[AirspaceZone]:
        """解析 UOM 空域响应数据

        UOM 返回的 data.coor 使用网格编码 (Gridcode)。
        这里将其解析为多边形区域。
        """
        zones = []
        data = raw.get("data", {})
        if isinstance(data, dict):
            items = [data]
        elif isinstance(data, list):
            items = data
        else:
            items = []

        for item in items:
            coor = item.get("coor", "")
            if not coor:
                continue

            # Gridcode 解码: 将网格编码转为多边形顶点
            # 简化处理: 用 Gridcode 中心点扩展为矩形
            vertices = self._gridcode_to_polygon(coor)
            if not vertices:
                continue

            zones.append(AirspaceZone(
                zone_id=str(item.get("zoneId", coor[:8])),
                name=item.get("zoneName", "UOM禁飞区"),
                zone_type=item.get("zoneType", "no_fly"),
                vertices=vertices,
                altitude_floor=float(item.get("altFloor", 0)),
                altitude_ceiling=float(item.get("altCeiling", 10000)),
                source="uom",
                effective_start=item.get("effStart", ""),
                effective_end=item.get("effEnd", ""),
            ))
        logger.info("UOM 数据解析: %d 个空域区域", len(zones))
        return zones

    @staticmethod
    def _gridcode_to_polygon(gridcode: str) -> list:
        """Gridcode (网格编码) 转多边形顶点

        UOM 适飞空域使用网格编码表示。网格编码格式:
          - 每两位十六进制字符表示一个层级
          - 完整编码可还原为经纬度范围的矩形

        简化实现: 假设 gridcode 为位置码，扩展 ~0.01 度矩形。
        完整实现需查阅 UOM Gridcode 编码规范。
        """
        try:
            code_val = int(gridcode[:8], 16)
            lat = (code_val >> 32) / 1e7
            lon = (code_val & 0xFFFFFFFF) / 1e7
        except (ValueError, OverflowError):
            return []

        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return []

        # 扩展为约 500m 矩形
        dlat = 0.0045
        dlon = 0.0045
        return [
            (lat + dlat, lon - dlon),
            (lat + dlat, lon + dlon),
            (lat - dlat, lon + dlon),
            (lat - dlat, lon - dlon),
        ]

    def _load_cache(self) -> Optional[List[AirspaceZone]]:
        if not os.path.exists(self.cache_path):
            return None
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            age = time.time() - raw.get("cached_at", 0)
            if age > self.cache_ttl:
                logger.info("UOM 缓存已过期 (%.0fs)", age)
                return None
            logger.info("UOM 缓存命中 (%.0fs old, %d zones)",
                        age, len(raw.get("zones", [])))
            return [AirspaceZone(**z) for z in raw.get("zones", [])]
        except Exception as e:
            logger.warning("UOM 缓存读取失败: %s", e)
            return None

    def _save_cache(self, zones: List[AirspaceZone]):
        os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
        data = {
            "cached_at": time.time(),
            "zones": [
                {k: v for k, v in z.__dict__.items()}
                for z in zones
            ],
        }
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


class CompositeAirspaceSource(AirspaceSource):
    """组合多个空域数据源"""

    def __init__(self, sources: List[AirspaceSource] = None):
        self.sources = sources or []

    @property
    def source_name(self) -> str:
        return "+".join(s.source_name for s in self.sources)

    def add_source(self, source: AirspaceSource):
        self.sources.append(source)

    def fetch(self) -> List[AirspaceZone]:
        all_zones = []
        for s in self.sources:
            try:
                zones = s.fetch()
                all_zones.extend(zones)
                logger.info("%s: 加载 %d 个空域区域", s.source_name, len(zones))
            except Exception as e:
                logger.error("%s 加载失败: %s", s.source_name, e)
        return all_zones


def _point_in_polygon(lat: float, lon: float, vertices: list) -> bool:
    """射线法判断点是否在多边形内"""
    n = len(vertices)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = vertices[i]
        yj, xj = vertices[j]
        if ((yi > lon) != (yj > lon)) and \
           (lat < (xj - xi) * (lon - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def check_airspace_violation(lat: float, lon: float, alt: float,
                             zones: List[AirspaceZone]) -> Optional[AirspaceZone]:
    """检查无人机位置是否在空域区域内"""
    for zone in zones:
        if zone.altitude_floor <= alt <= zone.altitude_ceiling:
            if _point_in_polygon(lat, lon, zone.vertices):
                return zone
    return None
