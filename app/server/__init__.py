"""
中心服务器 App 工厂
"""

import os
import secrets

from datetime import datetime

from flask import Flask, jsonify

from .models import init_db, close_db


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

    app.register_blueprint(auth_bp)
    app.register_blueprint(report_bp)
    app.register_blueprint(heartbeat_bp)
    app.register_blueprint(status_bp)
    app.register_blueprint(web_bp)
    app.register_blueprint(trajectory_bp)
    app.register_blueprint(dashboard_bp)

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

    # 请求结束时释放数据库会话
    @app.teardown_appcontext
    def teardown(exc):
        close_db()

    return app
