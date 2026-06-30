-- ================================================================
--  无人机 RID 接收器 — SDRTU DTU (ZL400) Lua 脚本
--  从串口3接收 ESP32 RID JSON，解析后通过 4G 上报 Developlink
--  通道类型: developlink-iot-new (N_SEND_1)
--  上报格式: 扁平JSON (无params层) 适配属性上报
--  上传方式: 上位机 → 增量下载 → userapp.lua
--  版本: v4.0 — 新增电力走廊距离计算 + 三级告警
-- ================================================================

-- ================================================================
--  用户参数 (通过上位机 "用户参数" 配置)
-- ================================================================
local config = io.getDtu()

local delay       = config and config.up and config.up.delay
                  and tonumber(config.up.delay) or 30          -- 上报间隔(秒)
local device_id   = config and config.up and config.up.device_id
                  or misc.getImei()                           -- 设备标识
local test_mode   = config and config.up and config.up.test_mode
                  and tonumber(config.up.test_mode) or 1     -- 0=自动自检 1=强制自检
local cable_len   = config and config.up and config.up.cable_len
                  and tonumber(config.up.cable_len) or 0     -- 挂线长度(米)

-- ================================================================
--  电力走廊 — 电线杆配置 (从用户参数读取，最多8根)
--  格式: poleN = "lat,lon,alt,line_height"
-- ================================================================
local poles = {}
for i = 1, 8 do
    local key = "pole" .. i
    local val = config and config.up and config.up[key]
    if val and type(val) == "string" and #val > 0 then
        local parts = {}
        local idx = 1
        for part in string.gmatch(val, "([^,]+)") do
            parts[idx] = tonumber(part)
            idx = idx + 1
        end
        if idx >= 5 then
            poles[#poles + 1] = {
                lat = parts[1],
                lon = parts[2],
                alt = parts[3],
                line_h = parts[4],
            }
        end
    end
end

-- ================================================================
--  全局状态
-- ================================================================
local drone_cache     = {}        -- key=devId, value=最近一次RID数据
local drone_count_var = 0         -- 无人机数量
local total_packets   = 0         -- 累计接收包数(含心跳)
local serial_buf      = ""        -- 串口缓冲区
local last_reported   = {}        -- key=devId, value=上次上报时的 packet_count
local self_test_done  = false     -- 自检是否已完成
local has_drone_data  = false     -- 是否有非零RID数据可上报

-- ================================================================
--  地球半径 (米)
-- ================================================================
local EARTH_R = 6371000
local DEG2RAD = math.pi / 180

-- ================================================================
--  工具函数
-- ================================================================
local function trim(s)
    if not s then return "" end
    return s:match("^%s*(.-)%s*$") or s
end

-- ================================================================
--  地理计算函数
-- ================================================================

-- Haversine: 两点间水平距离(米)
local function haversine(lat1, lon1, lat2, lon2)
    local dlat = (lat2 - lat1) * DEG2RAD
    local dlon = (lon2 - lon1) * DEG2RAD
    local a = math.sin(dlat / 2) ^ 2
            + math.cos(lat1 * DEG2RAD) * math.cos(lat2 * DEG2RAD)
            * math.sin(dlon / 2) ^ 2
    return EARTH_R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
end

-- Web Mercator 投影 (近似，用于垂线计算)
-- 返回 x,y (米)
local function mercator(lat, lon)
    local x = lon * DEG2RAD * EARTH_R
    local y = math.log(math.tan(math.pi / 4 + lat * DEG2RAD / 2)) * EARTH_R
    return x, y
end

