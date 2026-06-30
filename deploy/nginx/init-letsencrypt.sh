#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Let's Encrypt 证书申请脚本 (首次运行)
# 用法: DOMAIN=drone.yourdomain.com EMAIL=admin@yourdomain.com ./init-letsencrypt.sh
# ═══════════════════════════════════════════════════════════════

set -e

if [ -z "$DOMAIN" ]; then
    echo "错误: 请设置 DOMAIN 环境变量"
    echo "用法: DOMAIN=drone.yourdomain.com EMAIL=admin@yourdomain.com $0"
    exit 1
fi

EMAIL=${EMAIL:-admin@$DOMAIN}
CERTBOT_DIR="./certbot"
WWW_DIR="./certbot/www"

mkdir -p "$CERTBOT_DIR/conf" "$WWW_DIR"

# 生成临时 Nginx 配置 (仅 HTTP，用于证书验证)
cat > ./nginx-temp.conf << NGINX_EOF
events { worker_connections 1024; }
http {
    include /etc/nginx/mime.types;
    server {
        listen 80;
        server_name ${DOMAIN};
        location /.well-known/acme-challenge/ {
            root /var/www/certbot;
        }
        location / {
            return 200 'OK';
        }
    }
}
NGINX_EOF

echo "=== 启动临时 Nginx 用于证书验证 ==="
docker run -d --name nginx-temp \
    -p 80:80 \
    -v "$(pwd)/nginx-temp.conf:/etc/nginx/nginx.conf:ro" \
    -v "$(pwd)/$WWW_DIR:/var/www/certbot:ro" \
    nginx:alpine

echo "=== 申请 Let's Encrypt 证书 ==="
docker run --rm \
    -v "$(pwd)/$CERTBOT_DIR/conf:/etc/letsencrypt" \
    -v "$(pwd)/$WWW_DIR:/var/www/certbot" \
    certbot/certbot certonly \
    --webroot -w /var/www/certbot \
    --email "$EMAIL" \
    --domain "$DOMAIN" \
    --agree-tos \
    --non-interactive

echo "=== 清理临时容器 ==="
docker stop nginx-temp && docker rm nginx-temp
rm -f nginx-temp.conf

echo ""
echo "=== 证书已申请成功! ==="
echo "证书位置: $(pwd)/$CERTBOT_DIR/conf/live/$DOMAIN/"
echo ""
echo "现在可以启动正式服务: docker compose up -d"
