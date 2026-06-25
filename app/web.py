"""
边缘数据采集器 — 无人机 RID 接收与 MQTT 转发

纯 headless 模式：接收 BLE/WiFi/串口 RID 数据 → 解析 → 计算电力线距离 →
记录轨迹 → MQTT 转发到云服务器。不渲染 HTML 页面，不提供 Web 认证，不重复云 API。
"""

import json
import os
import sys
import threading
import time
import asyncio
from pathlib import Path
from datetime import datetime

from functools import wraps
from flask import Flask, jsonify, request

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from logging_config import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = SCRIPT_DIR.parent

from core.parser.types import lookup_model_by_sn

app = Flask(__name__)

# ── 全局状态 ──
controller = None


# ═══════════════════════════════════════════════════════════
# 边缘设备 API — 仅提供本地数据采集和状态查询
# ═══════════════════════════════════════════════════════════

@app.route('/api/status')
def api_status():
    """边缘设备本地状态"""
    global controller
    if controller is None:
        return jsonify({'running': False, 'mode': '', 'drone_count': 0,
                        'alert_count': 0, 'pl_count': 0, 'drones': [], 'logs': [],
                        'now': datetime.now().strftime('%H:%M:%S'), 'backhaul': None})

    drones = controller.db.get_active_drones() if controller else []
    alert_drones = controller.alert_system.drone_level if controller else {}

    # 补充机型名和电力线名
    device_name = controller._config.get('backhaul', {}).get('device_name', '') if controller else ''
    line_names = {l.line_id: l.name for l in controller.pl_manager.lines}
    for d in drones:
        d['product_model'] = lookup_model_by_sn(d['id']) or ''
        line_id = d.get('nearest_line_id')
        d['line_name'] = line_names.get(line_id, '') if line_id else ''
        d.setdefault('device_name', device_name)

    logs = controller._log_buffer[-50:] if controller and hasattr(controller, '_log_buffer') else []

    bhaul = controller.backhaul if controller else None
    return jsonify({
        'running': controller.running if controller else False,
        'mode': controller.mode if controller else '',
        'drone_count': len(drones),
        'alert_count': len(alert_drones),
        'pl_count': len(controller.pl_manager.lines) if controller else 0,
        'drones': drones,
        'logs': logs,
        'now': datetime.now().strftime('%H:%M:%S'),
        'backhaul': {
            'mqtt_online': bhaul.primary_online if bhaul else False,
            'queue_size': bhaul.queue_size if bhaul else 0,
        } if bhaul else None,
    })


@app.route('/api/start', methods=['POST'])
def api_start():
    global controller
    data = request.json or {}
    mode = data.get('mode', 'ble')
    if controller is None:
        controller = WebController()
    controller.switch_mode(
        mode,
        wifi_interface=data.get('interface'),
        serial_device=data.get('serial_device') or None,
        serial_baud=data.get('serial_baud'),
        serial_auto=data.get('serial_auto'),
        serial_probe_timeout=data.get('serial_probe_timeout'),
    )
    return jsonify({'status': 'ok', 'mode': mode})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    global controller
    if controller:
        controller.stop()
    return jsonify({'status': 'ok'})


