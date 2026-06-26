-- =============================================================================
-- Drone RID Edge Receiver — SDRTU (DevelopLink ZL4xx/ZL5xx)
-- ESP32 BLE 二进制 → hex 编码 → MQTT → 云端解析 ASTM F3411
-- =============================================================================
-- 接线: ESP32 UART TX → SDRTU UART3 RX (115200 baud, 8N1)
-- =============================================================================

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          CONFIG                                             ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

local CFG = {
    device_name = "EXD001",

    -- UART (ESP32 BLE 数据流)
    uart_id     = 3,
    uart_baud   = 115200,
    uart_bits   = 8,
    uart_parity = uart.None,
    uart_stop   = 1,
    uart_rwait  = 200,         -- 分帧超时 ms

    -- MQTT Broker
    mqtt_host       = "test.developlink.cloud",
    mqtt_port       = 1883,
    mqtt_user       = "devlink",
    mqtt_pass       = "devlink",
    mqtt_keepalive  = 60000,
    mqtt_clean      = 1,
    mqtt_reconnect  = 3000,

    -- 心跳间隔 ms
    heartbeat_ms = 30000,
}

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          STATE                                              ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

local mq = nil
local mq_online = false
local msg_total = 0

-- 16进制查找表 (性能优化)
local HEX = "0123456789abcdef"

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          TOPIC                                              ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

local function T(suffix)
    return "drone/" .. CFG.device_name .. "/" .. suffix
end

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          BINARY → HEX                                       ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

local function to_hex(data)
    local n = #data
    if n > 4096 then
        local parts = {}
        local offset = 1
        while offset <= n do
            local chunk = data:sub(offset, offset + 4095)
            local hex_chunk = {}
            for i = 1, #chunk do
                local b = chunk:byte(i)
                hex_chunk[i] = HEX:sub(math.floor(b / 16) + 1, math.floor(b / 16) + 1)
                            .. HEX:sub(b % 16 + 1, b % 16 + 1)
            end
            parts[#parts + 1] = table.concat(hex_chunk)
            offset = offset + 4096
        end
        return table.concat(parts)
    end

    local hex_chars = {}
    for i = 1, n do
        local b = data:byte(i)
        hex_chars[i] = HEX:sub(math.floor(b / 16) + 1, math.floor(b / 16) + 1)
                    .. HEX:sub(b % 16 + 1, b % 16 + 1)
    end
    return table.concat(hex_chars)
end

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          UART → MQTT                                        ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

local function on_uart_recv(id, len)
    local raw = uart.read(id, len)
    if not raw or #raw == 0 then return end

    msg_total = msg_total + 1

    if not mq_online then
        log.warn("UART", "data dropped, MQTT offline, len=", #raw)
        return
    end

    -- 二进制 → hex → JSON → MQTT
    -- 云端 consumer 负责 ASTM F3411 协议解析 + 距离计算 + 告警
    local hex_str = to_hex(raw)

    local payload = json.encode({
        dev_id  = CFG.device_name,
        raw_hex = hex_str,
        len     = #raw,
        count   = msg_total,
        type    = "ble_raw",
    })

    mq:pub(T("raw"), payload)
    log.info("UART", "pub raw len=", #raw, "hex=", #hex_str)
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
        mq:pub(T("status"), "online")
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
end

-- ╔═════════════════════════════════════════════════════════════════════════════╗
-- ║                          INIT                                               ║
-- ╚═════════════════════════════════════════════════════════════════════════════╝

local function init_uart()
    log.info("UART init: uart", CFG.uart_id, CFG.uart_baud, "baud, raw BLE mode")
    uart.setup(CFG.uart_id, CFG.uart_baud, CFG.uart_bits,
               CFG.uart_parity, CFG.uart_stop, 0, CFG.uart_rwait)
    uart.on(CFG.uart_id, "recv", on_uart_recv)
end

local function init_mqtt()
    local client_id = CFG.device_name .. "-sdrtu"
    mq = mqtt.tcp(CFG.mqtt_host, CFG.mqtt_port, client_id,
                  CFG.mqtt_user, CFG.mqtt_pass,
                  CFG.mqtt_keepalive, CFG.mqtt_clean, CFG.mqtt_reconnect)
    mq:will(T("status"), 1, 1, "offline")
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

log.info("=== Drone RID Edge v4.0 ===", CFG.device_name, "sn=", misc.getSn())
init_uart()
init_mqtt()
sys.timerLoopStart(send_heartbeat, CFG.heartbeat_ms)
sys.taskInit(function()
    while true do sys.wait(60000) end
end)
while true do
    sys.eventloop()
end
