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

# Deposit address — USDC (PoS) on Polygon, all incoming = topup
DEPOSIT_ADDRESS = os.environ.get(
    "DEPOSIT_ADDRESS", "0xCf9907F84Fe2B9e2976766f466EFE37bCb738CA0"
).lower()

# USDC contract on Polygon (PoS USDC.e)
USDC_CONTRACT = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"

# Polygon RPC for deposit polling (use multiple for fallback)
POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.rpc.subquery.network/public",
]

# Token economy
TOKEN_PER_CENT = 1     # 1 token = 1 cent (= $0.01)
SIGNUP_BONUS_TOKENS = 10
REFERRAL_L1_TOKENS = 10   # 직접 추천 (Level 1) = 10 tokens
REFERRAL_L2_TOKENS = 5    # 추천한 사람이 또 추천 (Level 2) = 5 tokens

# Cycle cost — 1 cycle = 1 token
COST_PER_CYCLE = 1

# Web3 — chain ID
CHAIN_ID = 137  # Polygon

# SIWE
SIWE_DOMAIN = os.environ.get("SIWE_DOMAIN", "localhost:8000")
SIWE_URI = os.environ.get("SIWE_URI", "http://localhost:8000")
NONCE_TTL_SEC = 300  # 5 min
