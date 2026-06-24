"""
Locust 负载测试 — Drone RID 云服务 API

用法:
  1. 启动开发服务器 (另开终端):
     $env:PYTHONUTF8=1
     python app/server.py --db sqlite:///data/center.db --port 5000

  2. 运行 locust:
     $env:PYTHONUTF8=1
     locust -f tests/locustfile.py --host http://localhost:5000

  3. 浏览器 http://localhost:8089 → 填并发数 → Start

  4. 或 headless:
     locust -f tests/locustfile.py --host http://localhost:5000 `
       --headless -u 20 -r 5 -t 30s --csv=results/load_test

流量模型 (模拟运维人员浏览器):
  - 80% GET /api/status    (仪表盘轮询, 2-5s 间隔)
  - 10% GET /api/alerts/history (告警记录)
  - 10% GET /api/health    (健康检查)
"""

from locust import FastHttpUser, task, between


class DashboardUser(FastHttpUser):
    """模拟运维人员浏览器 — 仪表盘轮询"""

    wait_time = between(2, 5)

    def on_start(self):
        """登录获取 session cookie (web 表单登录)"""
        resp = self.client.post("/login", data={
            "username": "admin",
            "password": "admin123",
        }, allow_redirects=False)
        if resp.status_code in (200, 302):
            self._logged_in = True

    @task(8)    # 权重 8/10 = 80%
    def poll_status(self):
        with self.client.get("/api/status", catch_response=True) as resp:
            if resp.status_code == 200:
                data = resp.json()
                assert "drone_count" in data, "missing drone_count"
                assert isinstance(data.get("drones", []), list), "drones not a list"
            elif resp.status_code == 401:
                resp.success()  # 未登录时鉴权正常

    @task(1)    # 权重 1/10 = 10%
    def get_alert_history(self):
        with self.client.get("/api/alerts/history", catch_response=True) as resp:
            if resp.status_code in (200, 401):
                resp.success()

    @task(1)    # 权重 1/10 = 10%
    def health_check(self):
        with self.client.get("/api/health", catch_response=True) as resp:
            if resp.status_code == 200:
                data = resp.json()
                assert data["status"] == "ok"
