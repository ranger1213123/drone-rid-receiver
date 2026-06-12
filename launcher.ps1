<#
.SYNOPSIS
    无人机 RID 接收与电力线防碰撞监控系统 — Windows 启动器
.DESCRIPTION
    PowerShell 原生 UTF-8 支持，中文菜单正常显示
.NOTES
    用法: 右键 → 使用 PowerShell 运行
    或:   powershell -ExecutionPolicy Bypass -File launcher.ps1
#>

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

# ── 环境检查 ──
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "[错误] 未找到 Python" -ForegroundColor Red
    Write-Host "请安装 Python 3.10+: https://www.python.org/downloads/"
    Read-Host "按 Enter 退出"
    exit 1
}

$pyVer = & python --version 2>&1
Write-Host "[信息] $pyVer" -ForegroundColor Cyan

# ── 主菜单 ──
function Show-Menu {
    Clear-Host
    Write-Host ""
    Write-Host "╔══════════════════════════════════════════════════════╗" -ForegroundColor Green
    Write-Host "║     无人机 RID 接收与电力线防碰撞监控系统 v2.0       ║" -ForegroundColor Green
    Write-Host "╠══════════════════════════════════════════════════════╣" -ForegroundColor Green
    Write-Host "║                                                      ║" -ForegroundColor Green
    Write-Host "║  [1] 启动 GUI (模拟数据测试)                          ║" -ForegroundColor White
    Write-Host "║  [2] 启动 GUI (BLE 蓝牙扫描)                          ║" -ForegroundColor White
    Write-Host "║  [3] 启动 GUI (WiFi 扫描)                             ║" -ForegroundColor White
    Write-Host "║  [4] 启动 CLI 终端模式                                ║" -ForegroundColor White
    Write-Host "║  [5] 运行单元测试                                     ║" -ForegroundColor White
    Write-Host "║  [6] 安装全部依赖                                     ║" -ForegroundColor White
    Write-Host "║  [0] 退出                                             ║" -ForegroundColor White
    Write-Host "║                                                      ║" -ForegroundColor Green
    Write-Host "╚══════════════════════════════════════════════════════╝" -ForegroundColor Green
    Write-Host ""
}

do {
    Show-Menu
    $choice = Read-Host "请输入选项 [0-6]"
    
    switch ($choice) {
        "1" {
            Write-Host "[启动] GUI (模拟模式)" -ForegroundColor Yellow
            & python -B src\main_gui.py
        }
        "2" {
            Write-Host "[启动] GUI (BLE 蓝牙模式)" -ForegroundColor Yellow
            try { python -c "import bleak" } catch { 
                Write-Host "[安装] bleak..." -ForegroundColor Cyan
                pip install -q bleak
            }
            & python -B src\main_gui.py
        }
        "3" {
            Write-Host "[启动] GUI (WiFi 模式)" -ForegroundColor Yellow
            Write-Host "提示: WiFi 模式需要安装 Npcap https://npcap.com" -ForegroundColor DarkGray
            try { python -c "import scapy" } catch {
                Write-Host "[安装] scapy..." -ForegroundColor Cyan
                pip install -q scapy
            }
            & python -B src\main_gui.py
        }
        "4" {
            Write-Host "[启动] CLI 终端模式" -ForegroundColor Yellow
            & python -B src\main.py --mode mock
        }
        "5" {
            Write-Host "[测试] 运行单元测试..." -ForegroundColor Yellow
            try { python -c "import pytest" } catch { pip install -q pytest }
            & python -m pytest tests\test_system.py -v
            Read-Host "按 Enter 继续"
        }
        "6" {
            Write-Host "[安装] 全部依赖..." -ForegroundColor Yellow
            pip install -r requirements.txt
            Write-Host "完成!" -ForegroundColor Green
            Read-Host "按 Enter 继续"
        }
        "0" {
            Write-Host "再见!" -ForegroundColor Green
        }
    }
} while ($choice -ne "0")
