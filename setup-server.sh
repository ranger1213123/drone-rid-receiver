#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Drone RID 服务器一键初始化脚本 (Cloudflare Origin CA)
# 在服务器上运行: bash setup-server.sh
# ═══════════════════════════════════════════════════════════════

set -e

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "================================================"
echo "  Drone RID 中心服务器 — 生产部署初始化"
echo "================================================"
echo ""

# ── 1. 检查 Docker ──
if ! command -v docker &> /dev/null; then
    echo -e "${RED}错误: 未安装 Docker，请先安装: curl -fsSL https://get.docker.com | sh${NC}"
    exit 1
fi

if ! docker compose version &> /dev/null 2>&1; then
    echo -e "${RED}错误: 需要 Docker Compose v2 (docker compose 命令)${NC}"
    exit 1
fi

echo -e "${GREEN}[OK] Docker + Compose 已就绪${NC}"

# ── 2. 交互式配置 ──
echo ""
echo "--- 基本配置 ---"

read -p "域名 (如 drone.example.com): " DOMAIN
read -p "管理员用户名 [admin]: " ADMIN_USER
ADMIN_USER=${ADMIN_USER:-admin}
read -p "管理员密码 [留空自动生成]: " ADMIN_PASS

echo ""
echo "--- Cloudflare 证书配置 ---"
echo -e "${YELLOW}请先在 Cloudflare 后台生成 Origin CA 证书:${NC}"
echo "  SSL/TLS → Origin Server → Create Certificate"
echo "  下载 cert.pem 和 key.pem"
echo ""
read -p "证书文件所在目录 [./certs]: " CERT_DIR
CERT_DIR=${CERT_DIR:-./certs}

if [ ! -f "$CERT_DIR/cert.pem" ] || [ ! -f "$CERT_DIR/key.pem" ]; then
    echo -e "${RED}错误: 在 $CERT_DIR 目录下找不到 cert.pem 和 key.pem${NC}"
    echo "请先把 Cloudflare Origin CA 证书放到 $CERT_DIR/ 后再运行"
    exit 1
fi
echo -e "${GREEN}[OK] 证书已就位${NC}"

echo ""
echo "--- 生成随机密钥 ---"

DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_hex(16))" 2>/dev/null || openssl rand -hex 16)
JWT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || openssl rand -hex 32)
WEB_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || openssl rand -hex 32)
EMQX_ADMIN_PASSWORD=$(python3 -c "import secrets; print(secrets.token_hex(8))" 2>/dev/null || openssl rand -hex 8)

if [ -z "$ADMIN_PASS" ]; then
    ADMIN_PASS=$(python3 -c "import secrets; print(secrets.token_hex(8))" 2>/dev/null || openssl rand -hex 8)
fi

# ── 3. 生成 .env ──
echo ""
echo "--- 写入 .env ---"

cat > .env << EOF
# 域名
DOMAIN=${DOMAIN}

# 数据库
DB_PASSWORD=${DB_PASSWORD}

# JWT + Session
JWT_SECRET_KEY=${JWT_SECRET_KEY}
WEB_SECRET_KEY=${WEB_SECRET_KEY}

# CORS
CORS_ORIGINS=https://${DOMAIN}

# EMQX
EMQX_ADMIN_PASSWORD=${EMQX_ADMIN_PASSWORD}

# 管理员
ADMIN_USER=${ADMIN_USER}
ADMIN_PASS=${ADMIN_PASS}

# 设备密钥
DEVICE_SECRETS={}
EOF

echo -e "${GREEN}[OK] .env 已生成${NC}"

# ── 4. 构建并启动 ──
echo ""
echo "--- 构建 Docker 镜像 ---"
docker compose build

echo ""
echo "--- 启动所有服务 ---"
docker compose up -d

echo ""
echo "================================================"
echo -e "${GREEN}部署完成!${NC}"
echo ""
echo "  Web 仪表盘:    https://${DOMAIN}"
echo "  EMQX 管理:     ssh -L 18083:localhost:18083 your-server"
echo "                 然后打开 http://localhost:18083"
echo ""
echo "  管理员:         ${ADMIN_USER} / ${ADMIN_PASS}"
echo "  MQTT Broker:    ${DOMAIN}:1883"
echo ""
echo "  Cloudflare SSL: 请确认 SSL/TLS 模式设为 Full (strict)"
echo ""
echo "  查看日志:       docker compose logs -f web"
echo "  重启服务:       docker compose restart web"
echo "================================================"
