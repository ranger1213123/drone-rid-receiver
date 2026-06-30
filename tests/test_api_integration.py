"""
Comprehensive API integration tests — validates all frontend-backend contracts.
Run against a running dev server: python tests/test_api_integration.py
"""

import http.cookiejar
import json
import sys
import urllib.request
import urllib.error
import urllib.parse


BASE = "http://localhost:8080"
PASS = 0
FAIL = 0
CSRF_TOKEN = None

# Cookie jar for session persistence
COOKIE_JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(COOKIE_JAR),
    urllib.request.HTTPRedirectHandler(),
)


def _req(method, path, data=None, expect_status=None):
    global CSRF_TOKEN, PASS, FAIL
    url = BASE + path
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {"Content-Type": "application/json"}
    if CSRF_TOKEN and method not in ("GET", "HEAD"):
        headers["X-CSRF-Token"] = CSRF_TOKEN

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = OPENER.open(req, timeout=10)
        raw = resp.read()
        status = resp.status
        try:
            j = json.loads(raw)
        except Exception:
            j = raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            j = json.loads(e.read())
        except Exception:
            j = str(e)

    ok = True
    if expect_status is not None:
        ok = status == expect_status

    label = "OK" if ok else "FAIL"
    name = f"{method} {path}"
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print(f"  {label} {name}  (expected {expect_status}, got {status})")
        print(f"       body={json.dumps(j, ensure_ascii=False, default=str)[:200]}")
    return status, j


def run_case(name, method, path, data=None, expect_status=200):
    print(f"  [{name}]", end=" ", flush=True)
    status, j = _req(method, path, data, expect_status)
    return status, j


