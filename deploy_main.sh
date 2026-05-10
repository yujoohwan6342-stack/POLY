#!/usr/bin/env bash
# STREAK 메인 사이트 (백엔드 + 프론트엔드) Vultr 배포
# 사용법:
#   ssh root@YOUR.SERVER.IP
#   curl -sSL https://raw.githubusercontent.com/yujoohwan6342-stack/POLY/main/deploy_main.sh | bash -s yourdomain.com

set -euo pipefail

DOMAIN="${1:-}"
REPO="https://github.com/yujoohwan6342-stack/POLY.git"
APP_DIR="/opt/streak-main"

echo "==> System update"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip python3-venv git nginx ufw curl
[ -n "$DOMAIN" ] && apt-get install -y certbot python3-certbot-nginx

echo "==> Clone"
if [ -d "$APP_DIR/.git" ]; then
    cd "$APP_DIR" && git pull
else
    rm -rf "$APP_DIR"
    git clone "$REPO" "$APP_DIR"
fi

echo "==> Python venv + deps"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/backend/requirements.txt"

echo "==> Generate JWT secret"
JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
mkdir -p /etc/streak
cat > /etc/streak/main.env <<EOF
JWT_SECRET=$JWT_SECRET
DEPOSIT_ADDRESS=0xCf9907F84Fe2B9e2976766f466EFE37bCb738CA0
SIWE_DOMAIN=${DOMAIN:-localhost}
SIWE_URI=https://${DOMAIN:-localhost}
ALLOWED_ORIGINS=https://${DOMAIN:-localhost}
DATABASE_URL=sqlite:////var/lib/streak/streak.db
EOF
mkdir -p /var/lib/streak
chmod 600 /etc/streak/main.env

echo "==> systemd service"
cat > /etc/systemd/system/streak-main.service <<EOF
[Unit]
Description=STREAK main backend
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/streak/main.env
ExecStart=$APP_DIR/.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/streak-main.log
StandardError=append:/var/log/streak-main.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable streak-main
systemctl restart streak-main

echo "==> nginx"
cat > /etc/nginx/sites-available/streak-main <<EOF
server {
    listen 80;
    server_name ${DOMAIN:-_};

    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
ln -sf /etc/nginx/sites-available/streak-main /etc/nginx/sites-enabled/streak-main
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo "==> Firewall"
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable

if [ -n "$DOMAIN" ]; then
    echo "==> SSL"
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "admin@$DOMAIN" --redirect
fi

echo ""
echo "✓ Deploy complete"
sleep 2
systemctl status streak-main --no-pager | head -10
echo ""
if [ -n "$DOMAIN" ]; then
    echo "→ https://$DOMAIN/"
else
    IP=$(curl -s ifconfig.me)
    echo "→ http://$IP/"
fi
echo ""
echo "Logs: journalctl -u streak-main -f"
