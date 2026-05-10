"""중앙 자동 매매 (모든 사용자 공유) — 페이퍼 트레이딩 v1.

플로우:
  1. 사용자가 /api/trading/start 로 전략 + 활성화
  2. 백그라운드 cycle_runner 가 5분마다:
     - 새 BTC 5분 마켓 생성 (다음 5분 캔들 마감 = 시장 종료)
     - 활성 + 토큰 ≥ COST_PER_CYCLE 인 모든 사용자에게 토큰 차감 후 가상 포지션 오픈
     - 진입가는 전략별 (low=0.10, lead=실제 BTC 가격 vs 마켓 strike 비교 결과 우세 측 시장가)
  3. 마켓 만료 시:
     - 실제 BTC 종가 vs strike → YES/NO 결정
     - 모든 오픈 포지션 청산, P&L 기록
     - Cycle 행 삽입

v2: 실제 Polymarket CLOB 호출 + 자금 수탁
"""
from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, List

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select, func

from . import config
from .db import get_session, engine
from .models import User, TradingConfig, Position, Cycle, TokenTx
from .auth import get_current_user
from .tokens import credit


log = logging.getLogger("trading")
router = APIRouter(prefix="/api/trading", tags=["trading"])


# ──────────────────────────────────────────────────────────────────
# BTC 가격 조회 (다중 소스 — 무료 / 키 불필요)
# ──────────────────────────────────────────────────────────────────
async def fetch_btc_price() -> Optional[float]:
    """Binance → Coinbase 폴백."""
    async with httpx.AsyncClient(timeout=4.0) as cli:
        for url, key in [
            ("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", "price"),
            ("https://api.coinbase.com/v2/prices/BTC-USD/spot", None),
        ]:
            try:
                r = await cli.get(url)
                if r.status_code != 200:
                    continue
                j = r.json()
                if key:
                    return float(j[key])
                return float(j["data"]["amount"])
            except Exception:
                continue
    return None


