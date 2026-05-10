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

# Polygon RPC for deposit polling (use multiple for fallback)
POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.rpc.subquery.network/public",
]

# 받을 수 있는 토큰들 (모두 Polygon 기준)
# 각 토큰은 ERC20 Transfer 이벤트로 감지 + Chainlink 오라클로 USD 환산
ACCEPTED_TOKENS = [
    {
        "symbol": "USDC",
        "contract": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # native USDC
        "decimals": 6,
        "stable": True,    # 항상 $1 (Chainlink 호출 안 함)
    },
    {
        "symbol": "USDC.e",
        "contract": "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  # bridged USDC
        "decimals": 6,
        "stable": True,
    },
    {
        "symbol": "USDT",
        "contract": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
        "decimals": 6,
        "stable": True,
    },
    {
        "symbol": "WETH",
        "contract": "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619",
        "decimals": 18,
        "stable": False,
        "chainlink_aggregator": "0xF9680D99D6C9589e2a93a78A04A279e509205945",  # ETH/USD
        "aggregator_decimals": 8,
    },
    {
        "symbol": "WBTC",
        "contract": "0x1bfd67037b42cf73acf2047067bd4f2c47d9bfd6",
        "decimals": 8,
        "stable": False,
        "chainlink_aggregator": "0xc907E116054Ad103354f2D350FD2514433D57F6f",  # BTC/USD
        "aggregator_decimals": 8,
    },
    {
        "symbol": "MATIC",
        "contract": "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",  # WMATIC
        "decimals": 18,
        "stable": False,
        "chainlink_aggregator": "0xAB594600376Ec9fD91F8e885dADF0CE036862dE0",  # MATIC/USD
        "aggregator_decimals": 8,
    },
]

# 하위 호환 (옛 코드용)
USDC_CONTRACT = ACCEPTED_TOKENS[1]["contract"]

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
