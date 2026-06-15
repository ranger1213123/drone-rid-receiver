"""
原始 RID 报文存档 — 哈希链防篡改 + 定期清理

每条原始报文存入 raw_messages 表，通过 SHA256 链式哈希保证不可篡改性。
首条记录的 prev_hash 为 64 个零，后续每条: current = SHA256(prev + raw_data + timestamp)
"""

import hashlib
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from logging_config import get_logger
from storage.database import Database

logger = get_logger(__name__)


def compute_hash(prev_hash: str, raw_data: bytes, timestamp: str) -> str:
    h = hashlib.sha256()
    h.update(prev_hash.encode("ascii"))
    h.update(raw_data)
    h.update(timestamp.encode("ascii"))
    return h.hexdigest()


class RawArchiveManager:
    """原始报文存档管理器 — 哈希链 + 定期清理"""

    def __init__(self, db: Database, retention_days: int = 30,
                 cleanup_interval: int = 86400):
        self.db = db
        self.retention_days = retention_days
        self.cleanup_interval = cleanup_interval
        self._last_hashes: Dict[str, str] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._thread.start()
        logger.info("原始报文存档已启动 (保留 %d 天)", self.retention_days)

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def archive(self, drone_id: str, raw_data: bytes, protocol: str,
                msg_type: str, mac: str = "", rssi: int = 0) -> int:
        """存档一条原始报文，返回记录 ID"""
        prev_hash = self._last_hashes.get(drone_id, "0" * 64)
        timestamp = datetime.now(timezone.utc).isoformat()
        current_hash = compute_hash(prev_hash, raw_data, timestamp)
        self._last_hashes[drone_id] = current_hash
        return self.db.insert_raw_message(
            drone_id=drone_id, timestamp=timestamp,
            protocol=protocol, msg_type=msg_type,
            raw_data=raw_data.hex(), mac=mac, rssi=rssi,
            prev_hash=prev_hash, current_hash=current_hash,
        )

    def verify_chain(self, drone_id: str) -> Tuple[bool, int, Optional[int]]:
        """验证哈希链完整性 (is_intact, checked_count, break_at_id)"""
        records = self.db.get_raw_message_chain(drone_id)
        if not records:
            return True, 0, None

        prev = "0" * 64
        for i, rec in enumerate(records):
            expected = compute_hash(
                prev,
                bytes.fromhex(rec["raw_data"]),
                rec["timestamp"],
            )
            if expected != rec["current_hash"]:
                logger.warning("哈希链断裂: drone=%s record_id=%s pos=%d",
                               drone_id, rec["id"], i)
                return False, i + 1, rec["id"]
            prev = expected

        return True, len(records), None

    def verify_all(self) -> Dict[str, Tuple[bool, int]]:
        """验证所有无人机的哈希链"""
        drone_ids = self.db.get_all_drones_with_raw_data()
        results = {}
        for did in drone_ids:
            ok, count, _ = self.verify_chain(did)
            results[did] = (ok, count)
        return results

    def cleanup(self):
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(days=self.retention_days)).isoformat()
        self.db.delete_raw_messages_before(cutoff)
        logger.info("原始报文清理完成 (cutoff: %s)", cutoff[:19])

    def _cleanup_loop(self):
        while self._running:
            time.sleep(self.cleanup_interval)
            if not self._running:
                break
            try:
                self.cleanup()
            except Exception as e:
                logger.error("原始报文清理失败: %s", e)
