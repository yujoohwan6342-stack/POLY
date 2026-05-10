"""STREAK Backend — FastAPI entrypoint.

실행:
  uvicorn backend.main:app --host 0.0.0.0 --port 8000

또는 직접:
  python -m backend.main
"""
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from . import config, auth, tokens, referrals
from .db import init_db
from .deposits_worker import deposit_poller


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("streak")


# Rate limiter (per-IP)
limiter = Limiter(key_func=get_remote_address, default_limits=["100/minute"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """startup/shutdown."""
    init_db()
    log.info("DB initialized")

    # 입금 폴링 워커 백그라운드 시작
    task = asyncio.create_task(deposit_poller())
    log.info("deposit poller started (USDC → %s)", config.DEPOSIT_ADDRESS)

    yield

    task.cancel()
    log.info("shutting down")


app = FastAPI(
    title="STREAK API",
    version="0.2.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(tokens.router)
app.include_router(referrals.router)


@app.get("/api/health")
def health():
    return {"ok": True, "version": "0.2.0"}


@app.get("/api/config")
def public_config():
    """Frontend가 필요로 하는 공개 설정."""
    return {
        "deposit_address": config.DEPOSIT_ADDRESS,
        "chain_id": config.CHAIN_ID,
        "signup_bonus": config.SIGNUP_BONUS_TOKENS,
        "ref_l1": config.REFERRAL_L1_TOKENS,
        "ref_l2": config.REFERRAL_L2_TOKENS,
        "cost_per_cycle": config.COST_PER_CYCLE,
        "token_per_cent": config.TOKEN_PER_CENT,
        "accepted_tokens": [
            {
                "symbol": t["symbol"],
                "contract": t["contract"],
                "decimals": t["decimals"],
                "stable": t.get("stable", False),
            }
            for t in config.ACCEPTED_TOKENS
        ],
    }


# ─── Static frontend (SPA) ──────────────────────────────────────
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def root():
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/{path:path}")
    def spa(path: str):
        # SPA fallback
        f = FRONTEND_DIR / path
        if f.is_file():
            return FileResponse(f)
        return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
