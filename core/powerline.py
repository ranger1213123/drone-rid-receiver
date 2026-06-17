"""
电力线模块 - 电力线数据管理

管理电力线段的加载、查询、以及与无人机的距离计算。
参考标准: GB 50545-2010《110kV～750kV架空输电线路设计规范》
"""

import math
import yaml
from typing import List, Dict, Optional, Tuple

# GB 50545-2010 非居民区最小对地距离 (m)
VOLTAGE_CLEARANCE = {
    '10kV': 5.5,
    '35kV': 6.5,
    '66kV': 7.0,
    '110kV': 7.0,
    '220kV': 8.5,
    '330kV': 9.5,
    '500kV': 14.0,
    '750kV': 19.5,
    '±800kV': 21.0,
    '1000kV': 27.0,
}

VOLTAGE_LEVELS = list(VOLTAGE_CLEARANCE.keys())


class PowerLineSegment:
    """电力线段 - 两个三维端点定义一个线段

    缓存端点 2 相对于端点 1 的平面偏移和线段长度平方，
    避免对同一线段重复进行经纬度→米制转换。
    """

    __slots__ = ('name', 'line_id', 'lat1', 'lon1', 'alt1',
                 'lat2', 'lon2', 'alt2', 'voltage_level',
                 '_cache_dx2', '_cache_dy2', '_cache_len_sq')

    def __init__(self, name: str,
                 lat1: float, lon1: float, alt1: float,
                 lat2: float, lon2: float, alt2: float,
                 line_id: int = 0, voltage_level: str = ''):
        self.name = name
        self.line_id = line_id
        self.lat1, self.lon1, self.alt1 = lat1, lon1, alt1
        self.lat2, self.lon2, self.alt2 = lat2, lon2, alt2
        self.voltage_level = voltage_level
        self._cache_dx2: Optional[float] = None
        self._cache_dy2: Optional[float] = None
        self._cache_len_sq: Optional[float] = None

    def ensure_cache(self):
        """惰性计算并缓存端点 2 相对于端点 1 的平面偏移"""
        if self._cache_dx2 is not None:
            return
        self._cache_dx2, self._cache_dy2 = _latlon_to_meters(
            self.lat2, self.lon2, self.lat1, self.lon1
        )
        self._cache_len_sq = (self._cache_dx2 * self._cache_dx2 +
                              self._cache_dy2 * self._cache_dy2)

    def invalidate_cache(self):
        """坐标变更后清除缓存"""
        self._cache_dx2 = None
        self._cache_dy2 = None
        self._cache_len_sq = None

    def get_clearance(self) -> Optional[float]:
        """返回该电压等级对应的 GB 50545 最小对地距离，无等级则返回 None"""
        return VOLTAGE_CLEARANCE.get(self.voltage_level)

    def __repr__(self):
        vl = f" {self.voltage_level}" if self.voltage_level else ""
        return (f"PowerLine({self.name}{vl}: "
                f"[{self.lat1:.4f},{self.lon1:.4f},{self.alt1:.1f}] → "
                f"[{self.lat2:.4f},{self.lon2:.4f},{self.alt2:.1f}])")


