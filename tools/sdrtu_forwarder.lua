--[[
 SDRTU UART → MQTT 转发脚本 (生产版)
 适用: DevelopLink SDRTU 边缘网关 (ZL4xx/ZL5xx 系列)
 功能: 从 UART 读取 ESP32 Remote ID 数据, 透传到 MQTT Broker

 特性:
   - mTLS 双向认证
   - LWT 遗嘱消息 (设备离线自动通知)
   - 配置外置 (优先 config 模块, 回退内置默认值)
   - 心跳/数据自动区分 QoS 0/1

 依赖模块: uart, mqtt, sjson (均内置)
 参考文档: wiki.developlink.cloud/sdrtu_dev/api
--]]

-- ═══════════ 配置加载 (外置优先, 内置回退) ═══════════
local cfg = {}
local cfg_ok, cfg_raw = pcall(config.get, "rid_forwarder")
if cfg_ok and type(cfg_raw) == "table" then
    cfg = cfg_raw
    log.info("已加载外置配置: rid_forwarder")
else
    log.info("未找到外置配置, 使用内置默认值")
end

-- UART
local UART_ID       = cfg.uart_id       or 1
local BAUD          = cfg.baud          or 115200
local DATA_BITS     = cfg.data_bits     or 8
local PARITY        = cfg.parity        or uart.PAR_NONE
local STOP_BITS     = cfg.stop_bits     or uart.STOP_1

-- MQTT Broker
local MQTT_BROKER   = cfg.broker        or "emqx.drone-rid.svc.cluster.local"
local MQTT_PORT     = cfg.port          or 8883
local MQTT_USE_TLS  = (cfg.tls ~= false)  -- 默认启用以适应生产环境; 开发可设 cfg.tls=false
local MQTT_USER     = cfg.username      or ""
local MQTT_PASS     = cfg.password      or ""

-- TLS 证书路径 (部署到 SDRTU 文件系统)
local TLS_CA_CERT   = cfg.ca_cert       or "/etc/sdrtu/certs/ca.crt"
local TLS_CLIENT_CERT = cfg.client_cert or "/etc/sdrtu/certs/client.crt"
local TLS_CLIENT_KEY  = cfg.client_key  or "/etc/sdrtu/certs/client.key"

-- 设备身份
local MQTT_CLIENT_ID = "sdrtu_" .. (cfg.device_sn or misc.imei())

-- ═══════════ 初始化 ═══════════
log.info("===== SDRTU RID Forwarder (生产版) =====")
log.info("设备ID: %s", MQTT_CLIENT_ID)

-- 串口初始化
uart.setup(UART_ID, BAUD, DATA_BITS, PARITY, STOP_BITS)
log.info("UART%d ready: %d baud", UART_ID, BAUD)

-- MQTT 连接
local mqtt_opts = {
    client_id = MQTT_CLIENT_ID,
    username  = MQTT_USER,
    password  = MQTT_PASS,
    keepalive = 60,
}
if MQTT_USE_TLS then
    mqtt_opts.tls = {
        ca_cert   = TLS_CA_CERT,
        cert      = TLS_CLIENT_CERT,
        key       = TLS_CLIENT_KEY,
    }
    log.info("MQTT TLS 已启用")
end

-- LWT 遗嘱消息 — 设备异常离线时自动发布
local lwt_topic = "drone/" .. MQTT_CLIENT_ID .. "/status"
mqtt.set_will(lwt_topic, "offline", 1)

mqtt.connect(MQTT_BROKER, MQTT_PORT, mqtt_opts)
log.info("MQTT connecting: %s:%d (TLS=%s)", MQTT_BROKER, MQTT_PORT, tostring(MQTT_USE_TLS))

-- ═══════════ MQTT 事件 ═══════════
mqtt.on("connect", function()
    log.info("MQTT connected: %s", MQTT_CLIENT_ID)
    -- 发布上线状态
    mqtt.publish(lwt_topic, "online", 1)
end)

mqtt.on("disconnect", function(reason)
    log.warn("MQTT disconnected: %s, reconnecting...", reason)
    -- SDRTU MQTT 库自动重连; 重连成功后 LWT 会被重置
end)

-- ═══════════ UART 数据处理 ═══════════
uart.on(UART_ID, "receive", function(data)
    -- 按行分割
    local pos = 0
    while true do
        local nl = data:find("\n", pos + 1, true)
        local cr = data:find("\r", pos + 1, true)
        local term = nl
        if cr and (not nl or cr < nl) then term = cr end
        if not term then break end

        local line = data:sub(pos + 1, term - 1)
        pos = term
        -- 跳过空行
        if #line < 2 then goto continue end

        -- 解析 JSON
        local ok, json = pcall(sjson.decode, line)
        if not ok or not json or not json.devId then
            log.debug("skip non-ESP32 line: %s", line:sub(1, 60))
            goto continue
        end

        local dev_id = json.devId
        local topic = "drone/" .. dev_id .. "/raw"

        -- 判断消息类型: 心跳(无 data) vs 数据(有 data)
        local qos = 0
        if json.data and type(json.data) == "table" then
            qos = 1  -- 无人机位置数据: QoS 1
        end

        mqtt.publish(topic, line, qos)
        log.debug("MQTT → %s (qos=%d)", topic, qos)

        ::continue::
    end
end)

-- ═══════════ 运行 ═══════════
log.info("SDRTU RID Forwarder ready, waiting for ESP32 data...")
-- SDRTU 事件循环由固件管理, 脚本无需显式 loop
