"""
Flask Web GUI - 无人机 RID 接收与电力线防碰撞监控系统

纯 Python 依赖 (flask), 不依赖 tkinter。
浏览器访问 http://localhost:5000 即可使用。
"""

import json
import os
import sys
import threading
import time
import asyncio
from pathlib import Path
from datetime import datetime

from flask import Flask, render_template, jsonify, request

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from logging_config import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = SCRIPT_DIR.parent

from core.parser.types import UA_TYPE_NAMES, lookup_model_by_sn

app = Flask(__name__, template_folder=str(PROJECT_ROOT / 'templates'))

# ── 全局状态 ──
controller = None

@app.route('/')
def index():
    return render_template('map.html')


@app.route('/list')
def list_view():
    return render_template('dashboard.html')

@app.route('/api/status')
def api_status():
    global controller
    drones = controller.db.get_active_drones() if controller else []
    alert_drones = dict(controller.alert_system._drone_level) if controller else {}

    # Add power line names, model names to drones
    if controller:
        line_names = {l.line_id: l.name for l in controller.pl_manager.lines}
        for d in drones:
            d['category_name'] = UA_TYPE_NAMES.get(d.get('ua_type', 0), '未知')
            d['product_model'] = lookup_model_by_sn(d['id']) or ''
            line_id = d.get('nearest_line_id')
            d['line_name'] = line_names.get(line_id, '') if line_id else ''

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
            'channel': bhaul.channel_status if bhaul else 'offline',
            'primary_online': bhaul.primary_online if bhaul else False,
            'beidou_online': bhaul.beidou_online if bhaul else False,
            'active_channel': bhaul.active_channel if bhaul else 'none',
            'queue_size': bhaul.queue_size if bhaul else 0,
            'stats': bhaul.stats if bhaul else {},
        } if bhaul else None,
    })

@app.route('/api/start', methods=['POST'])
def api_start():
    global controller
    data = request.json or {}
    mode = data.get('mode', 'ble')
    if controller is None:
        controller = WebController()
    controller.switch_mode(mode, wifi_interface=data.get('interface'))
    return jsonify({'status': 'ok', 'mode': mode})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    global controller
    if controller:
        controller.stop()
    return jsonify({'status': 'ok'})

@app.route('/api/powerlines', methods=['GET', 'POST', 'DELETE'])
def api_powerlines():
    global controller
    if not controller:
        return jsonify([])
    if request.method == 'GET':
        return jsonify([{
            'name': l.name, 'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
            'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2
        } for l in controller.pl_manager.lines])
    elif request.method == 'POST':
        data = request.json
        from core.powerline import PowerLineSegment
        seg = PowerLineSegment(
            name=data['name'], lat1=data['lat1'], lon1=data['lon1'], alt1=data['alt1'],
            lat2=data['lat2'], lon2=data['lon2'], alt2=data['alt2'],
            line_id=len(controller.pl_manager.lines) + 1
        )
        controller.pl_manager.lines.append(seg)
        controller.db.load_power_lines([{
            'name': l.name, 'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
            'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2, 'id': l.line_id
        } for l in controller.pl_manager.lines])
        controller._save_power_lines_to_yaml()
        return jsonify({'status': 'ok'})

@app.route('/api/powerlines/<int:idx>', methods=['DELETE'])
def api_delete_powerline(idx):
    global controller
    if controller and idx < len(controller.pl_manager.lines):
        del controller.pl_manager.lines[idx]
        controller.db.load_power_lines([{
            'name': l.name, 'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
            'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2, 'id': i+1
        } for i, l in enumerate(controller.pl_manager.lines)])
        controller._save_power_lines_to_yaml()
    return jsonify({'status': 'ok'})

@app.route('/api/trajectories')
def api_trajectories():
    global controller
    if not controller:
        return jsonify({})
    result = {}
    drones = controller.db.get_active_drones()
    for d in drones:
        did = d['id']
        points = controller.db.get_trajectory(did, 500)
        if points:
            result[did] = {
                'count': len(points),
                'min_dist': min(p['distance_to_line'] for p in points),
                'first': points[-1]['timestamp'][:19] if points else '',
                'last': points[0]['timestamp'][:19] if points else '',
            }
    return jsonify(result)


@app.route('/api/trajectories/<drone_id>/points')
def api_trajectory_points(drone_id):
    """返回指定无人机轨迹点坐标（用于地图绘制）"""
    global controller
    if not controller:
        return jsonify([])
    points = controller.db.get_trajectory(drone_id, 500)
    return jsonify([{
        'lat': p['lat'],
        'lon': p['lon'],
        'alt': p['alt'],
        'distance': p['distance_to_line'],
        'time': p['timestamp'][:19] if p['timestamp'] else '',
    } for p in reversed(points)])