class PowerLineManager:
    """电力线管理器 - 加载、存储、查询"""

    def __init__(self):
        self.lines: List[PowerLineSegment] = []

    def load_from_yaml(self, yaml_path: str):
        """从 YAML 配置文件加载电力线"""
        with open(yaml_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        self.lines = []
        line_id = 1
        for line_data in config.get("power_lines", []):
            segment = PowerLineSegment(
                name=line_data["name"],
                lat1=line_data["lat1"], lon1=line_data["lon1"],
                alt1=line_data["alt1"],
                lat2=line_data["lat2"], lon2=line_data["lon2"],
                alt2=line_data["alt2"],
                line_id=line_id,
                voltage_level=line_data.get("voltage_level", ""),
            )
            self.lines.append(segment)
            line_id += 1

        return len(self.lines)

    def load_from_list(self, lines: List[Dict]):
        """从字典列表加载电力线 (用于数据库恢复)"""
        self.lines = []
        for line_data in lines:
            segment = PowerLineSegment(
                name=line_data["name"],
                lat1=line_data["lat1"], lon1=line_data["lon1"],
                alt1=line_data["alt1"],
                lat2=line_data["lat2"], lon2=line_data["lon2"],
                alt2=line_data["alt2"],
                line_id=line_data.get("id", 0),
                voltage_level=line_data.get("voltage_level", ""),
            )
            self.lines.append(segment)
        return len(self.lines)

    def find_nearest_line(self, lat: float, lon: float, alt: float
                           ) -> Tuple[Optional[PowerLineSegment], float]:
        """
        查找距离无人机最近的电力线段，返回 (线段, 3D欧氏距离_米)

        使用三维欧氏距离: sqrt(水平距离² + 垂直距离²)
        线段平面坐标会被缓存以避免重复转换。
        """
        if not self.lines:
            return None, float('inf')

        best_line = None
        best_distance = float('inf')

        for line in self.lines:
            line.ensure_cache()
            dist = _distance_to_line(lat, lon, alt, line)
            if dist < best_distance:
                best_distance = dist
                best_line = line

        return best_line, best_distance

    def find_all_within(self, lat: float, lon: float, alt: float,
                        max_distance: float
                        ) -> List[Tuple[PowerLineSegment, float]]:
        """
        查找所有距离小于指定值的电力线段
        返回 [(线段, 3D欧氏距离), ...] 按距离升序排列
        """
        results = []
        for line in self.lines:
            line.ensure_cache()
            dist = _distance_to_line(lat, lon, alt, line)
            if dist <= max_distance:
                results.append((line, dist))

        results.sort(key=lambda x: x[1])
        return results


# ─────────────────── 经纬度转换 ───────────────────

def _latlon_to_meters(lat: float, lon: float,
                      ref_lat: float, ref_lon: float) -> Tuple[float, float]:
    """
    将经纬度转换为以参考点为原点的近似平面坐标 (米)
    使用 WGS84 简化公式
    """
    lat_mid = (ref_lat + lat) / 2.0
    meters_per_deg_lat = 111132.954 - 559.822 * math.cos(2 * math.radians(lat_mid))
    meters_per_deg_lon = (math.pi / 180) * 6378137.0 * math.cos(math.radians(lat_mid))

    dy = (lat - ref_lat) * meters_per_deg_lat
    dx = (lon - ref_lon) * meters_per_deg_lon
    return dx, dy


# ─────────────────── 3D 欧氏距离 ───────────────────

def _distance_to_line(drone_lat: float, drone_lon: float, drone_alt: float,
                      line: PowerLineSegment) -> float:
    """
    计算无人机到电力线段的三维欧氏距离 (米)

    1. 将经纬度转为平面坐标 (以线段起点为参考)
    2. 找到无人机在线段水平投影上的最近点
    3. 计算水平距离 + 垂直距离 → 3D 欧氏距离

    使用线段已缓存的端点 2 平面偏移。
    """
    # 线段端点 2 的平面偏移 (已缓存)
    dx2, dy2 = line._cache_dx2, line._cache_dy2
    seg_len_sq = line._cache_len_sq

    # 无人机相对线段起点的平面偏移
    dx_drone, dy_drone = _latlon_to_meters(
        drone_lat, drone_lon, line.lat1, line.lon1
    )

    if seg_len_sq < 1e-6:
        # 线段退化为点 — 3D 距离 = sqrt(水平² + 垂直²)
        h_sq = dx_drone * dx_drone + dy_drone * dy_drone
        v = drone_alt - line.alt1
        return math.sqrt(h_sq + v * v)

    # 投影参数 t ∈ [0, 1]
    t = (dx_drone * dx2 + dy_drone * dy2) / seg_len_sq
    t = max(0.0, min(1.0, t))

    # 最近点坐标 + 插值高度
    closest_x = t * dx2
    closest_y = t * dy2
    line_alt = line.alt1 + t * (line.alt2 - line.alt1)

    # 3D 欧氏距离
    h_sq = (dx_drone - closest_x) ** 2 + (dy_drone - closest_y) ** 2
    v = drone_alt - line_alt
    return math.sqrt(h_sq + v * v)
