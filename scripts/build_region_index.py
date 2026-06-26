"""Build data/region_index.json mapping county_name -> [province, city, county]
from region-data.js (GB/T 2260 China administrative divisions)."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGION_JS = ROOT / "app" / "server" / "static" / "js" / "region-data.js"
OUTPUT = ROOT / "data" / "region_index.json"


def _strip_suffix(name: str) -> str:
    """Return name without administrative suffix."""
    for suffix in ("省", "市", "区", "县", "自治州", "地区", "盟", "林区", "自治县",
                   "自治旗", "镇", "乡", "街道"):
        if name.endswith(suffix) and name != suffix:
            return name[:-len(suffix)]
    return name


def build() -> dict:
    raw = REGION_JS.read_text(encoding="utf-8")
    m = re.search(r"export default (\[.*?\n\]);", raw, re.DOTALL)
    if not m:
        sys.exit("Cannot parse region-data.js: export default array not found")
    data = json.loads(m.group(1))
    # {county_full_name: [{province, city, county}, ...]} — multi-value for name collisions
    index = {}
    for province, cities in data:
        for city, counties in cities:
            for county in counties:
                entry = [province, city, county]
                index.setdefault(county, []).append(entry)
                # Also index without suffix for fuzzy matching
                stripped = _strip_suffix(county)
                if stripped != county:
                    existing = index.setdefault(stripped, [])
                    if entry not in existing:
                        existing.append(entry)
    return index


if __name__ == "__main__":
    index = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(index)} entries to {OUTPUT}")