@app.route('/api/backhaul')
def api_backhaul():
    """数据回传通道状态"""
    global controller
    if not controller or not controller.backhaul:
        return jsonify({'channel': 'offline', 'primary_online': False,
                        'beidou_online': False, 'active_channel': 'none'})
    bh = controller.backhaul
    return jsonify({
        'channel': bh.channel_status,
        'primary_online': bh.primary_online,
        'beidou_online': bh.beidou_online,
        'active_channel': bh.active_channel,
        'queue_size': bh.queue_size,
        'stats': bh.stats,
    })


@app.route('/api/stats/dashboard')
def api_stats_dashboard():
    """聚合统计: 24h 告警趋势 + 机型分布 + 站点信息"""
    global controller
    if not controller:
        return jsonify({})

    try:
        from core.parser.types import UA_TYPE_NAMES, lookup_model_by_sn
        from collections import Counter

        hourly = controller.db.get_hourly_alert_counts(24)
        ua_stats = controller.db.get_ua_type_stats()
        # 产品型号分布 — 优先 lookup_model_by_sn，无匹配 fallback 到 UA_TYPE_NAMES
        model_counts = Counter()
        for d in controller.db.get_active_drones():
            model = lookup_model_by_sn(d['id']) or UA_TYPE_NAMES.get(d.get('ua_type', 0), '未知')
            model_counts[model] += 1
        model_dist = [{'name': k, 'count': v} for k, v in model_counts.most_common()]

        bh = controller.backhaul
        pos_lat, pos_lon, pos_alt = 0.0, 0.0, 0.0
        try:
            if bh:
                pos_lat, pos_lon, pos_alt = bh._get_device_position()
        except Exception:
            pass

        station = {
            'device_name': controller._config.get('backhaul', {}).get('device_name', 'NW-F1'),
            'device_location': controller._config.get('backhaul', {}).get('device_location', ''),
            'position': {'lat': pos_lat, 'lon': pos_lon, 'alt': pos_alt},
            'active_channel': str(bh.active_channel) if bh else 'none',
            'primary_online': bool(bh.primary_online) if bh else False,
            'beidou_online': bool(bh.beidou_online) if bh else False,
            'beidou_signal': 0,
            'queue_size': int(bh.queue_size) if bh else 0,
            'http_sent': int(bh.stats.get('http_sent', 0)) if bh else 0,
            'beidou_sent': int(bh.stats.get('beidou_sent', 0)) if bh else 0,
            'last_send': str(bh.stats.get('last_send_time', '--')) if bh else '--',
            'pl_count': len(controller.pl_manager.lines),
            'drone_count': len(controller.db.get_active_drones()),
        }
    except Exception as e:
        logger.warning("stats/dashboard 查询失败: %s", e)
        return jsonify({'error': str(e)}), 500

    return jsonify({
        'hourly_alerts': hourly,
        'model_dist': model_dist,
        'station': station,
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

        # 使用共享工厂初始化所有核心组件
        from core.bootstrap import bootstrap_core
        config_path = str(PROJECT_ROOT / 'config' / 'config.yaml')
        core = bootstrap_core(config_path=config_path)
        self._config = core['config']
        self._pl_file = core['pl_file']
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

    def _save_power_lines_to_yaml(self):
        """将当前电力线数据写回 YAML 文件"""
        import yaml
        pl_data = {
            "power_lines": [
                {
                    "name": l.name,
                    "lat1": l.lat1, "lon1": l.lon1, "alt1": l.alt1,
                    "lat2": l.lat2, "lon2": l.lon2, "alt2": l.alt2,
                }
                for l in self.pl_manager.lines
            ]
        }
        with open(str(self._pl_file), 'w', encoding='utf-8') as f:
            yaml.dump(pl_data, f, allow_unicode=True, default_flow_style=False)

    def switch_mode(self, mode, wifi_interface=None):
        """切换接收模式 (不重建 DB)"""
        self.stop()
        self.mode = mode
        self._wifi_interface = wifi_interface
        self.start()

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

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
                pass  # event loop closed during shutdown

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
        else:
            self._receiver = BLE_RIDReceiver(
                callback=safe_callback,
                scan_duration=self._config.get('ble', {}).get('scan_duration', 5.0),
            )

        async def runner():
            mode_names = {'ble': 'BLE 蓝牙', 'wifi': 'WiFi'}
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


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    
    global controller
    controller = WebController()
    controller.switch_mode('simulated')

    logger.info("Drone RID Receiver Web GUI 启动")
    logger.info("浏览器打开: http://%s:%s", args.host, args.port)
    logger.info("按 Ctrl+C 停止")
    
    try:
        app.run(host=args.host, port=args.port, debug=False)
    except KeyboardInterrupt:
        pass
    finally:
        if controller:
            controller.shutdown()


if __name__ == '__main__':
    main()
