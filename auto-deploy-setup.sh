#!/usr/bin/env bash
# 서버에 1분 주기 git auto-pull cron 설치 (한 번만 실행하면 됨)
# 이후엔 git push만 하면 60초 내 사이트 자동 업데이트.
# 사용법: 서버 SSH 접속 후
#   curl -sSL https://raw.githubusercontent.com/yujoohwan6342-stack/POLY/main/auto-deploy-setup.sh | bash
set -euo pipefail

APP_DIR="/opt/streak-main"
SCRIPT="/usr/local/bin/streak-autopull"

cat > "$SCRIPT" <<'PULLER'
#!/usr/bin/env bash
# 1분마다 호출됨. 새 커밋이 있으면 pull + 재시작.
set -e
cd /opt/streak-main
LOCAL=$(git rev-parse HEAD 2>/dev/null || echo "")
git fetch origin main --quiet
REMOTE=$(git rev-parse origin/main 2>/dev/null || echo "")
if [ "$LOCAL" != "$REMOTE" ] && [ -n "$REMOTE" ]; then
    echo "[$(date -u +%FT%TZ)] new commit detected: $LOCAL → $REMOTE"
    git pull --ff-only origin main
    /opt/streak-main/.venv/bin/pip install -r backend/requirements.txt --quiet
    systemctl restart streak-main
    echo "[$(date -u +%FT%TZ)] redeployed"
fi
PULLER
chmod +x "$SCRIPT"

# systemd timer (cron보다 robust)
cat > /etc/systemd/system/streak-autopull.service <<EOF
[Unit]
Description=STREAK auto pull from GitHub
[Service]
Type=oneshot
ExecStart=$SCRIPT
StandardOutput=append:/var/log/streak-autopull.log
StandardError=append:/var/log/streak-autopull.log
EOF

cat > /etc/systemd/system/streak-autopull.timer <<EOF
[Unit]
Description=Run STREAK auto pull every minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now streak-autopull.timer

echo "✓ 자동 배포 활성화"
echo "  - 1분마다 git fetch & pull check"
echo "  - 새 커밋 있으면 자동 deps 재설치 + restart"
echo "  - 로그: tail -f /var/log/streak-autopull.log"
echo "  - timer 상태: systemctl list-timers streak-autopull.timer"
echo ""
echo "이제 git push만 하면 60초 내 사이트 자동 업데이트됩니다."
