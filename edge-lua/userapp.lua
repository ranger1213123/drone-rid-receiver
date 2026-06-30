-- =============================================================================
-- Drone RID Forwarder v3.2 — SDRTU (ZL4xx/ZL5xx)
-- 使用 SDRTU 框架网络连接2 (N_SEND_2) 走 UDP 链路, 不自建 MQTT
-- 部署: DTU Setup 上位机 → 增量下载 → 重启
-- =============================================================================

-- ═══════════════════════════════════════════════════════════
-- CONFIG
-- ═══════════════════════════════════════════════════════════

local CFG = {
    device_name = "EXD001",

    -- 数据走框架网络连接2
    net_channel  = 2,

    -- UART (串口3)
    uart_id     = 3,
    uart_baud   = 115200,
    uart_bits   = 8,
    uart_parity = uart.None,
    uart_stop   = 1,
    uart_rs485  = 0,
    uart_rwait  = 200,

    -- 心跳间隔 ms
    heartbeat_ms = 30000,
}

-- ═══════════════════════════════════════════════════════════
-- TOPIC HELPERS
-- ═══════════════════════════════════════════════════════════

local function T(suffix) return "drone/" .. CFG.device_name .. "/" .. suffix end

-- ═══════════════════════════════════════════════════════════
-- STATE
-- ═══════════════════════════════════════════════════════════

local msg_total = 0
local gps = { lat = 0.0, lon = 0.0, alt = 0.0, valid = false }

-- ═══════════════════════════════════════════════════════════
-- GPS BUS SNIFFER
-- ═══════════════════════════════════════════════════════════

local gps_topics = {
    "GPS", "GPS_DATA", "GPS_LOCATION",
    "LOCATION", "LOC", "POS",
    "LBS", "LBS_DATA", "LBS_LOCATION",
    "CELL", "CELL_INFO", "CELL_LOCATION",
    "NET_INFO", "NET_LOCATION", "NET_STATUS",
    "MODEM_STATUS", "MODEM_INFO",
}

local function on_gps_bus(...)
    local args = {...}
    if #args < 2 then return end
    local lat = tonumber(args[1])
    local lon = tonumber(args[2])
    if lat and lon and lat ~= 0 and lon ~= 0 then
        gps.lat = lat
        gps.lon = lon
        gps.alt = tonumber(args[3]) or 0.0
        gps.valid = true
        log.info("GPS_BUS", "FIX", string.format("%.6f,%.6f", lat, lon))
    end
end

local function init_bus_sniffer()
    for _, topic in ipairs(gps_topics) do
        sys.subscribe(topic, on_gps_bus)
    end
    log.info("BUS", "sniffer subscribed to", #gps_topics, "topics")
end

-- ═══════════════════════════════════════════════════════════
-- 发送数据到指定 MQTT topic (通过框架网络连接2)
-- 格式参考官方 demo: sys.publish("N_SEND_1", {b=data, t=topic})
-- ═══════════════════════════════════════════════════════════

local function net_send(topic, data)
    sys.publish("N_SEND_" .. CFG.net_channel, { b = data, t = topic })
end

-- ═══════════════════════════════════════════════════════════
-- UART → 框架网络通道 (RID 透传)
-- ═══════════════════════════════════════════════════════════

local function process_rid_line(line)
    if #line == 0 then return end
    local ok = pcall(json.decode, line)
    if not ok then return end

    msg_total = msg_total + 1
    net_send(T("raw"), line)
    log.info("RID", "tx #", msg_total, "len=", #line)
end

local function on_uart_recv(id, len)
    local raw = uart.read(id, len)
    if not raw or #raw == 0 then return end

    local start = 1
    while start <= #raw do
        local nl = raw:find("\n", start)
        if not nl then nl = raw:find("\r", start) end
        if not nl then
            local line = raw:sub(start):match("^%s*(.-)%s*$")
            if #line > 0 then process_rid_line(line) end
            break
        end
        local line = raw:sub(start, nl - 1):match("^%s*(.-)%s*$")
        if #line > 0 then process_rid_line(line) end
        start = nl + 1
    end
end

local function init_uart()
    log.info("UART", "init uart", CFG.uart_id, CFG.uart_baud, "baud")
    uart.setup(
        CFG.uart_id, CFG.uart_baud, CFG.uart_bits,
        CFG.uart_parity, CFG.uart_stop, CFG.uart_rs485, CFG.uart_rwait
    )
    uart.on(CFG.uart_id, "recv", on_uart_recv)
end

-- ═══════════════════════════════════════════════════════════
-- HEARTBEAT (也通过框架网络通道)
-- ═══════════════════════════════════════════════════════════

local function send_heartbeat()
    local payload = json.encode({
        dev_id    = CFG.device_name,
        lat       = gps.lat,
        lon       = gps.lon,
        alt       = gps.alt,
        gps_valid = gps.valid,
        count     = msg_total,
        sn        = misc.getSn(),
        csq       = misc.getCsq(),
        type      = "heartbeat",
    })

    net_send(T("heartbeat"), payload)
    log.info("HB", "sent", "gps=" .. (gps.valid and "OK" or "NONE"),
             "count=" .. msg_total, "csq=" .. misc.getCsq())
end

-- ═══════════════════════════════════════════════════════════
-- MAIN
-- ═══════════════════════════════════════════════════════════

log.info("=============================================")
log.info("Drone RID Forwarder v3.2")
log.info("device:", CFG.device_name, "sn:", misc.getSn())
log.info("channel: N_SEND_" .. CFG.net_channel)
log.info("UART:", CFG.uart_id, CFG.uart_baud)
log.info("=============================================")

init_bus_sniffer()
init_uart()

-- 心跳定时器 (30s)
sys.timerLoopStart(send_heartbeat, CFG.heartbeat_ms)

-- userapp.lua 作为 SDRTU 框架补丁运行，框架本身已有事件循环，无需额外 eventloop
