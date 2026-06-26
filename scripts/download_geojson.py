"""Download China county GeoJSON from DataV.GeoAtlas API.

Recursively fetches province → city → county boundaries from
https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json

Writes data/geojson/china_county_simplified.geojson (~10 MB).
"""

import json
import sys
import time
from pathlib import Path
from typing import Any

import requests

API = "https://geo.datav.aliyun.com/areas_v3/bound"
OUTPUT = Path(__file__).resolve().parent.parent / "data" / "geojson" / "china_county_simplified.geojson"
HEADERS = {"User-Agent": "DroneRID/geojson-download"}

session = requests.Session()
session.headers.update(HEADERS)

# Province adcode → name mapping (from DataV 100000_full.json)
# We build this dynamically but maintain a fallback for common codes.
PROVINCE_NAMES: dict[int, str] = {
    110000: "北京市", 120000: "天津市", 130000: "河北省", 140000: "山西省",
    150000: "内蒙古自治区", 210000: "辽宁省", 220000: "吉林省", 230000: "黑龙江省",
    310000: "上海市", 320000: "江苏省", 330000: "浙江省", 340000: "安徽省",
    350000: "福建省", 360000: "江西省", 370000: "山东省", 410000: "河南省",
    420000: "湖北省", 430000: "湖南省", 440000: "广东省", 450000: "广西壮族自治区",
    460000: "海南省", 500000: "重庆市", 510000: "四川省", 520000: "贵州省",
    530000: "云南省", 540000: "西藏自治区", 610000: "陕西省", 620000: "甘肃省",
    630000: "青海省", 640000: "宁夏回族自治区", 650000: "新疆维吾尔自治区",
    710000: "台湾省", 810000: "香港特别行政区", 820000: "澳门特别行政区",
}


def fetch(adcode: int) -> dict[str, Any]:
    """Fetch GeoJSON for an adcode with _full suffix to include children."""
    url = f"{API}/{adcode}_full.json"
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def collect_counties() -> list[dict[str, Any]]:
    """Recursively collect all county-level features from DataV API."""
    # Fetch root to get province list
    print("Fetching province list...")
    root = fetch(100000)
    province_features = root.get("features", [])

    provinces = []
    for f in province_features:
        props = f.get("properties", {})
        adcode = props.get("adcode")
        name = props.get("name", "")
        if props.get("level") == "province" and adcode:
            provinces.append((adcode, name))
            PROVINCE_NAMES[adcode] = name

    print(f"Found {len(provinces)} provinces")

    all_counties = []
    total_cities = 0
    skipped_city_counties = 0

    for prov_adcode, prov_name in provinces:
        try:
            prov_data = fetch(prov_adcode)
        except Exception as exc:
            print(f"  Skip province {prov_name} ({prov_adcode}): {exc}")
            continue

        features = prov_data.get("features", [])
        city_features = [f for f in features if f.get("properties", {}).get("level") == "city"]
        district_features = [f for f in features if f.get("properties", {}).get("level") == "district"]

        if district_features and not city_features:
            # Municipality: districts ARE counties
            for f in district_features:
                props = dict(f.get("properties", {}))
                props["parent"] = {"name": prov_name}
                f["properties"] = props
                all_counties.append(f)
            print(f"  {prov_name}: {len(district_features)} districts (municipality)")
            continue

        # Also capture province-directly-administered county-level entities
        for f in district_features:
            props = dict(f.get("properties", {}))
            props["parent"] = {"name": prov_name}
            f["properties"] = props
            all_counties.append(f)
            skipped_city_counties += 1

        # Regular province: iterate cities
        for cf in city_features:
            city_props = cf.get("properties", {})
            city_adcode = city_props.get("adcode")
            city_name = city_props.get("name", "")
            if not city_adcode:
                continue
            try:
                city_data = fetch(city_adcode)
            except Exception:
                # City without districts (e.g. 东莞市, 中山市) — add itself as county
                props = dict(city_props)
                props["parent"] = {"name": prov_name}
                cf["properties"] = props
                all_counties.append(cf)
                skipped_city_counties += 1
                continue
            for f in city_data.get("features", []):
                level = f.get("properties", {}).get("level")
                if level in ("district", "county"):
                    props = dict(f.get("properties", {}))
                    props["parent"] = {"name": city_name}
                    f["properties"] = props
                    all_counties.append(f)
            total_cities += 1
            time.sleep(0.15)  # be polite to the CDN

        print(f"  {prov_name}: {len(city_features)} cities")
        time.sleep(0.3)

    print(f"Total: {len(all_counties)} counties from {len(provinces)} provinces, "
          f"{total_cities} cities, {skipped_city_counties} direct-administered")
    return all_counties


def simplify_geojson(features: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce precision of coordinates to 4 decimal places (~11m) to shrink file size."""
    import copy
    result = {"type": "FeatureCollection", "features": []}

    def _round_coords(coord):
        if isinstance(coord[0], (list, tuple)):
            return [_round_coords(c) for c in coord]
        return [round(coord[0], 4), round(coord[1], 4)]

    for f in features:
        f = copy.deepcopy(f)
        geom = f.get("geometry", {})
        if geom.get("type") == "Polygon":
            geom["coordinates"] = [_round_coords(ring) for ring in geom["coordinates"]]
        elif geom.get("type") == "MultiPolygon":
            geom["coordinates"] = [[_round_coords(ring) for ring in poly] for poly in geom["coordinates"]]
        result["features"].append(f)
    return result


if __name__ == "__main__":
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    counties = collect_counties()
    if not counties:
        sys.exit("No county features collected — aborting.")
    geojson = simplify_geojson(counties)
    OUTPUT.write_text(json.dumps(geojson, ensure_ascii=False), encoding="utf-8")
    size_kb = OUTPUT.stat().st_size / 1024
    print(f"Saved {size_kb:.0f} KB to {OUTPUT}")
