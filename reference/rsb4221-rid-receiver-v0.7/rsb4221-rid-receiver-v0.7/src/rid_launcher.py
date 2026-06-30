# -*- coding: utf-8 -*-
import os, sys, time, logging, threading, subprocess, argparse

BASE_DIR = "/opt/rid-receiver"
sys.path.insert(0, BASE_DIR)

from server_web import db, alert_sys, process_drone_data, logger
from rid_wifi_scanner import start_scan, stop_scan, is_scanning, get_latest_drones

WIFI_SCANNER_PATH = "/opt/rid-receiver/rid_wifi_scanner.py"
DRIVER_PATH = "/home/root/8812au_drv/8812au_mod.ko"
FLOAD_PATH = "/tmp/fload"

def setup_wifi(iface, channel):
    import subprocess
    logger.info("WiFi setup %s CH%d" % (iface, channel))
    def run(cmd, timeout=5):
        try:
            p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = p.communicate()
            return p.returncode, out, err
        except:
            return -1, "", str(sys.exc_info()[1])
    rc, out, _ = run("lsmod | grep 8812au")
    if rc != 0 or "8812au" not in out:
        logger.info("loading driver...")
        run("/sbin/modprobe cfg80211")
        rc2, _, err2 = run("%s %s" % (FLOAD_PATH, DRIVER_PATH))
        if rc2 != 0:
            logger.error("driver load failed: %s" % err2)
            return False
        time.sleep(1)
        rc3, _, err3 = run('echo "0bda 8812" > /sys/bus/usb/drivers/rtl8812au/new_id')
        if rc3 != 0:
            logger.error("USB bind failed: %s" % err3)
            return False
        time.sleep(2)
    run("/sbin/ip link set %s down" % iface)
    run("/usr/sbin/iw dev %s set type monitor" % iface)
    run("/sbin/ip link set %s up" % iface)
    run("/usr/sbin/iw dev %s set channel %d" % (iface, channel))
    rc, out, _ = run("/usr/sbin/iw dev %s info" % iface)
    if "type monitor" in out:
        logger.info("monitor mode ok")
        return True
    logger.error("monitor mode failed")
    return False

def drone_callback(drone_data):
    try:
        drone_id = drone_data.get("drone_id", "")
        if not drone_id:
            return
        data_for_server = {
            "drone_id": drone_id,
            "mac": drone_data.get("mac", ""),
            "rssi": drone_data.get("rssi", 0),
            "source": "wifi_rid",
            "location": None,
        }
        process_drone_data(data_for_server)
    except Exception as e:
        logger.error("callback error: %s" % str(e))

def start_web_server(port):
    from server_web import RIDHandler
    from BaseHTTPServer import HTTPServer
    import SocketServer
    class FastHTTPServer(HTTPServer):
        def server_bind(self):
            SocketServer.TCPServer.server_bind(self)
            host, port = self.socket.getsockname()[:2]
            self.server_name = host
    server = FastHTTPServer(("0.0.0.0", port), RIDHandler)
    logger.info("Web server: http://0.0.0.0:%d" % port)
    logger.info("power lines: %d" % len(db.get_power_lines()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

def setup_serial_receiver(serial_device="/dev/ttyUSB0", baud=115200):
    try:
        import rid_serial_receiver as rsr
        def serial_callback(data):
            try:
                parsed = rsr.extract_location(data)
                drone_id = parsed["drone_id"]
                if not drone_id:
                    return
                drone_data = {
                    "drone_id": drone_id,
                    "mac": "",
                    "rssi": parsed.get("rssi", 0),
                    "source": "serial_rid",
                    "location": parsed.get("location"),
                }
                process_drone_data(drone_data)
                count = parsed["count"]
                if parsed["has_location"]:
                    loc = parsed["location"]
                    logger.info("[SERIAL] drone=%s count=%s loc=(%.5f, %.5f) ok" %
                                (drone_id, count, loc["lat"], loc["lon"]))
                else:
                    logger.info("[SERIAL] devId=%s count=%s ok" % (data.get("devId", drone_id), count))
            except Exception as e:
                logger.error("serial cb error: %s" % str(e))
        receiver = rsr.SerialRIDReceiver(device=serial_device, baud=baud, callback=serial_callback)
        receiver.start()
        logger.info("serial RID started: %s @ %d baud" % (serial_device, baud))
        return receiver
    except Exception as e:
        logger.error("serial startup failed: %s" % str(e))
        return None

def main():
    parser = argparse.ArgumentParser(description="RSB-4221 RID launcher")
    parser.add_argument("--port", type=int, default=5000, help="web port (5000)")
    parser.add_argument("--iface", default="wlan0", help="wifi interface (wlan0)")
    parser.add_argument("--channel", type=int, default=6, help="scan channel (6)")
    parser.add_argument("--no-driver", action="store_true", help="skip driver load")
    parser.add_argument("--serial", default="/dev/ttyUSB0", help="serial device")
    parser.add_argument("--serial-baud", type=int, default=115200, help="serial baud (115200)")
    parser.add_argument("--no-serial", action="store_true", help="disable serial")
    args = parser.parse_args()

    import shutil
    src = os.path.join(BASE_DIR, "rid_wifi_scanner.py")
    if not os.path.exists(src):
        tmp_src = "/tmp/rid_wifi_scanner.py"
        if os.path.exists(tmp_src):
            shutil.copy(tmp_src, src)
        else:
            logger.error("rid_wifi_scanner.py not found!")
            sys.exit(1)

    logger.info("=" * 60)
    logger.info("  RSB-4221 RID Receiver v0.7")
    logger.info("  iface: %s  CH%d  serial: %s" % (args.iface, args.channel, args.serial if not args.no_serial else "OFF"))
    logger.info("=" * 60)

    # 1. WiFi setup
    if not args.no_driver:
        if not setup_wifi(args.iface, args.channel):
            logger.error("WiFi setup failed")
            sys.exit(1)

    # 2. Serial RID receiver
    if not args.no_serial:
        logger.info("starting serial RID: %s @ %d baud" % (args.serial, args.serial_baud))
        serial_receiver = setup_serial_receiver(args.serial, args.serial_baud)
        if not serial_receiver:
            logger.warning("serial receiver failed, continuing")
    else:
        logger.info("serial disabled (--no-serial)")

    # 3. WiFi RID scan (skip if interface is AP mode)
    import subprocess as _sp
    _iw_out = _sp.check_output(["/usr/sbin/iw", "dev", args.iface, "info"], stderr=_sp.STDOUT)
    if "type AP" in _iw_out:
        logger.info("wlan0 is in AP mode, skipping WiFi RID scan")
    else:
        logger.info("starting RID scanner...")
        if not start_scan(iface=args.iface, channel=args.channel, callback=drone_callback):
            logger.error("scanner start failed")
            sys.exit(1)
        logger.info("RID scanner started (CH%d)" % args.channel)

    # 4. Web server (main thread)
    logger.info("starting web server...")
    start_web_server(args.port)

if __name__ == "__main__":
    main()
