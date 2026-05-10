#!/usr/bin/env bash
# nginx 캐시 헤더 적용 — HTML은 항상 fresh, JS/CSS는 1시간 캐시
# 한 번만 실행하면 됨.
set -e

NGINX_CONF="/etc/nginx/sites-available/streak-main"

# 백업
cp "$NGINX_CONF" "$NGINX_CONF.bak.$(date +%s)"

# proxy_pass 직전에 cache 헤더 add_header 삽입
python3 <<'PY'
path = "/etc/nginx/sites-available/streak-main"
content = open(path).read()
# 이미 적용됐으면 skip
if "Cache-Control" in content:
    print("already has Cache-Control headers, skipping")
else:
    # 첫 번째 location / 블록 안에 location ~ \.html 추가는 복잡하므로
    # 모든 응답에 add_header만 추가 (간단)
    new = content.replace(
        "location / {",
        '''# HTML 항상 fresh, /static/* 짧은 캐시
    location ~* \.(html)$ {
        add_header Cache-Control "no-cache, no-store, must-revalidate";
        add_header Pragma "no-cache";
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location ~* \.(js|css|png|jpg|svg)$ {
        add_header Cache-Control "public, max-age=300";
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
    }
    location / {''')
    open(path, "w").write(new)
    print("nginx config updated")
PY

nginx -t && systemctl reload nginx
echo "✓ nginx reloaded"