-- 计算点到线段的最短3D距离
-- 线段: (pole_a, pole_b)，无人机: (lat, lon, alt)
-- 返回: (最短3D距离, 水平距离, 垂直距离)
local function distance_to_segment(pole_a, pole_b, lat, lon, alt)
    -- 导线两端海拔高度
    local line_alt_a = pole_a.alt + pole_a.line_h
    local line_alt_b = pole_b.alt + pole_b.line_h

    -- Web Mercator 投影
    local x1, y1 = mercator(pole_a.lat, pole_a.lon)
    local x2, y2 = mercator(pole_b.lat, pole_b.lon)
    local xp, yp = mercator(lat, lon)

    local dx = x2 - x1
    local dy = y2 - y1
    local seg_len_sq = dx * dx + dy * dy

    local t, close_x, close_y
    if seg_len_sq < 1e-10 then
        t = 0
        close_x, close_y = x1, y1
    else
        t = (xp - x1) * dx + (yp - y1) * dy
        t = t / seg_len_sq
        t = math.max(0, math.min(1, t))
        close_x = x1 + t * dx
        close_y = y1 + t * dy
    end

    -- 水平距离
    local h_dist = math.sqrt((close_x - xp) ^ 2 + (close_y - yp) ^ 2)

    -- 导线在t点的海拔高度（线性插值 + 弧垂修正约3%）
    local line_alt_t = line_alt_a + t * (line_alt_b - line_alt_a)
    local seg_len = math.sqrt(seg_len_sq)
    if seg_len > 10 then
        line_alt_t = line_alt_t - seg_len * 0.03 * math.sin(t * math.pi)
    end

    -- 垂直距离
    local v_dist = alt - line_alt_t

    -- 3D距离
    local d3 = math.sqrt(h_dist * h_dist + v_dist * v_dist)

    return d3, h_dist, v_dist, t
end

-- 计算无人机到整条电力走廊的最短距离
-- 返回: (最短3D距离, 水平距离, 垂直距离, 最近线段索引)
-- 如果无电线杆配置，返回 nil
local function calc_corridor_distance(lat, lon, alt)
    if #poles < 2 then
        return nil
    end

    local best_d3 = 1e9
    local best_hd = 0
    local best_vd = 0
    local best_idx = 0

    for i = 1, #poles - 1 do
        local d3, hd, vd = distance_to_segment(poles[i], poles[i + 1], lat, lon, alt)
        if d3 < best_d3 then
            best_d3 = d3
            best_hd = hd
            best_vd = vd
            best_idx = i
        end
    end

    return best_d3, best_hd, best_vd, best_idx
end

-- 计算告警级别
-- 0=安全 1=提醒(<200m) 2=警告(<100m) 3=危险(<50m)
local function calc_alert_level(dist)
    if dist < 50 then
        return 3
    elseif dist < 100 then
        return 2
    elseif dist < 200 then
        return 1
    else
        return 0
    end
end

-- ================================================================
--  RID JSON 解析 (匹配 ESP32 输出格式)
-- ================================================================
local function rid_parse(line)
    local ok, data = pcall(json.decode, line)
    if not ok or type(data) ~= "table" then
        return nil
    end

    local result = {
        devId = data.devId or "unknown",
        osid = "",
        count = data.count or 0,
        lat = 0, lon = 0,
        alt_baro = 0, alt_geo = -1000, height = 0,
        op_lat = 0, op_lon = 0, op_alt = -1000,
        heading = 0, speed = 0, uprate = 0, rssi = 0,
        status = 0, ua_type = 0, fre = 0, ua_time = 0,
        has_data = false,
    }

    local inner = data.data
    if type(inner) == "table" then
        result.has_data = true
        result.osid    = inner.osid    or result.osid
        result.lat     = inner.Lat     or result.lat
        result.lon     = inner.Lon     or result.lon
        result.alt_baro= inner.AltBaro or result.alt_baro
        result.alt_geo = inner.AltGeo  or result.alt_geo
        result.height  = inner.Height  or result.height
        result.op_lat  = inner.Op_Lat  or result.op_lat
        result.op_lon  = inner.Op_Lon  or result.op_lon
        result.op_alt  = inner.Op_Alt  or result.op_alt
        result.heading = inner.Heading or result.heading
        result.speed   = inner.Speed   or result.speed
        result.uprate  = inner.Uprate  or result.uprate
        result.rssi    = inner.RSSI    or result.rssi
        result.status  = inner.Status  or result.status
        result.ua_type = inner.UAType  or result.ua_type
        result.fre     = inner.Fre     or result.fre
        result.ua_time = inner.UATime  or result.ua_time
    end

    return result
