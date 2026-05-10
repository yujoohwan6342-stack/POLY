"""환경 설정 (env vars + defaults)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# Database
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR}/streak.db")

# JWT
JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-in-production-please-set-env")
JWT_ALG = "HS256"
JWT_TTL_HOURS = 24 * 7  # 7 days

# CORS
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

# 사이클 경제 (USDC 결제 제거 — 무료 + 추천 + 광고 기반)
SIGNUP_BONUS_CYCLES = 10        # 가입 즉시 부여
REFERRAL_L1_CYCLES = 10         # 직접 추천한 사람 가입시 → 추천인에게
REFERRED_BONUS_CYCLES = 10      # 추천 코드 통해 가입한 사람 → 추천받은 사람에게 추가
REFERRAL_L2_CYCLES = 5          # 추천한 사람이 또 추천하면 (간접) → 원 추천인에게

COST_PER_CYCLE = 1              # 1 사이클 = 1 토큰

# Web3 — chain ID
CHAIN_ID = 137  # Polygon

# SIWE
SIWE_DOMAIN = os.environ.get("SIWE_DOMAIN", "localhost:8000")
SIWE_URI = os.environ.get("SIWE_URI", "http://localhost:8000")
NONCE_TTL_SEC = 300  # 5 min
