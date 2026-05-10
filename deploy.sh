#!/usr/bin/env bash
# STREAK 자동 배포 — Ubuntu 22.04+ on Vultr (또는 임의 VPS)
# 사용법:
#   1. Vultr에서 Ubuntu 22.04 인스턴스 생성 ($5/월 인스턴스 충분)
#   2. SSH 접속:  ssh root@YOUR.SERVER.IP
#   3. 이 스크립트 업로드 후 실행: bash deploy.sh
#
# 설치 후 https://YOUR.DOMAIN/ 접속 가능 (도메인 + Let's Encrypt SSL 적용시)

set -euo pipefail

DOMAIN="${1:-}"     # 첫 인자로 도메인 (선택). 없으면 IP 직접 접근
REPO="https://github.com/yujoohwan6342-stack/POLY.git"
APP_DIR="/opt/streak"

echo "==> System update"
apt-get update -y && apt-get upgrade -y

echo "==> Install dependencies"
apt-get install -y python3 python3-pip git nginx ufw curl
[ -n "$DOMAIN" ] && apt-get install -y certbot python3-certbot-nginx

echo "==> Create user"
id -u streak >/dev/null 2>&1 || useradd -r -s /bin/false -d /opt/streak streak

echo "==> Clone code"
if [ -d "$APP_DIR/.git" ]; then
    cd "$APP_DIR" && git pull
else
    rm -rf "$APP_DIR"
    git clone "$REPO" "$APP_DIR"
fi
chown -R streak:streak "$APP_DIR"

echo "==> Install Python packages"
pip3 install --break-system-packages -r "$APP_DIR/requirements.txt"

echo "==> Install systemd service"
cp "$APP_DIR/streak.service" /etc/systemd/system/streak.service
systemctl daemon-reload
systemctl enable streak
systemctl restart streak

echo "==> Configure nginx"
cp "$APP_DIR/nginx.conf" /etc/nginx/sites-available/streak
[ -n "$DOMAIN" ] && sed -i "s/streak.example.com/$DOMAIN/" /etc/nginx/sites-available/streak
ln -sf /etc/nginx/sites-available/streak /etc/nginx/sites-enabled/streak
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo "==> Firewall"
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

if [ -n "$DOMAIN" ]; then
    echo "==> SSL via Let's Encrypt for $DOMAIN"
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "admin@$DOMAIN" --redirect
fi

echo ""
echo "✓ Deploy complete"
echo ""
sleep 2
systemctl status streak --no-pager | head -10
echo ""
if [ -n "$DOMAIN" ]; then
    echo "→ Open: https://$DOMAIN/"
else
    IP=$(curl -s ifconfig.me)
    echo "→ Open: http://$IP/"
fi
echo ""
echo "Logs:    journalctl -u streak -f"
echo "Restart: systemctl restart streak"
echo "Status:  systemctl status streak"
