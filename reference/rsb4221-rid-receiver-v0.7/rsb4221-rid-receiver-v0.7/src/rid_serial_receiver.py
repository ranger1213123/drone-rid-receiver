#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
RSB-4221 串口 RID 接收器
从 /dev/ttyUSB0 读取 JSON 格式的 RID 数据
支持两种格式:
  Format 1 (heartbeat): {"devId":"EXD001","count":N}
  Format 2 (with data): {"devId":"EXD001","data":{"osid":"...","Op_Lat":...,"Op_Lon":...,...}}
"""
import os
import sys
import json
import time
import logging
import threading

logger = logging.getLogger("rid_serial")

SERIAL_DEVICE = "/dev/ttyUSB0"
BAUD_RATE = 115200


class SerialRIDReceiver(object):
    """串口 RID 接收器"""

    def __init__(self, device=SERIAL_DEVICE, baud=BAUD_RATE, callback=None):
        self.device = device
        self.baud = baud
        self.callback = callback
        self._running = False
        self._thread = None
        self._dev = None

    def _configure_serial(self):
        """配置串口波特率"""
        import subprocess
        err = open(os.devnull, "w")
        subprocess.call(["stty", "-F", self.device, str(self.baud),
                        "cs8", "-cstopb", "-parenb", "raw", "-echo",
                        "icrnl"], stdout=err, stderr=err)
        err.close()

    def _read_loop(self):
        """串口读取线程"""
        logger.info("串口接收线程启动: %s @ %d baud" % (self.device, self.baud))
        while self._running:
            try:
                self._dev = open(self.device, "rb", 0)
                self._configure_serial()
                buf = b""
                while self._running:
                    c = self._dev.read(1)
                    if not c:
                        break
                    if c == b"\n" or c == b"\r":
                        if buf.strip():
                            line = buf.strip()
                            buf = b""
                            try:
                                data = json.loads(line)
                                if self.callback:
                                    self.callback(data)
                            except ValueError:
                                logger.debug("非JSON行: %s" % repr(line))
                    else:
                        buf += c
                        if len(buf) > 1024:
                            buf = b""
            except Exception as e:
                if self._running:
                    logger.error("串口异常: %s" % str(e))
                    time.sleep(2)
            finally:
                try:
                    if self._dev:
                        self._dev.close()
                except:
                    pass
                self._dev = None
        logger.info("串口接收线程结束")

    def start(self):
        """启动接收"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, )
        self._thread.start()

    def stop(self):
        """停止接收"""
        self._running = False
        if self._dev:
            try:
                self._dev.close()
            except:
                pass
        logger.info("串口接收已停止")


def extract_location(data):
    """
    Parse ESP32 JSON and extract location + drone info.
    
    Format 1 (heartbeat): {"devId":"EXD001","count":N}
      -> returns {"drone_id": devId, "count": count, "has_location": False}
    
    Format 2 (with data): {"devId":"EXD001","data":{"osid":"...","Op_Lat":...,"Op_Lon":...,...}}
      -> returns {"drone_id": osid, "count": count, "has_location": True,
                  "location": {"lat": Op_Lat, "lon": Op_Lon, "alt": Op_Alt,
                               "heading": Heading, "speed": Speed},
                  "raw": {...}}
    """
    result = {
        "drone_id": data.get("devId", "unknown"),
        "count": data.get("count", 0),
        "has_location": False,
        "location": None,
    }
    
    # Check for nested data object (Format 2)
    inner = data.get("data")
    if inner and isinstance(inner, dict):
        # Use osid as the real drone_id if available
        osid = inner.get("osid", "")
        if osid:
            result["drone_id"] = osid
        
        # Check if we have valid operator location
        op_lat = inner.get("Op_Lat")
        op_lon = inner.get("Op_Lon")
        
        if op_lat is not None and op_lon is not None and op_lat != 0.0 and op_lon != 0.0:
            result["has_location"] = True
            result["location"] = {
                "lat": float(op_lat),
                "lon": float(op_lon),
                "alt": inner.get("Op_Alt", 0),
                "heading": inner.get("Heading", 0),
                "speed": inner.get("Speed", 0),
                "alt_baro": inner.get("AltBaro", 0),
                "alt_geo": inner.get("AltGeo", -1000),
                "height": inner.get("Height", 0),
                "rssi": inner.get("RSSI", 0),
                "uatype": inner.get("UAType", 0),
                "status": inner.get("Status", 0),
                "frequency": inner.get("Fre", 0),
                "ua_time": inner.get("UATime", 0),
            }
            result["rssi"] = inner.get("RSSI", result.get("rssi", 0))
    
    return result


def default_callback(data):
    """默认回调 - 打印解析后的 JSON 数据"""
    parsed = extract_location(data)
    dev_id = parsed["drone_id"]
    count = parsed["count"]
    if parsed["has_location"]:
        loc = parsed["location"]
        logger.info("[SERIAL] drone=%s count=%s loc=(%.5f, %.5f) hdg=%s spd=%s" %
                    (dev_id, count, loc["lat"], loc["lon"], loc["heading"], loc["speed"]))
    else:
        logger.info("[SERIAL] devId=%s count=%s" % (data.get("devId", dev_id), count))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print("串口RID接收器启动...")
    receiver = SerialRIDReceiver(callback=default_callback)
    receiver.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    receiver.stop()
