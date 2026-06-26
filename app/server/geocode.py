"""
Offline reverse geocoding: point-in-polygon via shapely.STRtree
against a simplified China county GeoJSON from DataV.GeoAtlas.

Usage:
    geocoder = get_geocoder()
    if geocoder.available:
        addr = geocoder.reverse(lat, lon)
        # addr = {"province": "...", "city": "...", "county": "..."} or None
"""

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_geocoder = None

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_GEOJSON_PATH = _DATA_DIR / "geojson" / "china_county_simplified.geojson"
_REGION_INDEX_PATH = _DATA_DIR / "region_index.json"


def _init() -> dict | None:
    """Try to load region_index.json. Returns None if unavailable."""
    try:
        return json.loads(_REGION_INDEX_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        log.warning("region_index.json 不可用: %s", exc)
        return None


class OfflineGeocoder:
    """Point-in-polygon reverse geocoder using shapely STRtree."""

    def __init__(self):
        self.available = False
        self._region_index = None
        self._tree = None
        self._polys = []  # parallel to tree: [(polygon, county_name, city_name), ...]
        self._try_load()

    def _try_load(self):
        self._region_index = _init()
        if not self._region_index:
            return
        if not _GEOJSON_PATH.exists():
            log.warning("GeoJSON 数据文件不存在: %s", _GEOJSON_PATH)
            return
        try:
            from shapely import STRtree
            from shapely.geometry import shape
        except ImportError:
            log.warning("shapely 未安装，离线地理编码不可用")
            return

        try:
            geojson = json.loads(_GEOJSON_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("无法加载 GeoJSON: %s", exc)
            return

        features = geojson.get("features", [])
        polys = []
        for feat in features:
            geom = feat.get("geometry")
            props = feat.get("properties", {})
            if not geom or not props:
                continue
            try:
                poly = shape(geom)
            except Exception:
                continue
            county_name = (props.get("name") or "").strip()
            parent = props.get("parent") or {}
            city_name = (parent.get("name") or "").strip() if isinstance(parent, dict) else ""
            if not county_name:
                continue
            polys.append((poly, county_name, city_name))

        if not polys:
            log.warning("GeoJSON 无可解析的多边形")
            return

        geometries = [p[0] for p in polys]
        try:
            self._tree = STRtree(geometries)
        except Exception:
            # Fallback for older shapely
            self._tree = STRtree(geometries)
        self._polys = polys
        self.available = True
        log.info("离线地理编码器已就绪，%d 个县级行政区", len(polys))

    def reverse(self, lat: float, lon: float) -> dict | None:
        """Return {province, city, county} or None if no match."""
        if not self.available:
            return None
        from shapely.geometry import Point
        point = Point(lon, lat)
        candidates = self._tree.query(point, predicate="intersects")
        idxs = list(candidates) if not hasattr(candidates, "__iter__") else candidates
        if hasattr(idxs, "tolist"):
            idxs = idxs.tolist()
        if not idxs:
            return None

        for idx in idxs:
            poly, county_name, city_name = self._polys[idx]
            if poly.contains(point):
                return self._lookup(county_name, city_name)
        return None

    def _lookup(self, county_name: str, city_name: str) -> dict | None:
        """Match county+city against region_index to get province/city/county."""
        idx = self._region_index
        if not idx:
            return None

        # Build sanitised keys
        ckey = _strip(county_name)

        # Try exact: county_name lookup, filter by city
        entries = idx.get(county_name, [])
        if not entries:
            entries = idx.get(ckey, [])

        # Filter by city_name match
        city_stripped = _strip(city_name)
        for entry in entries:
            if city_name and _strip(entry[1]) == city_stripped:
                return {"province": entry[0], "city": entry[1], "county": entry[2]}

        # Fallback: return first entry if only one
        if len(entries) == 1:
            e = entries[0]
            return {"province": e[0], "city": e[1], "county": e[2]}

        return None


def _strip(name: str) -> str:
    """Remove administrative suffix."""
    for suffix in ("省", "市", "区", "县", "自治州", "地区", "盟", "林区",
                   "自治县", "自治旗", "镇", "乡", "街道"):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    return name


def get_geocoder() -> OfflineGeocoder:
    """Return singleton OfflineGeocoder instance."""
    global _geocoder
    if _geocoder is None:
        _geocoder = OfflineGeocoder()
    return _geocoder