end

-- ================================================================
--  取最佳位置 (lat/lon优先，fallback到op_lat/op_lon)
-- ================================================================
local function best_location(parsed)
    local lat = parsed.lat
    local lon = parsed.lon
    local alt = parsed.alt_baro

    if lat == 0 and lon == 0 then
        lat = parsed.op_lat
        lon = parsed.op_lon
        alt = parsed.op_alt ~= -1000 and parsed.op_alt or alt
    end

    return lat, lon, alt
end

-- ================================================================
--  串口3数据回调 (花括号匹配 + 缓冲区拼接)
-- ================================================================
local function serialRidHook(data)
    if not data or #data == 0 then return end

    serial_buf = serial_buf .. data
    log.info("RID_RAW-serial", data)

    -- 缓冲区防溢出
    if #serial_buf > 4096 and not string.find(serial_buf, "{") then
        serial_buf = ""
    end

    while true do
        local brace_start = string.find(serial_buf, "{")
        if not brace_start then
            if #serial_buf > 4096 then serial_buf = "" end
            break
        end

        local depth = 0
        local json_end = nil
        for i = brace_start, #serial_buf do
            local c = string.byte(serial_buf, i)
            if c == 0x7B then
                depth = depth + 1
            elseif c == 0x7D and depth > 0 then
                depth = depth - 1
                if depth == 0 then
                    json_end = i
                    break
                end
            end
        end

        if not json_end then
            if #serial_buf > 4096 then serial_buf = "" end
            break
        end

        local line = string.sub(serial_buf, brace_start, json_end)
        serial_buf = string.sub(serial_buf, json_end + 1)

        local parsed = rid_parse(line)
        if not parsed then
            log.info("RID_PARSE_FAIL", string.sub(line, 1, 80))
            goto continue
        end

        total_packets = total_packets + 1
        log.info("RID_RAW", line)

        local key = parsed.devId
        if not key or key == "" or key == "unknown" then
            goto continue
        end

        -- 跳过无data字段的心跳包
        if not parsed.has_data then
            goto continue
        end

        -- 计算电力走廊距离
        local loc_lat, loc_lon, loc_alt = best_location(parsed)
        local dist = nil
        local alert_lv = 0
        if loc_lat ~= 0 or loc_lon ~= 0 then
            local d3 = calc_corridor_distance(loc_lat, loc_lon, loc_alt)
            if d3 then
                -- 减去挂线长度
                dist = d3 - cable_len
                if dist < 0 then dist = 0 end
                alert_lv = calc_alert_level(dist)
            end
        end

        -- 存入缓存
        local prev = drone_cache[key]
        if not prev then
            drone_count_var = drone_count_var + 1
        end
        drone_cache[key] = {
            osid = parsed.osid or "",
            devId = parsed.devId,
            lat = parsed.lat,
            lon = parsed.lon,
            alt_baro = parsed.alt_baro,
            alt_geo = parsed.alt_geo,
            height = parsed.height,
            op_lat = parsed.op_lat,
            op_lon = parsed.op_lon,
            op_alt = parsed.op_alt,
            heading = parsed.heading,
            speed = parsed.speed,
            uprate = parsed.uprate,
            rssi = parsed.rssi,
            status = parsed.status,
            ua_type = parsed.ua_type,
            fre = parsed.fre,
            ua_time = parsed.ua_time,
            last_seen = os.time(),
            packet_count = (prev and prev.packet_count or 0) + 1,
            -- 距离告警字段
            dist_to_line = dist,
            cable_len = cable_len,
            alert_level = alert_lv,
        }

        -- 标记有新数据待上报
        has_drone_data = true

        -- 每5包打印一条摘要
        if total_packets % 5 == 1 then
            local loc = ""
            if loc_lat ~= 0 then
                loc = string.format(" (%.5f,%.5f alt=%dm)", loc_lat, loc_lon, loc_alt)
            end
            local dist_str = ""
            if dist then
                dist_str = string.format(" dist=%.1fm alert=%d", dist, alert_lv)
            end
            log.info("RID", string.format("[%d] %s rssi=%d%s%s",
                total_packets, parsed.devId, parsed.rssi or 0, loc, dist_str))
        end

        ::continue::
    end
