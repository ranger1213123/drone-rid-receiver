"""
电力线模块 - 电力线数据管理

管理电力线段的加载、查询、以及与无人机的距离计算。
"""

import math
import yaml
from typing import List, Dict, Optional, Tuple


class PowerLineSegment:
    """电力线段 - 两个三维端点定义一个线段"""

    def __init__(self, name: str,
                 lat1: float, lon1: float, alt1: float,
                 lat2: float, lon2: float, alt2: float,
                 line_id: int = 0):
        self.name = name
        self.line_id = line_id
        self.lat1, self.lon1, self.alt1 = lat1, lon1, alt1
        self.lat2, self.lon2, self.alt2 = lat2, lon2, alt2

    def __repr__(self):
        return f"PowerLine({self.name}: [{self.lat1:.4f},{self.lon1:.4f},{self.alt1:.1f}] → [{self.lat2:.4f},{self.lon2:.4f},{self.alt2:.1f}])"


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
            )
            self.lines.append(segment)
        return len(self.lines)

    def find_nearest_line(self, lat: float, lon: float, alt: float
                           ) -> Tuple[Optional[PowerLineSegment], float]:
        """
        查找距离无人机最近的电力线段，返回 (线段, 垂直距离_米)

        垂直距离 = 无人机海拔高度 - 电力线在该点的海拔高度
        （正值表示无人机在电力线上方，负值表示在下方）

        为了简化计算：
        1. 将经纬度近似转换为平面坐标 (米)
        2. 计算点到线段的水平投影距离
        3. 取电力线在投影点的高程
        4. 垂直距离 = |无人机高度 - 电力线高程|
        """
        if not self.lines:
            return None, float('inf')

        best_line = None
        best_distance = float('inf')

        for line in self.lines:
            dist = self._vertical_distance_to_line(
                drone_lat=lat, drone_lon=lon, drone_alt=alt,
                line=line
            )
            if dist < best_distance:
                best_distance = dist
                best_line = line

        return best_line, best_distance

    def find_all_within(self, lat: float, lon: float, alt: float,
                        max_distance: float
                        ) -> List[Tuple[PowerLineSegment, float]]:
        """
        查找所有距离小于指定值的电力线段
        返回 [(线段, 垂直距离), ...] 按距离升序排列
        """
        results = []
        for line in self.lines:
            dist = self._vertical_distance_to_line(
                drone_lat=lat, drone_lon=lon, drone_alt=alt, line=line
            )
            if dist <= max_distance:
                results.append((line, dist))

        results.sort(key=lambda x: x[1])
        return results

    @staticmethod
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

    @staticmethod
    def _vertical_distance_to_line(drone_lat: float, drone_lon: float,
                                    drone_alt: float,
                                    line: PowerLineSegment) -> float:
        """
        计算无人机到电力线段的垂直距离 (绝对值，米)

        方法:
        1. 将经纬度转为平面坐标 (以线段起点为参考)
        2. 计算无人机水平投影到线段的最短垂直距离
        3. 在线段最近点处插值电力线高度
        4. 垂直距离 = |无人机高度 - 电力线最近点高度|
        """
        # 转换为平面坐标
        dx1, dy1 = 0.0, 0.0  # 线段起点为原点

        dx2, dy2 = PowerLineManager._latlon_to_meters(
            line.lat2, line.lon2, line.lat1, line.lon1
        )

        dx_drone, dy_drone = PowerLineManager._latlon_to_meters(
            drone_lat, drone_lon, line.lat1, line.lon1
        )

        # 线段向量
        seg_dx = dx2 - dx1
        seg_dy = dy2 - dy1

        # 线段长度的平方
        seg_len_sq = seg_dx * seg_dx + seg_dy * seg_dy

        if seg_len_sq < 1e-6:
            # 线段退化为点
            line_alt = line.alt1
        else:
            # 计算投影参数 t (0~1 表示投影在线段上)
            t = ((dx_drone - dx1) * seg_dx + (dy_drone - dy1) * seg_dy) / seg_len_sq
            t = max(0.0, min(1.0, t))  # 钳制到线段范围内

            # 插值电力线高度
            line_alt = line.alt1 + t * (line.alt2 - line.alt1)

        # 垂直距离 = |无人机海拔高度 - 电力线在该点的高度|
        return abs(drone_alt - line_alt)


# ─────────────────── 距离计算独立函数 ───────────────────

def calculate_vertical_distance(drone_alt: float, line_alt: float) -> float:
    """简单垂直距离计算 (两海拔高度之差)"""
    return abs(drone_alt - line_alt)


def calculate_3d_distance(drone_lat: float, drone_lon: float, drone_alt: float,
                          line_lat: float, line_lon: float, line_alt: float) -> float:
    """计算无人机到电力线点的三维距离 (米)"""
    dx, dy = PowerLineManager._latlon_to_meters(
        drone_lat, drone_lon, line_lat, line_lon
    )
    dz = drone_alt - line_alt
    return math.sqrt(dx * dx + dy * dy + dz * dz)