# ──────────────────────────────────────────────────────────────────
# Market scheduling — 5분 단위
# ──────────────────────────────────────────────────────────────────
def _next_market_close(now: datetime) -> datetime:
    """now 다음의 정각 5분 단위 (UTC). e.g. 13:32 → 13:35."""
    epoch = int(now.replace(tzinfo=timezone.utc).timestamp())
    next_epoch = ((epoch // 300) + 1) * 300
    return datetime.fromtimestamp(next_epoch, tz=timezone.utc)


def _market_id(close_time: datetime) -> str:
    return "btc-5m-" + close_time.strftime("%Y%m%dT%H%M")


def _market_label(close_time: datetime, strike: float) -> str:
    return f"BTC ≥ ${strike:,.0f} @ {close_time.strftime('%H:%M UTC')}"


# ──────────────────────────────────────────────────────────────────
# 전략 진입가
# ──────────────────────────────────────────────────────────────────
def _entry_for(strategy: str, btc_now: float, strike: float) -> tuple[str, float, Optional[float]]:
    """returns (side, entry_price, target_price)

    low: 가격 차이 무관, 균형(0.5)에 가까운 시점에 0.10 매수 노림 (낮은 확률 베팅)
    lead: 현재 우세한 측을 0.7 가정으로 매수, hold-to-expiry
    """
    if strategy == "low":
        # 약자 측을 10센트로 매수 — BTC > strike 아니면 NO, 아니면 YES
        side = "NO" if btc_now >= strike else "YES"
        return (side, 0.10, 0.15)
    # lead
    side = "YES" if btc_now >= strike else "NO"
    return (side, 0.70, None)


def _resolve(side: str, btc_close: float, strike: float, entry: float, size: float
             ) -> tuple[float, str]:
    """포지션 청산 P&L (페이퍼) — Polymarket 결제는 1.0 / 0.0."""
    win = (side == "YES" and btc_close >= strike) or (side == "NO" and btc_close < strike)
    payout = 1.0 if win else 0.0
    pnl = (payout - entry) * size
    reason = "win" if win else "loss"
    return pnl, reason


# ──────────────────────────────────────────────────────────────────
# 백그라운드 사이클 러너
# ──────────────────────────────────────────────────────────────────
_runner_task: Optional[asyncio.Task] = None
_state = {
    "current_market_id": None,
    "current_strike": None,
    "current_close": None,        # datetime
}


async def _open_market(close_time: datetime, btc_now: float):
    """마켓 오픈 — 모든 활성 사용자에게 가상 포지션 생성."""
    strike = round(btc_now)               # 가장 가까운 정수 USD = strike
    market_id = _market_id(close_time)
    label = _market_label(close_time, strike)
    _state["current_market_id"] = market_id
    _state["current_strike"] = strike
    _state["current_close"] = close_time

    opened = 0
    with Session(engine) as session:
        cfgs = session.exec(
            select(TradingConfig).where(TradingConfig.active == True)
        ).all()
        for cfg in cfgs:
            user = session.get(User, cfg.user_id)
            if not user or user.tokens < config.COST_PER_CYCLE:
                continue
            if cfg.max_cycles and cfg.cycles_consumed >= cfg.max_cycles:
                continue
            # 이 마켓 이미 진입했나 (idempotent)
            existing = session.exec(
                select(Position).where(
                    Position.user_id == user.id,
                    Position.market_id == market_id,
                )
            ).first()
            if existing:
                continue
            try:
                credit(session, user, -config.COST_PER_CYCLE, "cycle",
                       ref_id=market_id, note=label)
            except Exception as e:
                log.warning("token consume failed for user=%s: %s", user.id, e)
                continue
            side, entry, target = _entry_for(cfg.strategy, btc_now, strike)
            size = 100.0 / max(0.01, entry)         # $100 노미널 사이즈
            pos = Position(
                user_id=user.id, market_id=market_id, market_label=label,
                strategy=cfg.strategy, side=side, entry_price=entry,
                size=size, target_price=target,
            )
            session.add(pos)
            cfg.cycles_consumed += 1
            cfg.updated_at = datetime.utcnow()
            session.add(cfg)
            session.commit()
            opened += 1
    log.info("market=%s strike=%s opened=%d positions", market_id, strike, opened)


async def _close_market(market_id: str, strike: float, btc_close: float):
    """마켓 만료 — 모든 오픈 포지션 청산."""
    closed = 0
    with Session(engine) as session:
        positions = session.exec(
            select(Position).where(
                Position.market_id == market_id,
                Position.status == "open",
            )
        ).all()
        for p in positions:
            pnl, reason = _resolve(p.side, btc_close, strike, p.entry_price, p.size)
            p.status = "closed"
            p.exit_price = 1.0 if reason == "win" else 0.0
            p.pnl = pnl
            p.exit_reason = reason
            p.closed_at = datetime.utcnow()
            session.add(p)
            session.add(Cycle(
                user_id=p.user_id, cycle_id=f"{market_id}-{p.id}",
                market_slug=p.market_label, side=p.side,
                entry_price=p.entry_price, exit_price=p.exit_price,
                shares=p.size, pnl=pnl, exit_reason=reason,
            ))
            closed += 1
        session.commit()
    log.info("market=%s closed=%d positions btc_close=%s", market_id, closed, btc_close)


async def cycle_runner():
    """주 루프 — 5분마다 마켓 오픈/청산 사이클."""
    log.info("cycle_runner started")
    last_opened: Optional[str] = None
    last_closed: Optional[str] = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            close_time = _next_market_close(now)
            secs_to_close = (close_time - now).total_seconds()

            # 새 마켓 오픈 (해당 5분 슬롯 시작 직후)
            mid = _market_id(close_time)
            if mid != last_opened and secs_to_close > 60:
                btc = await fetch_btc_price()
                if btc:
                    await _open_market(close_time, btc)
                    last_opened = mid

            # 만료 청산 (close_time 1초 후)
            if _state["current_market_id"] and _state["current_close"]:
                if (now - _state["current_close"]).total_seconds() >= 1 and \
                   _state["current_market_id"] != last_closed:
                    btc = await fetch_btc_price()
                    if btc:
                        await _close_market(
                            _state["current_market_id"],
                            _state["current_strike"],
                            btc,
                        )
                        last_closed = _state["current_market_id"]
                        _state["current_market_id"] = None

        except Exception as e:
            log.exception("cycle_runner error: %s", e)
        await asyncio.sleep(15)


def start_runner():
    global _runner_task
    if _runner_task is None or _runner_task.done():
        loop = asyncio.get_event_loop()
        _runner_task = loop.create_task(cycle_runner())
        log.info("runner task scheduled")


# ──────────────────────────────────────────────────────────────────
# API
# ──────────────────────────────────────────────────────────────────
class ConfigResp(BaseModel):
    active: bool
    strategy: str
    cycles_consumed: int
    max_cycles: int


class StartReq(BaseModel):
    strategy: str = "lead"            # low / lead
    max_cycles: int = 0


def _get_or_create_cfg(session: Session, user: User) -> TradingConfig:
    cfg = session.exec(
        select(TradingConfig).where(TradingConfig.user_id == user.id)
    ).first()
    if not cfg:
        cfg = TradingConfig(user_id=user.id)
        session.add(cfg); session.commit(); session.refresh(cfg)
    return cfg


@router.get("/config", response_model=ConfigResp)
def get_config(user: User = Depends(get_current_user),
               session: Session = Depends(get_session)):
    cfg = _get_or_create_cfg(session, user)
    return ConfigResp(active=cfg.active, strategy=cfg.strategy,
                      cycles_consumed=cfg.cycles_consumed, max_cycles=cfg.max_cycles)


@router.post("/start", response_model=ConfigResp)
def start_trading(req: StartReq,
                  user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)):
    if req.strategy not in ("low", "lead"):
        raise HTTPException(400, "strategy must be 'low' or 'lead'")
    if user.tokens < config.COST_PER_CYCLE:
        raise HTTPException(402, "insufficient tokens")
    cfg = _get_or_create_cfg(session, user)
    cfg.strategy = req.strategy
    cfg.max_cycles = max(0, req.max_cycles)
    cfg.active = True
    cfg.updated_at = datetime.utcnow()
    session.add(cfg); session.commit(); session.refresh(cfg)
    return ConfigResp(active=True, strategy=cfg.strategy,
                      cycles_consumed=cfg.cycles_consumed, max_cycles=cfg.max_cycles)


@router.post("/stop", response_model=ConfigResp)
def stop_trading(user: User = Depends(get_current_user),
                 session: Session = Depends(get_session)):
    cfg = _get_or_create_cfg(session, user)
    cfg.active = False
    cfg.updated_at = datetime.utcnow()
    session.add(cfg); session.commit(); session.refresh(cfg)
    return ConfigResp(active=False, strategy=cfg.strategy,
                      cycles_consumed=cfg.cycles_consumed, max_cycles=cfg.max_cycles)


class PositionResp(BaseModel):
    id: int
    market_id: str
    market_label: str
    strategy: str
    side: str
    entry_price: float
    size: float
    status: str
    exit_price: Optional[float] = None
    pnl: float
    exit_reason: Optional[str] = None
    opened_at: datetime
    closed_at: Optional[datetime] = None


@router.get("/positions", response_model=List[PositionResp])
def open_positions(user: User = Depends(get_current_user),
                   session: Session = Depends(get_session)):
    rows = session.exec(
        select(Position).where(
            Position.user_id == user.id,
            Position.status == "open",
        ).order_by(Position.opened_at.desc())
    ).all()
    return [PositionResp(**r.dict()) for r in rows]


@router.get("/history", response_model=List[PositionResp])
def history(limit: int = 30,
            user: User = Depends(get_current_user),
            session: Session = Depends(get_session)):
    rows = session.exec(
        select(Position).where(
            Position.user_id == user.id,
            Position.status == "closed",
        ).order_by(Position.closed_at.desc()).limit(limit)
    ).all()
    return [PositionResp(**r.dict()) for r in rows]


class StatsResp(BaseModel):
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    open_count: int
    market_state: dict


@router.get("/stats", response_model=StatsResp)
def stats(user: User = Depends(get_current_user),
          session: Session = Depends(get_session)):
    closed = session.exec(
        select(Position).where(
            Position.user_id == user.id,
            Position.status == "closed",
        )
    ).all()
    wins = sum(1 for p in closed if p.exit_reason == "win")
    losses = len(closed) - wins
    total_pnl = sum(p.pnl for p in closed)
    open_count = session.exec(
        select(func.count()).select_from(Position).where(
            Position.user_id == user.id,
            Position.status == "open",
        )
    ).one() or 0
    return StatsResp(
        total_trades=len(closed),
        wins=wins, losses=losses,
        win_rate=(wins / len(closed) if closed else 0.0),
        total_pnl=round(total_pnl, 2),
        open_count=open_count,
        market_state={
            "current_market_id": _state.get("current_market_id"),
            "current_strike": _state.get("current_strike"),
            "current_close": _state["current_close"].isoformat() if _state.get("current_close") else None,
        },
    )