end

-- ================================================================
--  上报函数 — developlink-iot-new 格式
--  JSON 字段 key 必须与 TSL 物模型属性 key 一一匹配
-- ================================================================
local function doReport(report)
    local payload = {
        device_id    = device_id,
        drone_count  = report.drone_count or 0,
        total_packets= report.total_packets or 0,
        uptime       = math.floor(rtos.tick() / 1000),
        osid         = report.osid or "",
        devId        = report.devId or "",
        lat          = report.lat or 0,
        lon          = report.lon or 0,
        alt_baro     = report.alt_baro or 0,
        alt_geo      = report.alt_geo or -1000,
        op_lat       = report.op_lat or 0,
        op_lon       = report.op_lon or 0,
        op_alt       = report.op_alt or -1000,
        heading      = report.heading or 0,
        speed        = report.speed or 0,
        rssi         = report.rssi or 0,
        status       = report.status or 0,
        packet_count = report.packet_count or 0,
        -- 电力走廊距离告警字段 (TSL v3新增)
        dist_to_line = report.dist_to_line or -1,
        cable_len    = report.cable_len or 0,
        alert_level  = report.alert_level or 0,
    }
    sys.publish("N_SEND_1", { b = payload, from = "RID_REPORT" })
    log.info("RID", string.format("上报 %s cache=%d 包%d dist=%.1f alert=%d",
        report.devId or "", report.drone_count or 0,
        report.total_packets or 0, report.dist_to_line or -1, report.alert_level or 0))
end

-- ================================================================
--  定时上报 (每 delay 秒)
-- ================================================================
local heartbeat_count = 0