@app.route('/api/serial/scan', methods=['GET'])
def api_serial_scan():
    """扫描所有可用串口并检测 ESP32 设备"""
    from receiver.serial_scanner import scan_ports, list_serial_ports
    timeout = float(request.args.get('timeout', 2.0))
    try:
        ports = list_serial_ports()
        results = scan_ports(probe_timeout=timeout)
        return jsonify({
            'ports': ports,
            'found': [{'port': r.port, 'baud': r.baud, 'dev_id': r.dev_id}
                      for r in results],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/serial/connect', methods=['POST'])
def api_serial_connect():
    """切换到指定串口设备"""
    global controller
    if not controller:
        return jsonify({'error': '系统未启动'}), 503
    data = request.json or {}
    port = (data.get('port') or '').strip() or None
    baud = int(data.get('baud', 115200))
    auto_scan = data.get('auto_scan', False)
    probe_timeout = float(data.get('probe_timeout', 2.0))
    controller.switch_mode('serial',
                           serial_device=port,
                           serial_baud=baud,
                           serial_auto=auto_scan,
                           serial_probe_timeout=probe_timeout)
    return jsonify({'status': 'ok', 'port': port or '(auto)'})


@app.route('/api/backhaul')
def api_backhaul():
    """MQTT 数据回传通道状态"""
    global controller
    if not controller or not controller.backhaul:
        return jsonify({'mqtt_online': False})
    bh = controller.backhaul
    return jsonify({
        'mqtt_online': bh.primary_online,
        'queue_size': bh.queue_size,
    })


@app.route('/api/archive/<drone_id>')
def api_archive_drone(drone_id):
    """原始报文存档查询 + 哈希链验证"""
    global controller
    if not controller or not controller.raw_archive:
        return jsonify({'error': 'raw archive not enabled'}), 404
    raw = controller.raw_archive
    chain_ok, count, break_id = raw.verify_chain(drone_id)
    messages = controller.db.get_raw_messages(drone_id, limit=100)
    return jsonify({
        'drone_id': drone_id,
        'chain_intact': chain_ok,
        'chain_length': count,
        'break_at_id': break_id,
        'messages': messages,
    })


@app.route('/api/archive/verify')
def api_archive_verify_all():
    """全量哈希链验证"""
    global controller
    if not controller or not controller.raw_archive:
        return jsonify({'error': 'raw archive not enabled'}), 404
    results = controller.raw_archive.verify_all()
    return jsonify({
        'total': len(results),
        'results': {did: {'intact': ok, 'count': cnt}
                     for did, (ok, cnt) in results.items()},
    })


# ═══════════════════════════════════════════════════════════
# WebController — 边缘数据采集生命周期管理
# ═══════════════════════════════════════════════════════════

class WebController:
    """Web 模式控制器 — 无 tkinter 依赖。DB 初始化只做一次。"""

    def __init__(self):
        self.mode = None
        self.running = False
        self._log_buffer = []
        self._receiver = None
        self._loop = None
        self._thread = None
        self._wifi_interface = None
        self._lock = threading.Lock()

        # 使用共享工厂初始化所有核心组件
        from core.bootstrap import bootstrap_core
        config_path = str(PROJECT_ROOT / 'config' / 'config.yaml')
        core = bootstrap_core(config_path=config_path)
        self._config = core['config']

        # 串口配置从 config.yaml 加载
        from receiver.serial import get_serial_config
        scfg = get_serial_config(self._config)
        self._serial_device = scfg["device"]
        self._serial_baud = scfg["baud"]
        self._serial_auto_scan = scfg["auto_scan"]
        self._serial_probe_timeout = scfg["probe_timeout"]
        self._pl_file = core['pl_file']
        self._stations_file = PROJECT_ROOT / 'config' / 'stations.yaml'
        self.stations = self._load_stations()
        self.db = core['db']
        self.pl_manager = core['pl_manager']
        self.alert_system = core['alert_system']
        self.trajectory_recorder = core['trajectory_recorder']
        self.raw_archive = core['raw_archive']
        self.pilot_notifier = core['pilot_notifier']
        self.pipeline = core['pipeline']
        self.backhaul = core['backhaul']
        self.thresholds = core['thresholds']

        self.backhaul.start()
        self._stale_cleanup_event = threading.Event()
        self._stale_thread = None

    def _load_stations(self):
        """从 YAML 文件加载监控站点列表"""
        import yaml
        try:
            if self._stations_file.exists():
                with open(str(self._stations_file), 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f) or {}
                    return data.get('stations', [])
        except Exception:
            pass
        return []

    def _save_stations_to_yaml(self):
        """将当前站点数据写回 YAML 文件"""
        import yaml
        data = {"stations": self.stations}
        with open(str(self._stations_file), 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)

    def _save_power_lines_to_yaml(self):
        """将当前电力线数据写回 YAML 文件"""
        import yaml
        pl_data = {
            "power_lines": [
                {
                    "name": l.name,
                    "lat1": l.lat1, "lon1": l.lon1, "alt1": l.alt1,
                    "lat2": l.lat2, "lon2": l.lon2, "alt2": l.alt2,
                    "voltage_level": getattr(l, 'voltage_level', ''),
                }
                for l in self.pl_manager.lines
            ]
        }
        with open(str(self._pl_file), 'w', encoding='utf-8') as f:
            yaml.dump(pl_data, f, allow_unicode=True, default_flow_style=False)

    def _reload_pl_db(self):
        """将内存中的电力线列表重新加载到 SQLite (用于 CRUD 后的持久化)"""
        self.db.load_power_lines([{
            'name': l.name, 'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
            'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2, 'id': l.line_id,
            'voltage_level': getattr(l, 'voltage_level', ''),
        } for l in self.pl_manager.lines])
        self._save_power_lines_to_yaml()

    def _stale_drone_cleanup_loop(self):
        """后台线程：定期标记过期无人机 + 清理过期数据 + 清理告警内存"""
        stale_timeout = self._config.get('stale_timeout', 120)
        iteration = 0
        while not self._stale_cleanup_event.is_set():
            self._stale_cleanup_event.wait(timeout=30)
            if self._stale_cleanup_event.is_set():
                break
            iteration += 1
            try:
                count = self.db.mark_stale_drones_as_gone(stale_timeout)
                if count > 0:
                    self._log(f"清理 {count} 个过期无人机", "info")
                if iteration % 120 == 0:
                    self.db.cleanup_stale_data()
                active_ids = {d['id'] for d in self.db.get_active_drones()}
                self.alert_system.cleanup_stale(active_ids)
            except Exception:
                pass

    def switch_mode(self, mode, wifi_interface=None,
                    serial_device=None, serial_baud=None,
                    serial_auto=None, serial_probe_timeout=None):
        """切换接收模式 (不重建 DB, 线程安全)"""
        with self._lock:
            self.stop()
            self.mode = mode
            self._wifi_interface = wifi_interface
            if serial_device is not None:
                self._serial_device = serial_device
            if serial_baud is not None:
                self._serial_baud = serial_baud
            if serial_auto is not None:
                self._serial_auto_scan = serial_auto
            if serial_probe_timeout is not None:
                self._serial_probe_timeout = serial_probe_timeout
            self.start()

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._stale_cleanup_event.clear()
        self._stale_thread = threading.Thread(
            target=self._stale_drone_cleanup_loop, daemon=True
        )
        self._stale_thread.start()

    def stop(self):
        self.running = False
        receiver = self._receiver
        self._receiver = None
        loop = self._loop
        if loop and receiver:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    receiver.stop(), loop
                )
                future.result(timeout=5)
            except Exception:
                pass
        if loop and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        self._loop = None
        self._stale_cleanup_event.set()
        if self._stale_thread and self._stale_thread.is_alive():
            self._stale_thread.join(timeout=5)
        self._stale_thread = None

    def shutdown(self):
        """完全关闭，释放数据库"""
        self.stop()
        try:
            self.backhaul.stop()
        except Exception:
            pass
        try:
            if self.raw_archive:
                self.raw_archive.stop()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass

    def _log(self, msg, level='info'):
        self._log_buffer.append({'time': datetime.now().strftime('%H:%M:%S'), 'msg': msg, 'level': level})
        if len(self._log_buffer) > 200:
            self._log_buffer = self._log_buffer[-100:]

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        from receiver.ble import BLE_RIDReceiver

        def safe_callback(parsed):
            if not self.running or not self._receiver:
                return
            try:
                self._on_rid(parsed)
            except RuntimeError:
                pass

        if self.mode == 'simulated':
            from receiver.simulated import create_simulated_receiver
            self._receiver = create_simulated_receiver(
                callback=safe_callback,
                pl_manager=self.pl_manager,
                drone_count=6,
                update_interval=1.0,
            )
        elif self.mode == 'wifi':
            from receiver.wifi import create_wifi_receiver
            self._receiver = create_wifi_receiver(
                callback=safe_callback,
                interface=self._wifi_interface,
            )
        elif self.mode == 'serial':
            from receiver.serial import create_serial_receiver
            self._receiver = create_serial_receiver(
                callback=safe_callback,
                device=self._serial_device,
                baud=self._serial_baud,
                auto_scan=self._serial_auto_scan,
                scan_timeout=self._serial_probe_timeout,
            )
        else:
            self._receiver = BLE_RIDReceiver(
                callback=safe_callback,
                scan_duration=self._config.get('ble', {}).get('scan_duration', 5.0),
            )

        async def runner():
            mode_names = {'ble': 'BLE 蓝牙', 'wifi': 'WiFi', 'serial': '串口'}
            self._log(f"系统启动 ({mode_names.get(self.mode, self.mode)}模式)", "info")
            await self._receiver.start()

        try:
            self._loop.run_until_complete(runner())
        except (RuntimeError, asyncio.CancelledError):
            pass
        except Exception as e:
            self._log(f"错误: {e}", "crit")
        finally:
            try:
                tasks = asyncio.all_tasks(self._loop)
                for t in tasks:
                    t.cancel()
                self._loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            except Exception:
                pass
            self._loop.close()
            self._loop = None

    def _on_rid(self, parsed):
        if not self.running:
            return

        result = self.pipeline.process(parsed)
        if result is None:
            return

        if result.alert_level:
            level_tag = {'critical': '[危险]', 'severe': '[严重]', 'warning': '[警告]'}.get(result.alert_level, '!')
            self._log(f"{level_tag} [{result.alert_level}] {result.drone_id} "
                      f"距离 {result.nearest_line.name} {result.distance:.0f}m",
                      result.alert_level)


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()

    global controller
    controller = WebController()
    controller.switch_mode('simulated')

    logger.info("边缘数据采集器启动于 http://%s:%s", args.host, args.port)
    logger.info("按 Ctrl+C 停止")

    try:
        app.run(host=args.host, port=args.port, debug=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        if controller:
            controller.shutdown()


if __name__ == '__main__':
    main()
