"""
中心监测服务器 — 接收各杆塔设备上报数据，统一聚合展示

用法:
  python app/server.py --port 8080
  python app/server.py --db postgresql://user:pass@localhost:5432/drone_rid_center
"""

import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from app.server import create_app
from logging_config import get_logger

logger = get_logger(__name__)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Drone RID 中心监测服务器")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--db", default=None,
                        help="数据库 URL (默认: sqlite:///data/center.db, "
                             "例: postgresql://user:pass@localhost:5432/drone_rid_center)")
    args = parser.parse_args()

    db_url = args.db or "sqlite:///data/center.db"

    # 确保 data 目录存在 (SQLite 需要)
    if "sqlite" in db_url:
        db_path = db_url.replace("sqlite:///", "")
        if not os.path.isabs(db_path):
            db_path = os.path.join(SCRIPT_DIR, "..", db_path)
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    app = create_app(database_url=db_url)

    logger.info("中心监测服务器启动")
    logger.info("监听: http://%s:%s", args.host, args.port)
    logger.info("数据库: %s", db_url.split("://")[0])
    logger.info("API:  POST /api/report /api/heartbeat /api/report_alert")
    logger.info("看板: GET  /  /map")
    logger.info("按 Ctrl+C 停止")

    try:
        app.run(host=args.host, port=args.port, debug=False)
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("中心服务器已停止")


if __name__ == "__main__":
    main()
