-- =============================================================================
-- Drone RID Edge Receiver — SDRTU (DevelopLink ZL4xx/ZL5xx)
-- ESP32 串口 → 透传 JSON 行 → MQTT → 云端
-- =============================================================================
-- 接线: ESP32 UART TX → SDRTU UART1 RX (115200 baud, 8N1)
-- 部署: DTU Setup 上位机 → 脚本升级
-- =============================================================================

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          CONFIG                                             ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

local CFG = {
    device_name = "EXD001",

    -- UART (ESP32 连接参数)
    uart_id     = 3,
    uart_baud   = 115200,
    uart_bits   = 8,
    uart_parity = uart.None,   -- 常量: uart.None / uart.Even / uart.Odd
    uart_stop   = 1,
    uart_rwait  = 200,         -- 分帧超时 ms (200ms 内无新数据则触发 recv)

    -- MQTT Broker
    mqtt_host       = "test.developlink.cloud",
    mqtt_port       = 1883,
    mqtt_user       = "devlink",
    mqtt_pass       = "devlink",
    mqtt_keepalive  = 60000,   -- 毫秒
    mqtt_clean      = 1,
    mqtt_reconnect  = 3000,    -- 重连间隔 ms

    -- 心跳间隔 ms
    heartbeat_ms = 30000,
}

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          STATE                                              ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

local mq = nil
local mq_online = false
local msg_total = 0

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          TOPIC HELPERS                                      ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

local function T(suffix)
    return "drone/" .. CFG.device_name .. "/" .. suffix
end

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          UART EVENT HANDLER                                 ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

local function on_uart_recv(id, len)
    local raw = uart.read(id, len)
    if not raw or #raw == 0 then return end

    -- 按行分割 JSON (ESP32 每行一条 JSON)
    local start = 1
    while start <= #raw do
        local nl = raw:find("\n", start)
        if not nl then nl = raw:find("\r", start) end
        if not nl then
            -- 最后一行 (可能不完整, 留在下次 recv 处理或丢弃超长行)
            local line = raw:sub(start):match("^%s*(.-)%s*$")
            if #line > 0 then process_line(line) end
            break
        end
        local line = raw:sub(start, nl - 1):match("^%s*(.-)%s*$")
        if #line > 0 then process_line(line) end
        start = nl + 1
    end
end

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          JSON LINE PROCESSING                               ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

function process_line(line)
    -- 快速校验 JSON 合法性
    local ok, _ = pcall(json.decode, line)
    if not ok then return end

    msg_total = msg_total + 1

    if mq_online then
        -- 透传: 原始 JSON → cloud drone/{device}/raw
        -- 云端 mqtt_consumer._buffer_raw() 负责解析 + 距离计算 + 告警
        mq:pub(T("raw"), line)
    end
end

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          HEARTBEAT                                          ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

local function send_heartbeat()
    if not mq_online then return end
    local payload = json.encode({
        dev_id = CFG.device_name,
        count  = msg_total,
        sn     = misc.getSn(),
        csq    = misc.getCsq(),
        type   = "heartbeat",
    })
    mq:pub(T("heartbeat"), payload)
end

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          MQTT EVENT HANDLERS                                ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

local function on_mqtt_disconnect()
    mq_online = false
    log.warn("MQTT disconnected")
end

local function on_mqtt_recon(id, ok)
    if ok then
        mq_online = true
        log.info("MQTT connected, id=", id)

        -- 上线
        mq:pub(T("status"), "online")

        -- 订阅下行指令
        mq:sub("cmd/" .. CFG.device_name .. "/config", 1)
        mq:sub("cmd/broadcast", 1)
    else
        mq_online = false
        log.warn("MQTT reconnect failed, id=", id)
    end
end

local function on_mqtt_error(id)
    log.error("MQTT error, channel=", id)
end

local function on_mqtt_recv(id, topic_str, data)
    log.info("MQTT rx:", topic_str, data)
    -- TODO: 云端下行指令处理
end

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          INIT                                               ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

local function init_uart()
    log.info("UART init: uart", CFG.uart_id, CFG.uart_baud, "baud")

    uart.setup(
        CFG.uart_id,
        CFG.uart_baud,
        CFG.uart_bits,
        CFG.uart_parity,
        CFG.uart_stop,
        0,              -- rs485 流控 (0=无)
        CFG.uart_rwait  -- 分帧超时 ms
    )

    -- 事件驱动: 串口收到数据后触发 recv
    uart.on(CFG.uart_id, "recv", on_uart_recv)
end

local function init_mqtt()
    local client_id = CFG.device_name .. "-sdrtu"

    mq = mqtt.tcp(
        CFG.mqtt_host,
        CFG.mqtt_port,
        client_id,
        CFG.mqtt_user,
        CFG.mqtt_pass,
        CFG.mqtt_keepalive,
        CFG.mqtt_clean,
        CFG.mqtt_reconnect
    )

    -- 遗嘱 (LWT)
    mq:will(T("status"), 1, 1, "offline")

    -- 事件
    mq:on("disconnect", on_mqtt_disconnect)
    mq:on("recon",      on_mqtt_recon)
    mq:on("error",      on_mqtt_error)
    mq:on("recv",       on_mqtt_recv)

    log.info("MQTT connecting:", CFG.mqtt_host, CFG.mqtt_port, "as", client_id)
    mq:connect()
end

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          MAIN                                               ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

log.info("=== Drone RID Edge v3.0 ===", CFG.device_name, "sn=", misc.getSn())

init_uart()
init_mqtt()

-- 心跳定时器 (周期性)
sys.timerLoopStart(send_heartbeat, CFG.heartbeat_ms)

-- 串口透传协程 (保持 uart.on 事件循环活跃)
sys.taskInit(function()
    while true do
        -- uart.on 回调在事件循环中触发, 此协程仅保活
        sys.wait(60000)
    end
end)

-- 启动事件循环 (必须, 驱动 MQTT 回调 + 定时器 + UART 事件)
while true do
    sys.eventloop()
end
