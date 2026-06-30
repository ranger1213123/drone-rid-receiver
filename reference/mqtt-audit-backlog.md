# MQTT/Outbox 审计遗留问题 — 待下次迭代修复

审计日期: 2026-06-26

---

## 2. 心跳间隔受 sleep 粒度影响（低）

**位置**: `core/backhaul.py:264`

`_health_loop` 的 sleep 间隔动态调整（有积压 1s，空闲 5s），heartbeat_interval=30s 时实际间隔 30-35s。不影响功能，但精确度略差。

**建议**: 将心跳定时器独立出 drain 循环，或用 `threading.Timer`。

---

## 3. Consumer 共享订阅 session 持久性不确定性（低）

**位置**: `app/mqtt_consumer.py:200-201`

`clean_session=False` 配合共享订阅（`$share/consumer/...`），不同 EMQX 版本对共享订阅的 session 恢复行为不一致。部分版本重连后 retained session 可能不恢复共享订阅。当前 `on_connect` 显式重新订阅作为兜底，但若 broker 先投递积压消息再处理 re-subscribe，可能短暂重复投递。影响不大（批量写入 `ON CONFLICT DO UPDATE` 幂等）。

**建议**: K8s 部署时将 consumer 的 `clean_session` 设为 True，完全依赖 `on_connect` 重新订阅。

---

## 4c. config_sync 每次调用都 close_db（低）

**位置**: `app/mqtt_consumer.py:755-761`

`_handle_config_sync` 在 finally 里调用 `close_db()`，导致 scoped session 被频繁关闭重建。开销不大但无必要。

**建议**: 移除 finally 中的 `close_db()`，让 scoped session 自然管理生命周期。

---

## 5. 证书过期监控与在线轮换（中）

**位置**: `app/server/cert_manager.py`

- CA 证书 20 年、设备证书 10 年，无到期前告警
- `revoke_device_cert()` 只改 DB，已连接设备的 MQTT 连接不断（除非 EMQX 配了 OCSP/CRL）
- 边缘设备从文件加载证书（`mqtt_client.py:73-77`），进程不重启就不会用新证书
- 无在线轮换流程（签发新证 → 边缘设备接收 → 热切换）

**建议**:
1. 添加证书到期前 30 天日志告警
2. 通过 MQTT 下行 `cmd/{device}/config` 推送新证书，边缘设备写入文件后断开重连
3. 轮换期间新旧证书同时有效（过渡期），EMQX 侧允许

---

## 6. Consumer 降级到 HTTP（中）

**位置**: `core/backhaul.py`

当 MQTT broker 不可用时，边缘设备仅将数据积压到 outbox，不降级到 HTTP 回传。云端 HTTP API 已完备（`/api/report`、`/api/heartbeat`、`/api/auth/token`），但 `BackhaulManager` 无 HTTP 客户端逻辑。

**建议**: 在 `BackhaulManager` 中添加 HTTP 回传降级——MQTT 发布失败时将 payload 通过 HTTP POST 发送到云端，JWT 令牌来自 `config.yaml` 中的 `backhaul.auth` 配置。

---

## 已修复（本次）

| # | 问题 | 文件 |
|---|------|------|
| 1 | `edge_backhaul.py` 双重心跳 | `app/edge_backhaul.py:91-112` → 移除 main loop 中重复的心跳+drain |
| 4a | `_flush()` 先清 buffer 再写 DB，失败丢数据 | `app/mqtt_consumer.py:779-852` → 仅在 commit 成功后清理 buffer |
| 4b | `_on_message` 无顶层 try/except | `app/mqtt_consumer.py:276` → 添加异常捕获 + 日志 |