local function reportDrones()
    if has_drone_data then
        local latest = nil
        local latest_time = 0
        for key, drone in pairs(drone_cache) do
            if drone.last_seen and drone.last_seen > latest_time then
                latest_time = drone.last_seen
                latest = drone
            end
        end

        if latest then
            local last_pc = last_reported[latest.devId] or 0
            if latest.packet_count > last_pc then
                last_reported[latest.devId] = latest.packet_count
                doReport({
                    drone_count   = drone_count_var,
                    total_packets = total_packets,
                    osid          = latest.osid or "",
                    devId         = latest.devId or "",
                    lat           = latest.lat or 0,
                    lon           = latest.lon or 0,
                    alt_baro      = latest.alt_baro or 0,
                    alt_geo       = latest.alt_geo or -1000,
                    op_lat        = latest.op_lat or 0,
                    op_lon        = latest.op_lon or 0,
                    op_alt        = latest.op_alt or -1000,
                    heading       = latest.heading or 0,
                    speed         = latest.speed or 0,
                    rssi          = latest.rssi or 0,
                    status        = latest.status or 0,
                    packet_count  = latest.packet_count or 0,
                    dist_to_line  = latest.dist_to_line,
                    cable_len     = latest.cable_len,
                    alert_level   = latest.alert_level,
                })
            end
        end
    end

    -- 心跳
    heartbeat_count = heartbeat_count + 1
    if not has_drone_data and heartbeat_count >= 6 then
        heartbeat_count = 0
        doReport({
            drone_count   = 0,
            total_packets = total_packets,
            osid = "", devId = "",
            lat = 0, lon = 0, alt_baro = 0, alt_geo = -1000,
            op_lat = 0, op_lon = 0, op_alt = -1000,
            heading = 0, speed = 0, rssi = 0, status = 0,
            packet_count = 0,
            dist_to_line = -1, cable_len = cable_len, alert_level = 0,
        })
        log.info("RID", string.format("心跳 poles=%d cable=%.1fm 缓冲%d",
            #poles, cable_len, #serial_buf))
    elseif heartbeat_count >= 360 then
        heartbeat_count = 0
    end
end

-- ================================================================
--  自检：注入模拟数据，验证全链路
-- ================================================================
local function injectTestData()
    if self_test_done then return end
    self_test_done = true
    log.info("SELFTEST", "=== 开始自检 ===")

    -- Step A: 直接上报验证数据
    local verify = {
        drone_count   = 99,
        total_packets = 0,
        uptime        = math.floor(rtos.tick() / 1000),
        osid = "SELFTEST", devId = "SELFTEST",
        lat = 30.53, lon = 104.08,
        alt_baro = 500, alt_geo = 510,
        op_lat = 30.52, op_lon = 104.07, op_alt = 480,
        heading = 0, speed = 0, rssi = -50, status = 0,
        packet_count = 0,
        dist_to_line = 150, cable_len = cable_len, alert_level = 1,
    }
    log.info("SELFTEST", "Step A: 发送 developlink 验证数据")
    doReport(verify)

    -- Step B: 注入模拟 RID 数据
    log.info("SELFTEST", "Step B: 注入模拟 RID JSON...")
    serialRidHook('{"devId":"TEST001","count":1}')
    serialRidHook('{"devId":"EXD001","count":100,"data":{' ..
        '"osid":"TEST-OSID-001","Lat":30.5298,"Lon":104.0847,' ..
        '"AltBaro":500,"AltGeo":510,"Height":100,' ..
        '"Op_Lat":30.53,"Op_Lon":104.08,"Heading":180,' ..
        '"Speed":5.2,"RSSI":-65,"Status":2,"UAType":0,' ..
        '"Fre":2437,"UATime":1000}}')

    -- Step C: 逐字节注入
    local json3 = '{"devId":"TEST002","count":2}'
    for i = 1, #json3 do
        serialRidHook(string.sub(json3, i, i))
    end

    log.info("SELFTEST", string.format("自检完成 cache=%d 包=%d poles=%d cable=%.1f",
        drone_count_var, total_packets, #poles, cable_len))
end

-- ================================================================
--  初始化
-- ================================================================
log.info("RID", "=== 无人机 RID 接收器 v4.0 (电力走廊告警) 启动 ===")
log.info("RID", "设备ID:", device_id)
log.info("RID", "上报间隔:", delay, "秒")
log.info("RID", "自检模式:", (test_mode == 1) and "强制" or "自动(30秒无数据)")
log.info("RID", "电线杆:", #poles, "根")
log.info("RID", "挂线长度:", cable_len, "米")

-- 打印电线杆信息
for i, p in ipairs(poles) do
    log.info("RID", string.format("  pole%d: (%.5f,%.5f) alt=%dm line_h=%dm",
        i, p.lat, p.lon, p.alt, p.line_h))
end

if #poles < 2 then
    log.info("RID", "⚠️ 电线杆不足2根，电力走廊距离计算禁用")
end

-- 配置串口3: 115200,8N1, RS485(GPIO19), rwait=500ms, buf=40960
uart.setup(3, 115200, 8, 0, 1, 19, 500, 40960)
sys.subscribe("D_RECV_3", serialRidHook)

-- 定时上报
sys.timerLoopStart(reportDrones, delay * 1000)

-- 自检调度
if test_mode == 1 then
    sys.timerStart(function()
        log.info("SELFTEST", "1秒后发送验证数据")
        injectTestData()
    end, 1000)
else
    sys.timerStart(function()
        if total_packets == 0 then
            log.info("SELFTEST", "30秒无数据，自动进入自检")
            injectTestData()
        end
    end, 30000)
end

log.info("RID", "IMEI:", misc.getImei())
log.info("RID", "就绪，等待 RID 数据...")
