#!/usr/bin/env python3
"""
环境检测脚本 — 检查 RID 接收所需的所有依赖

用法:
  python check_env.py

检测项目:
  - Python 版本
  - scapy (WiFi Beacon 扫描)
  - Npcap (Windows raw 802.11 捕获)
  - bleak (BLE 扫描)
  - 数据库 (SQLite)
"""

import sys
import subprocess


def check_python():
    print(f"  Python:  v{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    if sys.version_info < (3, 8):
        print("    [WARN]  Python 3.8 or higher recommended")
    return True


def check_module(name, required_for=""):
    try:
        __import__(name)
        print(f"  {name:<20} [OK] {required_for}")
        return True
    except ImportError:
        print(f"  {name:<20} [MISSING] {required_for}")
        return False


def check_npcap():
    """检查 Npcap 是否安装 (Windows)"""
    if sys.platform != "win32":
        print("  Npcap               [SKIP] not Windows")
        return None

    # 检查注册表
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE,):
            for path in [
                r"SOFTWARE\Npcap",
                r"SOFTWARE\WOW6432Node\Npcap",
            ]:
                try:
                    key = winreg.OpenKey(hive, path)
                    winreg.CloseKey(key)
                    print("  Npcap               [OK] installed")
                    return True
                except OSError:
                    continue
    except Exception:
        pass

    # 检查 npcap 服务
    try:
        result = subprocess.run(
            ["sc", "query", "npcap"],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if "RUNNING" in result.stdout or "STOPPED" in result.stdout:
            print("  Npcap               [OK] service found")
            return True
    except Exception:
        pass

    print("  Npcap               [MISSING] needed for WiFi RID capture")
    print("    Download: https://npcap.com/dist/npcap-1.80.exe")
    print("    Install with 'Support raw 802.11 traffic' checked!")
    return False


def main():
    print("=" * 60)
    print("  Drone RID Receiver - Environment Check")
    print("=" * 60)
    print()

    ok = True

    print("[System]")
    ok &= check_python()
    print()

    print("[Python Modules]")
    ok &= check_module("scapy", "WiFi Beacon 扫描")
    ok &= check_module("bleak", "BLE 扫描")
    ok &= check_module("flask", "Web GUI")
    ok &= check_module("yaml", "配置文件")
    print()

    print("[WiFi Capture Driver]")
    npcap_ok = check_npcap()
    if npcap_ok is False:
        ok = False
    print()

    print("[Database]")
    try:
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.close()
        print("  SQLite              [OK]")
    except Exception:
        print("  SQLite              [ERROR]")
        ok = False
    print()

    print("=" * 60)
    if ok:
        print("  All checks passed!")
        print()
        print("  Quick start:")
        print("    python scan_drone.py --mode wifi    # WiFi Beacon scan")
        print("    python scan_drone.py --mode ble     # BLE scan")
        print("    python src/main.py --mode wifi      # Full system")
    else:
        print("  Some checks failed. Install missing dependencies above.")
        print()
        print("  Quick fix:")
        print("    pip install scapy bleak flask pyyaml")
        print("    # Download Npcap: https://npcap.com")
    print("=" * 60)


if __name__ == "__main__":
    main()
