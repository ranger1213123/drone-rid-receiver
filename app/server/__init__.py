"""
中心服务器 App 工厂
"""

import os

from datetime import datetime

from flask import Flask, jsonify

from .models import init_db, close_db


def create_app(database_url: str = "sqlite:///data/center.db",
               pool_size: int = 5,
               config_overrides: dict = None) -> Flask:
    import os as _os
    from pathlib import Path as _Path

    app = Flask(
        __name__,
        template_folder=str(_Path(__file__).resolve().parent.parent.parent / "templates"),
    )
    app.config["JSON_AS_ASCII"] = False

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

    # Web session secret key
    app.secret_key = _os.environ.get("WEB_SECRET_KEY", _os.urandom(24).hex())

    # 请求结束时释放数据库会话
    @app.teardown_appcontext
    def teardown(exc):
        close_db()

    return app
