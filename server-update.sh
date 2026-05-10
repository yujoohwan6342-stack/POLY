#!/usr/bin/env bash
# 서버 한 줄 업데이트 — 사이트 복구 + 의존성 설치 + DB 마이그레이션 + 재시작
# 사용법: 서버 SSH 접속 후
#   curl -sSL https://raw.githubusercontent.com/yujoohwan6342-stack/POLY/main/server-update.sh | bash
set -euo pipefail

APP_DIR="/opt/streak-main"
ENV_FILE="/etc/streak/main.env"

echo "==> 1/5 git pull"
cd "$APP_DIR" && git pull --ff-only origin main

echo "==> 2/5 install python deps (incl firebase-admin)"
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/backend/requirements.txt" --quiet

echo "==> 3/5 ensure env file exists with defaults"
mkdir -p /etc/streak /var/lib/streak
if [ ! -f "$ENV_FILE" ]; then
    JWT=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    cat > "$ENV_FILE" <<EOF
JWT_SECRET=$JWT
DATABASE_URL=sqlite:////var/lib/streak/streak.db
ALLOWED_ORIGINS=*
FIREBASE_PROJECT_ID=hedam-poly
EOF
    chmod 600 "$ENV_FILE"
fi

# Firebase 미설정 키들 자동 추가 (있으면 유지, 없으면 빈 값)
for k in FIREBASE_API_KEY FIREBASE_AUTH_DOMAIN FIREBASE_APP_ID FIREBASE_MESSAGING_SENDER_ID FIREBASE_STORAGE_BUCKET; do
    grep -q "^$k=" "$ENV_FILE" || echo "$k=" >> "$ENV_FILE"
done
grep -q "^GOOGLE_APPLICATION_CREDENTIALS=" "$ENV_FILE" || \
    echo "GOOGLE_APPLICATION_CREDENTIALS=/etc/streak/firebase-service-account.json" >> "$ENV_FILE"

echo "==> 4/5 wipe DB (스키마 변경됨 — User 테이블 firebase_uid 추가)"
if [ -f /var/lib/streak/streak.db ]; then
    cp /var/lib/streak/streak.db "/var/lib/streak/streak.db.backup.$(date +%s)"
    rm /var/lib/streak/streak.db
    echo "    이전 DB는 백업됨 → /var/lib/streak/streak.db.backup.*"
fi

echo "==> 5/5 restart service"
systemctl restart streak-main
sleep 5

echo
echo "==> verify"
systemctl status streak-main --no-pager | head -8
echo
ss -tlnp | grep :8000 && echo "✓ port 8000 listening" || echo "✗ port 8000 NOT listening"
echo
HEALTH=$(curl -s http://127.0.0.1:8000/api/health)
echo "/api/health → $HEALTH"
echo
HTTPS=$(curl -sI https://hedam.io/ | head -1)
echo "https://hedam.io/ → $HTTPS"
echo
echo "✓ 끝. 사이트가 다시 열리면 OK."
echo "  - Firebase 로그인은 환경변수 설정 후 가능 (지금은 503)"
echo "  - 랜딩 페이지 + 카운터는 작동"