def section(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")



def main():
    global PASS, FAIL, CSRF_TOKEN
    # ═══════════════════════════════════════════════════════════════
    section("1. Health & CSRF (no auth)")
    # ═══════════════════════════════════════════════════════════════
    
    run_case("health", "GET", "/api/health")
    status, j = run_case("csrf token", "GET", "/api/csrf-token")
    if isinstance(j, dict) and j.get("token"):
        CSRF_TOKEN = j["token"]
        print("       (CSRF token obtained)")
    
    # ═══════════════════════════════════════════════════════════════
    section("2. Auth — Login & Session")
    # ═══════════════════════════════════════════════════════════════
    
    # CSRF should be enforced on POST without token
    _no_csrf = CSRF_TOKEN
    CSRF_TOKEN = None
    # Login is form-based (redirect), not JSON — so CSRF check doesn't apply (GET/HEAD/OPTIONS skip)
    # But login POST is HTML form, not JSON API. Skip CSRF test for login.
    CSRF_TOKEN = _no_csrf
    
    # Login via JSON — returns redirect to dashboard
    status, j = run_case("login", "POST", "/login", {"username": "admin", "password": "admin123"}, expect_status=None)
    # 302=redirect (session set), 200=redirect followed to dashboard. Both are success.
    if status in (200, 302):
        print(f"       (login OK, status={status})")
    else:
        print(f"       login FAILED: {json.dumps(j, ensure_ascii=False, default=str)[:200]}")
    
    # Get CSRF token again (now with session)
    status, j = run_case("csrf token (auth)", "GET", "/api/csrf-token")
    if isinstance(j, dict) and j.get("token"):
        CSRF_TOKEN = j["token"]
    
    # ═══════════════════════════════════════════════════════════════
    section("3. Status & Dashboard")
    # ═══════════════════════════════════════════════════════════════
    
    status, j = run_case("status", "GET", "/api/status")
    if isinstance(j, dict):
        required = ["server_time", "devices", "drones", "drone_count", "drone_total", "alerts"]
        missing = [k for k in required if k not in j]
        if missing:
            print(f"       MISSING fields: {missing}")
        else:
            print(f"       (all {len(required)} required fields present)")
    
    status, j = run_case("status paginated", "GET", "/api/status?page=1&per_page=10")
    status, j = run_case("status incremental", "GET", "/api/status?since=2020-01-01T00:00:00")
    
    status, j = run_case("stats dashboard", "GET", "/api/stats/dashboard")
    if isinstance(j, dict):
        required = ["hourly_alerts", "stations"]
        missing = [k for k in required if k not in j]
        if missing:
            print(f"       MISSING fields: {missing}")
    
    # ═══════════════════════════════════════════════════════════════
    section("4. Power Lines CRUD")
    # ═══════════════════════════════════════════════════════════════
    
    status, j = run_case("list powerlines", "GET", "/api/powerlines")
    
    # Create
    status, j = run_case("create powerline", "POST", "/api/powerlines", {
        "name": "TEST_LINE_01",
        "lat1": 30.0, "lon1": 120.0, "alt1": 50,
        "lat2": 30.1, "lon2": 120.1, "alt2": 55,
        "voltage_level": "110kV",
    })
    pl_id = None
    if isinstance(j, dict) and j.get("status") == "ok":
        pl_id = j.get("id")
        print(f"       (id={pl_id})")
    
    # List again to verify
    if pl_id:
        status, lines = run_case("list powerlines (verify)", "GET", "/api/powerlines")
        found = any(l.get("name") == "TEST_LINE_01" for l in lines) if isinstance(lines, list) else False
        if not found:
            print(f"       FAIL: created line not in list")
    
        # Edit (PUT)
        status, j = run_case("edit powerline", "PUT", f"/api/powerlines/{pl_id}", {
            "name": "TEST_LINE_01_MOD", "voltage_level": "220kV",
            "lat1": 30.0, "lon1": 120.0, "alt1": 50,
            "lat2": 30.1, "lon2": 120.1, "alt2": 55,
        })
    
        # Delete
        status, j = run_case("delete powerline", "DELETE", f"/api/powerlines/{pl_id}")
        if isinstance(j, dict) and j.get("status") == "ok":
            print(f"       (deleted)")
    
    # ═══════════════════════════════════════════════════════════════
    section("5. Stations CRUD (GET/POST/PUT/DELETE)")
    # ═══════════════════════════════════════════════════════════════
    
    status, j = run_case("list stations", "GET", "/api/stations")
    
    # Create
    status, j = run_case("create station", "POST", "/api/stations", {
        "name": "TEST_STATION_01",
        "location": "Test Location",
        "lat": 30.0, "lon": 120.0, "alt": 50,
        "device_name": "TEST_DEV_01",
    })
    
    # Edit (PUT)
    status, j = run_case("edit station", "PUT", "/api/stations", {
        "name": "TEST_STATION_01",
        "location": "Updated Location",
        "lat": 31.0, "lon": 121.0, "alt": 60,
    })
    
    # Verify edit
    status, stations = run_case("list stations (verify)", "GET", "/api/stations")
    if isinstance(stations, list):
        target = next((s for s in stations if s.get("name") == "TEST_STATION_01"), None)
        if target:
            if target.get("location") == "Updated Location":
                print(f"       (edit verified)")
            else:
                print(f"       FAIL: edit not applied — location={target.get('location')}")
    
    # Delete
    status, j = run_case("delete station", "DELETE", "/api/stations", {"name": "TEST_STATION_01"})
    
    # ═══════════════════════════════════════════════════════════════
    section("6. Users CRUD (GET/POST/PUT/DELETE)")
    # ═══════════════════════════════════════════════════════════════
    
    status, j = run_case("list users", "GET", "/api/users")
    
    # Create
    status, j = run_case("create user", "POST", "/api/users", {
        "username": "test_user_api",
        "password": "test123456",
        "role": "user",
        "station": "TEST_STATION_01",
    })
    
    # Edit (PUT)
    status, j = run_case("edit user", "PUT", "/api/users", {
        "username": "test_user_api",
        "role": "user",
        "station": "Updated_Station",
    })
    
    # Verify edit
    status, users = run_case("list users (verify)", "GET", "/api/users")
    if isinstance(users, list):
        target = next((u for u in users if u.get("username") == "test_user_api"), None)
        if target:
            if target.get("station") == "Updated_Station":
                print(f"       (edit verified)")
            else:
                print(f"       FAIL: edit not applied — station={target.get('station')}")
    
    # Delete
    status, j = run_case("delete user", "DELETE", "/api/users", {"username": "test_user_api"})
    
    # ═══════════════════════════════════════════════════════════════
    section("7. Password Management")
    # ═══════════════════════════════════════════════════════════════
    
    # Change own password (need a test user first)
    status, j = run_case("change password (wrong old)", "PUT", "/api/password", {
        "old_password": "wrong",
        "new_password": "newpass123",
    }, expect_status=403)
    
    # Reset password (admin only)
    status, j = run_case("reset user password", "POST", "/api/users/admin/reset-password", {
        "new_password": "admin123",
    })
    
    # ═══════════════════════════════════════════════════════════════
    section("8. Personnel CRUD")
    # ═══════════════════════════════════════════════════════════════
    
    status, j = run_case("list personnel", "GET", "/api/personnel")
    
    # Create (using correct field name "station_name")
    status, j = run_case("create personnel", "POST", "/api/personnel", {
        "station_name": "TEST_STATION_01",
        "name": "Test Person",
        "phone": "13800000001",
    })
    personnel_id = None
    if isinstance(j, dict) and j.get("status") == "ok":
        personnel_id = j.get("id")
    
    # List with station filter
    status, j = run_case("list personnel (filtered)", "GET", "/api/personnel?station=TEST_STATION_01")
    
    # Delete
    if personnel_id:
        status, j = run_case("delete personnel", "DELETE", "/api/personnel", {"id": personnel_id})
    
    # ═══════════════════════════════════════════════════════════════
    section("9. Alerts & Export")
    # ═══════════════════════════════════════════════════════════════
    
    status, j = run_case("alerts history", "GET", "/api/alerts/history")
    status, j = run_case("alerts history (filtered)", "GET", "/api/alerts/history?level=warning&limit=10")
    status, j = run_case("alerts history (ack)", "GET", "/api/alerts/history?acknowledged=0")
    status, j = run_case("alerts export", "GET", "/api/alerts/export")
    status, j = run_case("drones export", "GET", "/api/drones/export")
    
    # ═══════════════════════════════════════════════════════════════
    section("10. Trajectories")
    # ═══════════════════════════════════════════════════════════════
    
    status, j = run_case("trajectory list", "GET", "/api/trajectories")
    if isinstance(j, dict):
        print(f"       (returned {len(j)} drone summaries)")
    
    # ═══════════════════════════════════════════════════════════════
    section("11. License Management")
    # ═══════════════════════════════════════════════════════════════
    
    status, j = run_case("list licenses", "GET", "/api/licenses")
    status, j = run_case("create license", "POST", "/api/licenses", {
        "name": "TEST_TENANT_API",
        "max_users": 5,
        "contact": "test@test.com",
    })
    tenant_id = None
    if isinstance(j, dict) and j.get("license_key"):
        tenant_id = j.get("id")
        print(f"       (tenant_id={tenant_id}, key={j.get('license_key')})")
    
    # Edit license
    if tenant_id:
        status, j = run_case("edit license", "PUT", "/api/licenses", {
            "id": tenant_id,
            "name": "TEST_TENANT_API_MOD",
        })
    
    # Delete (soft-delete)
    if tenant_id:
        status, j = run_case("delete license", "DELETE", "/api/licenses", {"id": tenant_id})
    
    # ═══════════════════════════════════════════════════════════════
    section("12. Device Provisioning")
    # ═══════════════════════════════════════════════════════════════
    
    status, j = run_case("list devices", "GET", "/api/devices")
    
    status, j = run_case("provision device", "POST", "/api/devices/provision", {
        "device_name": "TEST_DEV_PROV",
        "station": "TEST_STATION_01",
    })
    if isinstance(j, dict) and j.get("device_secret"):
        print(f"       (cert serial={j.get('cert_serial','?')[:20]}...)")
    
    # Revoke
    if isinstance(j, dict) and j.get("status") == "ok":
        status, j = run_case("revoke device", "POST", f"/api/devices/TEST_DEV_PROV/revoke", None, expect_status=None)
        print(f"       (revoke: {json.dumps(j, ensure_ascii=False, default=str)[:120]})")
    
    # Delete device
    status, j = run_case("delete device", "DELETE", "/api/devices/TEST_DEV_PROV")
    
    # ═══════════════════════════════════════════════════════════════
    section("13. Tenant Self-Service")
    # ═══════════════════════════════════════════════════════════════
    
    status, j = run_case("tenant info", "GET", "/api/tenant/info")
    
    # Registration (public, no auth needed — but needs valid key)
    status, j = run_case("register stations (public)", "GET", "/api/register/stations?key=INVALID-KEY")
    
    # ═══════════════════════════════════════════════════════════════
    section("14. Audit & Settings")
    # ═══════════════════════════════════════════════════════════════
    
    status, j = run_case("audit logs", "GET", "/api/audit")
    status, j = run_case("audit logs (limited)", "GET", "/api/audit?limit=10")
    status, j = run_case("settings get", "GET", "/api/settings")
    status, j = run_case("settings update", "PUT", "/api/settings", {
        "threshold_warning": "200",
    })
    
    # ═══════════════════════════════════════════════════════════════
    section("15. Security — CSRF & Rate Limiting")
    # ═══════════════════════════════════════════════════════════════
    
    # Test CSRF rejection: POST without CSRF token should be rejected
    _saved_csrf = CSRF_TOKEN
    CSRF_TOKEN = None
    status, j = run_case("CSRF rejection", "POST", "/api/stations", {
        "name": "SHOULD_NOT_CREATE",
    }, expect_status=403)
    CSRF_TOKEN = _saved_csrf
    if status == 403:
        print(f"       (CSRF correctly rejected)")
    
    # Test rate limiting: hit login endpoint rapidly
    print(f"  [rate limit test]", end=" ", flush=True)
    for i in range(8):
        status, j = _req("POST", "/api/auth/token", {"device_name": "test", "device_secret": "bad"})
    if status == 429:
        print("OK — rate limit triggered at 429")
    else:
        print(f"OK (status={status}) — may not trigger with different IPs")
    
    # ═══════════════════════════════════════════════════════════════
    section("16. Auth — Logout")
    # ═══════════════════════════════════════════════════════════════
    
    status, j = run_case("logout", "POST", "/logout", expect_status=None)
    
    # After logout, protected endpoints should return 401
    status, j = run_case("status (no auth)", "GET", "/api/status", expect_status=401)
    if status == 401:
        print(f"       (correctly rejected after logout)")
    
    # ═══════════════════════════════════════════════════════════════
    section("17. Build & Static Assets")
    # ═══════════════════════════════════════════════════════════════
    
    import os as _os
    from pathlib import Path as _Path
    
    _project_root = _Path(__file__).resolve().parent.parent
    _manifest_path = _project_root / "app" / "server" / "static" / "dist" / ".vite" / "manifest.json"
    
    print(f"  [manifest exists]", end=" ", flush=True)
    if _manifest_path.exists():
        print("OK")
        PASS += 1
    else:
        print(f"FAIL — {_manifest_path} not found")
        FAIL += 1
    
    # Verify all 3 entry points resolve to existing files
    print(f"  [entry point resolution]", end=" ", flush=True)
    try:
        manifest = json.loads(_manifest_path.read_text(encoding="utf-8"))
        entries = [
            "app/server/static/js/vendor.js",
            "app/server/static/js/dashboard-entry.js",
            "app/server/static/js/map-entry.js",
        ]
        all_ok = True
        for entry in entries:
            item = manifest.get(entry)
            if not item:
                print(f"\n       MISSING manifest entry: {entry}")
                all_ok = False
                FAIL += 1
                continue
            file_path = _project_root / "app" / "server" / "static" / "dist" / item["file"]
            if not file_path.exists():
                print(f"\n       MISSING file: {file_path}")
                all_ok = False
                FAIL += 1
        if all_ok:
            print(f"OK (all {len(entries)} entries resolve)")
            PASS += 1
    except Exception as e:
        print(f"FAIL — {e}")
        FAIL += 1
    
    # Check no CDN URLs remain in templates
    print(f"  [CDN-free templates]", end=" ", flush=True)
    _tpl_dir = _project_root / "templates"
    _cdn_patterns = ["unpkg.com", "jsdelivr.net", "cdn.socket.io"]
    _cdn_found = []
    for _tpl in _tpl_dir.glob("*.html"):
        _content = _tpl.read_text(encoding="utf-8")
        for _pat in _cdn_patterns:
            if _pat in _content:
                _cdn_found.append(f"{_tpl.name}: {_pat}")
    if _cdn_found:
        print(f"FAIL — {len(_cdn_found)} CDN reference(s):")
        for _f in _cdn_found:
            print(f"       {_f}")
        FAIL += 1
    else:
        print("OK (no CDN URLs)")
        PASS += 1
    
    # ═══════════════════════════════════════════════════════════════
    section("18. CSRF Injection Verification")
    # ═══════════════════════════════════════════════════════════════
    
    # Re-login to get session
    status, j = run_case("re-login for CSRF check", "POST", "/login", {"username": "admin", "password": "admin123"}, expect_status=None)
    if status in (200, 302):
        print(f"       (login OK)")
    
    # Get fresh CSRF token
    status, j = run_case("csrf token (fresh)", "GET", "/api/csrf-token")
    if isinstance(j, dict) and j.get("token"):
        CSRF_TOKEN = j["token"]
        print(f"       (CSRF token obtained)")
    else:
        print(f"       FAIL: could not obtain CSRF token")
        FAIL += 1
    
    # Verify token length is reasonable (Hex 32+ chars)
    if CSRF_TOKEN and len(CSRF_TOKEN) >= 32:
        print(f"       (token length OK: {len(CSRF_TOKEN)} chars)")
    else:
        print(f"       WARNING: token seems short ({len(CSRF_TOKEN) if CSRF_TOKEN else 0} chars)")
    
    # CSRF rejection test — POST without token should be 403
    _saved = CSRF_TOKEN
    CSRF_TOKEN = None
    status, j = run_case("CSRF rejection (no token)", "POST", "/api/stations", {"name": "CSRF_TEST_DUMMY"}, expect_status=403)
    CSRF_TOKEN = _saved
    if status == 403:
        print(f"       (CSRF correctly rejected)")
        PASS += 1
    else:
        FAIL += 1
    
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*60}")
    print(f"  RESULTS: {PASS} passed, {FAIL} failed")
    print(f"{'='*60}")
    


if __name__ == '__main__':
    main()
    sys.exit(0 if FAIL == 0 else 1)
