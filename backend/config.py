"""환경 설정 (env vars + defaults)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# Database
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR}/streak.db")

# Firebase Auth
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "project-205632245559")
# 클라이언트가 읽는 공개 config (백엔드가 /api/config로 노출)
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "")
FIREBASE_AUTH_DOMAIN = os.environ.get("FIREBASE_AUTH_DOMAIN", "")
FIREBASE_APP_ID = os.environ.get("FIREBASE_APP_ID", "")
FIREBASE_STORAGE_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET", "")
FIREBASE_MESSAGING_SENDER_ID = os.environ.get("FIREBASE_MESSAGING_SENDER_ID", "")
# Service account JSON 경로 (필수, 백엔드가 토큰 검증할 때 사용)
GOOGLE_APPLICATION_CREDENTIALS = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS", "/etc/streak/firebase-service-account.json"
)

# CORS
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

# 토큰 경제 (모두 무료, 익명 < Google < 추천 보너스)
SIGNUP_BONUS_ANON = 10              # 익명 가입
SIGNUP_BONUS_GOOGLE = 20            # Google 가입 (익명보다 +10)
UPGRADE_BONUS = 10                  # 익명 → Google 업그레이드시 추가
REFERRAL_L1_TOKENS = 20             # 직접 추천 → 추천인에게 +20
REFERRED_BONUS_TOKENS = 20          # 추천 코드로 가입 → 가입자에게 +20
REFERRAL_L2_TOKENS = 10             # 간접 추천 (다단계 2층) → +10

COST_PER_CYCLE = 1                  # 1 토큰 = 1 마켓 참여
