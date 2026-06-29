"""
中心服务器 App 工厂
"""

import json
import os
import secrets

from datetime import datetime

from flask import Flask, jsonify
from flask_socketio import SocketIO
from markupsafe import Markup

from .models import init_db, close_db

# ── WebSocket 实时推送 (模块级，蓝图通过 from . import socketio 引用) ──
socketio = SocketIO()


def create_app(database_url: str = "sqlite:///data/center.db",
               pool_size: int = 5,
               config_overrides: dict = None) -> Flask:
    import os as _os
    from pathlib import Path as _Path

    # ── 后台线程: 定期清理过期设备 & 无人机 ──
    import threading as _threading
    import time as _time
    from .models import mark_stale_devices as _mark_stale_devices

    def _stale_cleaner():
        while True:
            _time.sleep(60)
            try:
                _mark_stale_devices(timeout_seconds=120)
            except Exception:
                pass  # 数据库可能尚未初始化，忽略

    _stale_thread = _threading.Thread(target=_stale_cleaner, daemon=True, name="stale-cleaner")
    _stale_thread.start()

    app = Flask(
        __name__,
        template_folder=str(_Path(__file__).resolve().parent.parent.parent / "templates"),
    )
    app.config["JSON_AS_ASCII"] = False

    manifest_path = _Path(app.static_folder) / "dist" / ".vite" / "manifest.json"
    _manifest_cache = {"data": None, "mtime": 0}

    def _load_vite_manifest():
        try:
            mtime = manifest_path.stat().st_mtime
            if _manifest_cache["data"] is not None and _manifest_cache["mtime"] == mtime:
                return _manifest_cache["data"]
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            _manifest_cache["data"] = data
            _manifest_cache["mtime"] = mtime
            return data
        except FileNotFoundError:
            app.logger.warning("Vite manifest 不存在: %s，请先运行 npm run build", manifest_path)
        except Exception as exc:
            app.logger.warning("读取 Vite manifest 失败: %s", exc)
        _manifest_cache["data"] = {}
        return {}

    # Dev-mode Vite server check (cached)
    _vite_dev_checked = False
    _vite_dev_available = False

    def _check_vite_dev():
        nonlocal _vite_dev_checked, _vite_dev_available
        if _vite_dev_checked:
            return _vite_dev_available
        _vite_dev_checked = True
        try:
            import urllib.request
            req = urllib.request.Request("http://localhost:3000/@vite/client", method="HEAD")
            urllib.request.urlopen(req, timeout=0.5)
            _vite_dev_available = True
        except Exception:
            pass
        return _vite_dev_available

    def _vite_asset(entry: str) -> str:
        manifest = _load_vite_manifest()
        item = manifest.get(entry)
        if not item:
            return ""
        return f"/static/dist/{item['file']}"

    def _vite_tags(entry: str) -> Markup:
        is_dev = os.environ.get("FLASK_ENV") == "development" or os.environ.get("VITE_DEV") == "1"
        if is_dev and _check_vite_dev():
            tags = [
                '<script type="module" src="http://localhost:3000/@vite/client"></script>',
                f'<script type="module" src="http://localhost:3000{entry}"></script>',
            ]
            return Markup("\n".join(tags))

        manifest = _load_vite_manifest()
        item = manifest.get(entry)
        if not item:
            return Markup("")
        tags = []
        for css_file in item.get("css", []):
            tags.append(f'<link rel="stylesheet" href="/static/dist/{css_file}">')
        tags.append(f'<script type="module" src="/static/dist/{item["file"]}"></script>')
        return Markup("\n".join(tags))

    @app.context_processor
    def inject_vite_helpers():
        return {
            "vite_asset": _vite_asset,
            "vite_tags": _vite_tags,
            "tile_urls": {
                "standard": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
                "satellite": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                "terrain": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
            },
        }

    # 生产环境安全检查: JWT_SECRET_KEY 必须配置
    _jwt_secret = os.environ.get("JWT_SECRET_KEY", "")
    _is_prod = os.environ.get("FLASK_ENV") == "production" or os.environ.get("APP_ENV") == "production"
    if (not _jwt_secret or _jwt_secret == "dev-secret-change-me") and _is_prod:
        raise RuntimeError(
            "JWT_SECRET_KEY 未配置或使用默认值，生产环境禁止启动。"
            "请设置环境变量 JWT_SECRET_KEY=<随机密钥>"
        )
    if not _jwt_secret or _jwt_secret == "dev-secret-change-me":
        app.logger.warning("WARNING: JWT_SECRET_KEY 使用默认值，仅限开发环境！生产环境必须设置此环境变量")

    # 注入安全配置 (环境变量 → Flask app.config)
    for key in ("JWT_SECRET_KEY", "JWT_EXPIRE_SECONDS", "DEVICE_SECRETS"):
        val = os.environ.get(key)
        if val is not None:
            app.config[key] = val
    if config_overrides:
        app.config.update(config_overrides)

    # 解析 DEVICE_SECRETS JSON
    raw = app.config.get("DEVICE_SECRETS", "{}")
    if isinstance(raw, str):
        import json as _json
        try:
            app.config["DEVICE_SECRETS"] = _json.loads(raw)
        except _json.JSONDecodeError:
            app.config["DEVICE_SECRETS"] = {}

    # 初始化数据库
    init_db(database_url, pool_size=pool_size)

    # 首次启动: 创建默认管理员
    from .models import count_admin_users, upsert_web_user
    if count_admin_users() == 0:
        upsert_web_user("admin", "admin123", "admin", "")
        app.logger.info("已创建默认管理员账户 admin / admin123")

    # ── 健康检查 (无需鉴权，供 K8s 探针使用) ──
    @app.route("/api/health")
    def health():
        return jsonify({
            "status": "ok",
            "time": datetime.now().isoformat(),
        })

    # 注册 API 蓝图
    from .auth import bp as auth_bp
    from .api_report import bp as report_bp
    from .api_heartbeat import bp as heartbeat_bp
    from .api_status import bp as status_bp
    from .api_web import bp as web_bp
    from .api_trajectory import bp as trajectory_bp
    from .dashboard import bp as dashboard_bp
    from .tile_server import bp as tile_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(report_bp)
    app.register_blueprint(heartbeat_bp)
    app.register_blueprint(status_bp)
    app.register_blueprint(web_bp)
    app.register_blueprint(trajectory_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(tile_bp)

    # Web session secret key — 优先环境变量，其次持久化文件
    _web_secret = _os.environ.get("WEB_SECRET_KEY", "")
    if not _web_secret:
        _key_file = _Path(__file__).resolve().parent.parent.parent / "data" / ".session_key"
        try:
            if _key_file.exists():
                _web_secret = _key_file.read_text().strip()
            else:
                _web_secret = secrets.token_hex(32)
                _key_file.parent.mkdir(parents=True, exist_ok=True)
                _key_file.write_text(_web_secret)
        except Exception:
            _web_secret = secrets.token_hex(32)
            app.logger.warning("无法持久化 session key，使用临时密钥(重启后所有用户需重新登录)")
    app.secret_key = _web_secret

    # WebSocket 实时推送
    socketio.init_app(app, cors_allowed_origins='*')

    # 请求结束时释放数据库会话
    @app.teardown_appcontext
    def teardown(exc):
        close_db()

    return app
