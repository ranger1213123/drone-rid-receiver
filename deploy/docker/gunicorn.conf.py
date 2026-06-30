"""Gunicorn 生产配置 — 中心服务器 (eventlet worker for WebSocket)"""

bind = "0.0.0.0:5000"
workers = 1
worker_class = "gevent"
timeout = 30
keepalive = 5
max_requests = 5000
max_requests_jitter = 200

# 日志
accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sμs'

# 安全
limit_request_line = 4096
limit_request_fields = 100
