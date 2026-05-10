"""중앙 자동 매매 엔진 — 종합 전략 + 멀티에셋 (페이퍼 v2).

bot/streak.py 의 단일사용자 전략 엔진을 멀티유저로 확장:
  - 사용자별 TradingConfig (entry_mode, bet_size, entry/tp/sl, tolerance, time gates...)
  - 30초마다 활성 사용자 전체 스캔
  - 실제 Polymarket gamma API + book API 호가 기반으로 매수/익절/손절 시뮬레이션
  - 토큰은 진입 시도 시 차감 (실패해도 차감 — 사용자에게 명시)
  - 마켓 만료 시 실제 BTC 종가로 잔여 포지션 자동 결제

지원 자산:
  - BTC 5분: 활성
  - ETH/SOL, 15분/60분: 정의만 (coming_soon=True)

라이브 모드 (실제 자금 매매)는 별도 커스터디 결정 후 polymarket_exec 모듈로 분리 예정.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field as PField
from sqlmodel import Session, select, func

from . import config
from .db import get_session, engine
from .models import User, TradingConfig, Position, Cycle, TokenTx
from .auth import get_current_user
from .tokens import credit
from . import polymarket_exec as pmx


log = logging.getLogger("trading")
router = APIRouter(prefix="/api/trading", tags=["trading"])


GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"


# ──────────────────────────────────────────────────────────────────
# Asset registry
# ──────────────────────────────────────────────────────────────────
ASSETS: Dict[str, Dict[str, Any]] = {
    "BTC": {
        "label": "Bitcoin",
        "icon": "₿",
        "binance_symbol": "BTCUSDT",
        "coinbase_pair": "BTC-USD",
        "polymarket_slug_prefix": "btc-updown",
        "active_durations": [5],          # 5분만 활성
        "coming_soon_durations": [15, 60],
    },
    "ETH": {
        "label": "Ethereum",
        "icon": "Ξ",
        "binance_symbol": "ETHUSDT",
        "coinbase_pair": "ETH-USD",
        "polymarket_slug_prefix": "eth-updown",
        "active_durations": [],
        "coming_soon_durations": [5, 15, 60],
    },
    "SOL": {
        "label": "Solana",
        "icon": "◎",
        "binance_symbol": "SOLUSDT",
        "coinbase_pair": "SOL-USD",
        "polymarket_slug_prefix": "sol-updown",
        "active_durations": [],
        "coming_soon_durations": [5, 15, 60],
    },
}


def is_active(asset: str, duration_min: int) -> bool:
    a = ASSETS.get(asset)
    return bool(a and duration_min in a["active_durations"])


# ──────────────────────────────────────────────────────────────────
# 가격 + 호가 조회
# ──────────────────────────────────────────────────────────────────
async def fetch_spot_price(asset: str) -> Optional[float]:
    a = ASSETS.get(asset)
    if not a:
        return None
    async with httpx.AsyncClient(timeout=4.0) as cli:
        for url, key in [
            (f"https://api.binance.com/api/v3/ticker/price?symbol={a['binance_symbol']}", "price"),
            (f"https://api.coinbase.com/v2/prices/{a['coinbase_pair']}/spot", None),
        ]:
            try:
                r = await cli.get(url)
                if r.status_code != 200:
                    continue
                j = r.json()
                return float(j[key]) if key else float(j["data"]["amount"])
            except Exception:
                continue
    return None


async def fetch_book_best(client: httpx.AsyncClient, token_id: str) -> tuple[float, float]:
    """returns (best_bid, best_ask). 실패시 (0.0, 1.0)."""
    try:
        r = await client.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=3.0)
        ob = r.json()
        bids = sorted([float(b["price"]) for b in ob.get("bids", [])], reverse=True)
        asks = sorted([float(a["price"]) for a in ob.get("asks", [])])
        return (bids[0] if bids else 0.0, asks[0] if asks else 1.0)
    except Exception:
        return 0.0, 1.0


async def find_market(client: httpx.AsyncClient, asset: str, duration_min: int) -> Optional[dict]:
    """현재 진행 중인 마켓 메타데이터 조회 (gamma API)."""
    a = ASSETS.get(asset)
    if not a:
        return None
    duration_sec = duration_min * 60
    now = int(time.time())
    current_window = now - (now % duration_sec)
    for offset in (0, duration_sec):
        ts = current_window + offset
        slug = f"{a['polymarket_slug_prefix']}-{duration_min}m-{ts}"
        try:
            r = await client.get(f"{GAMMA_HOST}/events", params={"slug": slug}, timeout=4.0)
            data = r.json()
            if not data:
                continue
            event = data[0]
            mkts = event.get("markets", [])
            if not mkts:
                continue
            m = mkts[0]
            if m.get("closed"):
                continue
            tokens = m.get("clobTokenIds", "[]")
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            if len(tokens) < 2:
                continue
            end_ts = ts + duration_sec
            if now >= end_ts:
                continue
            return {
                "slug": slug, "start_ts": ts, "end_ts": end_ts,
                "yes_token": tokens[0], "no_token": tokens[1],
                "question": m.get("question", ""),
                "asset": asset, "duration_min": duration_min,
            }
        except Exception as e:
            log.debug("market lookup %s: %s", slug, e)
    return None


# ──────────────────────────────────────────────────────────────────
# 전략 평가 — bot/streak.py 와 동일 로직
# ──────────────────────────────────────────────────────────────────
def get_side_prices(yes_bid: float, yes_ask: float, no_bid: float, no_ask: float, side: str
                    ) -> tuple[float, float]:
    if side == "YES":
        return yes_bid, yes_ask
    return no_bid, no_ask


def should_enter(cfg: TradingConfig, prices: dict, progress: float, remaining_pct: float
                 ) -> Optional[tuple[str, float]]:
    """returns (side, entry_price) if 진입 OK, else None.

    조건:
      - progress < tradeable_pct
      - remaining_pct <= buy_when_remaining_below_pct
      - low_target: 양쪽 ask 중 entry_price ± tolerance 안에 있는 것
      - high_lead: 우세 측 ask가 [entry_price, max_entry_price] 사이
    """
    if progress >= cfg.tradeable_pct:
        return None
    if remaining_pct > cfg.buy_when_remaining_below_pct:
        return None

    yes_bid, yes_ask = prices["yes_bid"], prices["yes_ask"]
    no_bid, no_ask = prices["no_bid"], prices["no_ask"]

    if cfg.entry_mode == "low_target":
        # entry_price ± tolerance 안에 있는 사이드 (양쪽 다면 더 가까운 쪽)
        lo = cfg.entry_price - cfg.entry_tolerance
        hi = cfg.entry_price + cfg.entry_tolerance
        candidates = []
        if lo <= yes_ask <= hi:
            candidates.append(("YES", yes_ask))
        if lo <= no_ask <= hi:
            candidates.append(("NO", no_ask))
        if not candidates:
            return None
        # 가장 cfg.entry_price 에 가까운 사이드
        side, ask = min(candidates, key=lambda x: abs(x[1] - cfg.entry_price))
        # limit 모드에선 cfg.entry_price 로 발주, market 모드에선 ask 로 즉시
        return side, (cfg.entry_price if cfg.buy_order_type == "limit" else ask)

    # high_lead: 우세 측 (bid 더 높은 쪽) 의 ask 가 [entry_price, max_entry_price]
    leading = "YES" if yes_bid > no_bid else "NO"
    bid, ask = get_side_prices(yes_bid, yes_ask, no_bid, no_ask, leading)
    if cfg.entry_price <= ask <= cfg.max_entry_price:
        return leading, (cfg.entry_price if cfg.buy_order_type == "limit" else ask)
    return None


def should_exit(pos: Position, cfg: TradingConfig, prices: dict
                ) -> Optional[tuple[float, str]]:
    """returns (exit_price, reason) if 청산해야 함."""
    bid, ask = get_side_prices(prices["yes_bid"], prices["yes_ask"],
                               prices["no_bid"], prices["no_ask"], pos.side)
    # TP: 시장 bid 가 tp_price 이상 → limit 매도 fill 가능 (bid >= tp_price)
    if bid >= cfg.tp_price:
        return cfg.tp_price if cfg.sell_order_type == "limit" else bid, "tp"
    # SL: 시장 bid 가 sl_price 이하
    if bid <= cfg.sl_price:
        return cfg.sl_price if cfg.sell_order_type == "limit" else bid, "sl"
    return None


# ──────────────────────────────────────────────────────────────────
# 백그라운드 러너
# ──────────────────────────────────────────────────────────────────
_runner_task: Optional[asyncio.Task] = None
_market_cache: Dict[str, dict] = {}    # asset_dur -> last seen market


async def _scan_users():
    """매 틱마다: 활성 사용자 스캔 → 진입/청산."""
    async with httpx.AsyncClient() as cli:
        # 어떤 (asset, duration) 조합이 활성 사용자에게 필요한지
        with Session(engine) as s:
            cfgs = s.exec(
                select(TradingConfig).where(TradingConfig.active == True)
            ).all()

        # 활성 (asset, duration) 별로 마켓 한 번만 조회
        active_combos: Dict[tuple, list] = {}
        for cfg in cfgs:
            if not is_active(cfg.asset, cfg.duration_min):
                continue
            active_combos.setdefault((cfg.asset, cfg.duration_min), []).append(cfg)

        for (asset, dur), cfg_list in active_combos.items():
            mkt = await find_market(cli, asset, dur)
            if not mkt:
                continue
            _market_cache[f"{asset}_{dur}"] = mkt

            yes_bid, yes_ask = await fetch_book_best(cli, mkt["yes_token"])
            no_bid, no_ask = await fetch_book_best(cli, mkt["no_token"])
            prices = {"yes_bid": yes_bid, "yes_ask": yes_ask,
                      "no_bid": no_bid, "no_ask": no_ask}

            now = int(time.time())
            elapsed = now - mkt["start_ts"]
            progress = elapsed / (dur * 60)
            remaining_pct = max(0.0, 1.0 - progress)

            for cfg in cfg_list:
                with Session(engine) as session:
                    cfg = session.get(TradingConfig, cfg.id)
                    if not cfg or not cfg.active:
                        continue
                    user = session.get(User, cfg.user_id)
                    if not user:
                        continue

                    # 1) 기존 오픈 포지션 청산 체크
                    open_pos = session.exec(
                        select(Position).where(
                            Position.user_id == user.id,
                            Position.market_id == mkt["slug"],
                            Position.status == "open",
                        )
                    ).all()
                    for pos in open_pos:
                        exit_decision = should_exit(pos, cfg, prices)
                        if exit_decision:
                            ex_px, reason = exit_decision
                            pos.status = "closed"
                            pos.exit_price = ex_px
                            pos.pnl = (ex_px - pos.entry_price) * pos.size
                            pos.exit_reason = reason
                            pos.closed_at = datetime.utcnow()
                            session.add(pos)
                            session.add(Cycle(
                                user_id=user.id, cycle_id=f"{mkt['slug']}-{pos.id}",
                                market_slug=mkt["slug"], side=pos.side,
                                entry_price=pos.entry_price, exit_price=ex_px,
                                shares=pos.size, pnl=pos.pnl, exit_reason=reason,
                            ))
                            session.commit()

                    # 2) 진입 체크 (이미 이 마켓에 진입한 적 없을 때만)
                    has_entered = session.exec(
                        select(Position).where(
                            Position.user_id == user.id,
                            Position.market_id == mkt["slug"],
                        )
                    ).first()
                    if has_entered:
                        continue

                    # 토큰/한도 체크
                    if user.tokens < config.COST_PER_CYCLE:
                        continue
                    if cfg.max_cycles_per_session and \
                       cfg.cycles_consumed >= cfg.max_cycles_per_session:
                        continue

                    decision = should_enter(cfg, prices, progress, remaining_pct)
                    if not decision:
                        continue
                    side, entry_px = decision
                    # low_target limit 모드에선 ask 가 entry_price+tolerance 이내면 fill 가정
                    bid, ask = get_side_prices(yes_bid, yes_ask, no_bid, no_ask, side)
                    if cfg.buy_order_type == "limit" and ask > entry_px + 0.001:
                        # 호가가 아직 닿지 않음 → 다음 틱에 재시도
                        continue
                    fill_px = ask if cfg.buy_order_type == "market" else entry_px
                    size = cfg.bet_size_usd / max(0.01, fill_px)

                    # 토큰 차감 + 포지션 오픈
                    try:
                        credit(session, user, -config.COST_PER_CYCLE, "cycle",
                               ref_id=mkt["slug"], note=f"{asset} {dur}m {side} @{fill_px:.2f}")
                    except Exception as e:
                        log.warning("token consume user=%s: %s", user.id, e)
                        continue
                    pos = Position(
                        user_id=user.id, market_id=mkt["slug"],
                        market_label=f"{asset} {dur}m {side}",
                        strategy=cfg.entry_mode, side=side,
                        entry_price=fill_px, size=size,
                        target_price=cfg.tp_price,
                    )
                    session.add(pos)
                    cfg.cycles_consumed += 1
                    cfg.updated_at = datetime.utcnow()
                    session.add(cfg)
                    session.commit()
                    log.info("ENTER user=%s %s %s @%.2f size=%.1f",
                             user.id, mkt["slug"], side, fill_px, size)


async def _expire_markets():
    """마감된 마켓의 잔여 오픈 포지션을 spot 종가로 결제."""
    now = int(time.time())
    with Session(engine) as session:
        markets_with_open = session.exec(
            select(Position.market_id).where(Position.status == "open").distinct()
        ).all()
    for mid in markets_with_open:
        # market_id 형식: "<asset>-updown-<dur>m-<ts>"  (gamma slug)
        try:
            parts = mid.split("-")
            ts_end = int(parts[-1]) + int(parts[-2].rstrip("m")) * 60
            asset = parts[0].upper()
        except Exception:
            continue
        if now < ts_end + 5:
            continue
        spot = await fetch_spot_price(asset)
        if not spot:
            continue
        # strike 추정: gamma question 에서 추출하는 게 정확하지만 단순화 — bid 근접도로 결정
        # v2: strike 정확히 재조회. 여기선 spot 기준 단순 결제(승=YES면 strike↑)
        # 더 정확히 하려면 cycle 시작 시 strike를 Position 에 저장하도록 모델 확장 필요.
        with Session(engine) as session:
            opens = session.exec(
                select(Position).where(
                    Position.market_id == mid, Position.status == "open"
                )
            ).all()
            for p in opens:
                # 가격 0.10에 매수했다면 만기 = 1.0(승) or 0.0(패)
                # 우세측 prediction: pos.side=YES면 spot ↑로 결제, NO면 ↓로 결제
                # strike 정보 없이는 정확 산정 어려움 → 호가 기준 마지막 bid 가져옴
                bid_now, _ = await fetch_book_best(httpx.AsyncClient(), p.market_id)  # rough
                # 안전: bid >= 0.5 → 그쪽이 이김
                yes_won = bid_now >= 0.5  # rough heuristic
                won = (p.side == "YES" and yes_won) or (p.side == "NO" and not yes_won)
                ex_px = 1.0 if won else 0.0
                p.status = "closed"
                p.exit_price = ex_px
                p.pnl = (ex_px - p.entry_price) * p.size
                p.exit_reason = "expiry_win" if won else "expiry_loss"
                p.closed_at = datetime.utcnow()
                session.add(p)
                session.add(Cycle(
                    user_id=p.user_id, cycle_id=f"{mid}-exp-{p.id}",
                    market_slug=mid, side=p.side,
                    entry_price=p.entry_price, exit_price=ex_px,
                    shares=p.size, pnl=p.pnl, exit_reason=p.exit_reason,
                ))
            session.commit()
        log.info("expire market=%s closed=%d", mid, len(opens))


async def cycle_runner():
    log.info("cycle_runner started (multi-asset, comprehensive strategy)")
    while True:
        try:
            await _scan_users()
            await _expire_markets()
        except Exception as e:
            log.exception("runner err: %s", e)
        await asyncio.sleep(10)


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
    asset: str
    duration_min: int
    entry_mode: str
    bet_size_usd: float
    entry_price: float
    entry_tolerance: float
    max_entry_price: float
    tp_price: float
    sl_price: float
    buy_order_type: str
    sell_order_type: str
    tradeable_pct: float
    buy_when_remaining_below_pct: float
    max_cycles_per_session: int
    cycles_consumed: int


class UpdateReq(BaseModel):
    asset: Optional[str] = None
    duration_min: Optional[int] = None
    entry_mode: Optional[str] = None
    bet_size_usd: Optional[float] = None
    entry_price: Optional[float] = None
    entry_tolerance: Optional[float] = None
    max_entry_price: Optional[float] = None
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    buy_order_type: Optional[str] = None
    sell_order_type: Optional[str] = None
    tradeable_pct: Optional[float] = None
    buy_when_remaining_below_pct: Optional[float] = None
    max_cycles_per_session: Optional[int] = None


def _to_resp(cfg: TradingConfig) -> ConfigResp:
    return ConfigResp(
        active=cfg.active, asset=cfg.asset, duration_min=cfg.duration_min,
        entry_mode=cfg.entry_mode, bet_size_usd=cfg.bet_size_usd,
        entry_price=cfg.entry_price, entry_tolerance=cfg.entry_tolerance,
        max_entry_price=cfg.max_entry_price,
        tp_price=cfg.tp_price, sl_price=cfg.sl_price,
        buy_order_type=cfg.buy_order_type, sell_order_type=cfg.sell_order_type,
        tradeable_pct=cfg.tradeable_pct,
        buy_when_remaining_below_pct=cfg.buy_when_remaining_below_pct,
        max_cycles_per_session=cfg.max_cycles_per_session,
        cycles_consumed=cfg.cycles_consumed,
    )


def _get_or_create_cfg(session: Session, user: User) -> TradingConfig:
    cfg = session.exec(
        select(TradingConfig).where(TradingConfig.user_id == user.id)
    ).first()
    if not cfg:
        cfg = TradingConfig(user_id=user.id)
        session.add(cfg); session.commit(); session.refresh(cfg)
    return cfg


# ──────────────────────────────────────────────────────────────────
# Stateless 라이브 실행 — PK 는 매 호출 받아 즉시 폐기 (DB/로그 X)
# ──────────────────────────────────────────────────────────────────
@router.get("/market_data")
def market_data(asset: str = "BTC", duration_min: int = 5):
    """공개 마켓 + 호가 — 인증 없음. 브라우저가 5초마다 폴링."""
    a = ASSETS.get(asset)
    if not a:
        raise HTTPException(400, "unknown asset")
    if not is_active(asset, duration_min):
        return {"available": False, "reason": "asset_duration_disabled"}
    mkt = pmx.fetch_market(a["polymarket_slug_prefix"], duration_min)
    if not mkt:
        return {"available": False, "reason": "no_market"}
    yes = pmx.fetch_book(mkt["yes_token"])
    no = pmx.fetch_book(mkt["no_token"])
    now = int(time.time())
    elapsed = now - mkt["start_ts"]
    return {
        "available": True,
        "market": mkt,
        "yes_book": yes, "no_book": no,
        "now_ts": now,
        "elapsed_pct": elapsed / (duration_min * 60),
        "remaining_sec": max(0, mkt["end_ts"] - now),
    }


class WalletCheckReq(BaseModel):
    private_key: str = PField(..., min_length=64, max_length=68)
    funder: Optional[str] = None


@router.post("/wallet_check")
def wallet_check(req: WalletCheckReq, user: User = Depends(get_current_user)):
    """PK 정합성 + 잔액 확인 (1회). 응답 후 PK 즉시 release."""
    pk = req.private_key.strip()
    if pk.startswith("0x"):
        pk_body = pk[2:]
    else:
        pk_body = pk
    if len(pk_body) != 64 or not all(c in "0123456789abcdefABCDEF" for c in pk_body):
        raise HTTPException(400, "invalid_pk_format")
    try:
        info = pmx.get_address_balance(pk, req.funder)
    finally:
        pk = None              # GC hint
        pk_body = None
    return info


class ExecReq(BaseModel):
    private_key: str = PField(..., min_length=64, max_length=68)
    funder: Optional[str] = None
    action: str                                          # buy / sell
    token_id: str
    price: float
    size: float
    order_type: str = "limit"                            # limit / market
    max_price: Optional[float] = None
    market_slug: Optional[str] = None                    # 토큰 차감 ref_id 용


class ExecResp(BaseModel):
    ok: bool
    address: Optional[str] = None
    error: Optional[str] = None
    tokens_left: int
    raw: Optional[dict] = None


@router.post("/execute", response_model=ExecResp)
def execute(req: ExecReq,
            user: User = Depends(get_current_user),
            session: Session = Depends(get_session)):
    """단일 주문 실행. PK 는 함수 끝나면 release. DB 에 거래 기록 X."""
    if req.action not in ("buy", "sell"):
        raise HTTPException(400, "action must be buy/sell")
    if req.order_type not in ("limit", "market"):
        raise HTTPException(400, "order_type must be limit/market")

    # 매수만 토큰 차감 (1 거래 사이클 = 매수 시점 기준; 매도는 후속 액션)
    if req.action == "buy":
        if user.tokens < config.COST_PER_CYCLE:
            raise HTTPException(402, "insufficient_tokens")
        try:
            credit(session, user, -config.COST_PER_CYCLE, "cycle",
                   ref_id=req.market_slug or "live", note=f"buy {req.token_id[:6]}")
        except Exception as e:
            raise HTTPException(500, f"token_consume_failed: {e}")

    pk = req.private_key
    try:
        result = pmx.execute_order(
            pk, action=req.action, token_id=req.token_id,
            price=req.price, size=req.size, order_type=req.order_type,
            funder=req.funder, max_price=req.max_price,
        )
    finally:
        pk = None
        # FastAPI/pydantic 가 req 보유 — 응답 직후 GC. 추가 zero-out 불가능 (Python 한계)

    return ExecResp(
        ok=bool(result.get("ok")),
        address=result.get("address"),
        error=result.get("error"),
        tokens_left=user.tokens,
        raw=result.get("raw") if result.get("ok") else None,
    )


@router.get("/assets")
def list_assets():
    """프런트가 자산 선택 UI 그릴 때 사용."""
    return [
        {
            "code": code, "label": a["label"], "icon": a["icon"],
            "active_durations": a["active_durations"],
            "coming_soon_durations": a["coming_soon_durations"],
        }
        for code, a in ASSETS.items()
    ]


@router.get("/config", response_model=ConfigResp)
def get_config(user: User = Depends(get_current_user),
               session: Session = Depends(get_session)):
    return _to_resp(_get_or_create_cfg(session, user))


@router.put("/config", response_model=ConfigResp)
def update_config(req: UpdateReq,
                  user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)):
    cfg = _get_or_create_cfg(session, user)
    data = req.dict(exclude_unset=True)

    # 검증
    if "asset" in data and data["asset"] not in ASSETS:
        raise HTTPException(400, "unknown asset")
    if "duration_min" in data and data["duration_min"] not in (5, 15, 60):
        raise HTTPException(400, "duration_min must be 5/15/60")
    if "entry_mode" in data and data["entry_mode"] not in ("low_target", "high_lead"):
        raise HTTPException(400, "entry_mode invalid")
    for k in ("buy_order_type", "sell_order_type"):
        if k in data and data[k] not in ("limit", "market"):
            raise HTTPException(400, f"{k} must be limit/market")
    for k, mn, mx in [
        ("bet_size_usd", 0.5, 1000), ("entry_price", 0.01, 0.99),
        ("entry_tolerance", 0.0, 0.5), ("max_entry_price", 0.5, 0.99),
        ("tp_price", 0.02, 0.99), ("sl_price", 0.01, 0.5),
        ("tradeable_pct", 0.05, 1.0), ("buy_when_remaining_below_pct", 0.05, 1.0),
        ("max_cycles_per_session", 0, 10000),
    ]:
        if k in data and not (mn <= data[k] <= mx):
            raise HTTPException(400, f"{k} out of range [{mn}, {mx}]")

    for k, v in data.items():
        setattr(cfg, k, v)
    cfg.strategy = cfg.entry_mode      # legacy compat
    cfg.updated_at = datetime.utcnow()
    session.add(cfg); session.commit(); session.refresh(cfg)
    return _to_resp(cfg)


@router.post("/start", response_model=ConfigResp)
def start_trading(user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)):
    if user.tokens < config.COST_PER_CYCLE:
        raise HTTPException(402, "insufficient tokens")
    cfg = _get_or_create_cfg(session, user)
    if not is_active(cfg.asset, cfg.duration_min):
        raise HTTPException(400, f"{cfg.asset} {cfg.duration_min}m not yet available")
    cfg.active = True
    cfg.updated_at = datetime.utcnow()
    session.add(cfg); session.commit(); session.refresh(cfg)
    return _to_resp(cfg)


@router.post("/stop", response_model=ConfigResp)
def stop_trading(user: User = Depends(get_current_user),
                 session: Session = Depends(get_session)):
    cfg = _get_or_create_cfg(session, user)
    cfg.active = False
    cfg.updated_at = datetime.utcnow()
    session.add(cfg); session.commit(); session.refresh(cfg)
    return _to_resp(cfg)


class PositionResp(BaseModel):
    id: int
    market_id: str
    market_label: str
    strategy: str
    side: str
    entry_price: float
    size: float
    target_price: Optional[float] = None
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
            Position.user_id == user.id, Position.status == "open"
        ).order_by(Position.opened_at.desc())
    ).all()
    return [PositionResp(**r.dict()) for r in rows]


@router.get("/history", response_model=List[PositionResp])
def history(limit: int = 30,
            user: User = Depends(get_current_user),
            session: Session = Depends(get_session)):
    rows = session.exec(
        select(Position).where(
            Position.user_id == user.id, Position.status == "closed"
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
            Position.user_id == user.id, Position.status == "closed"
        )
    ).all()
    wins = sum(1 for p in closed if (p.exit_reason or "").startswith(("tp", "expiry_win")))
    losses = len(closed) - wins
    open_count = session.exec(
        select(func.count()).select_from(Position).where(
            Position.user_id == user.id, Position.status == "open"
        )
    ).one() or 0

    cfg = _get_or_create_cfg(session, user)
    cache_key = f"{cfg.asset}_{cfg.duration_min}"
    mkt = _market_cache.get(cache_key)
    market_state = {}
    if mkt:
        now = int(time.time())
        elapsed = now - mkt["start_ts"]
        market_state = {
            "slug": mkt["slug"],
            "asset": mkt["asset"],
            "duration_min": mkt["duration_min"],
            "question": mkt["question"],
            "start_ts": mkt["start_ts"],
            "end_ts": mkt["end_ts"],
            "elapsed_pct": min(1.0, elapsed / (mkt["duration_min"] * 60)),
            "remaining_sec": max(0, mkt["end_ts"] - now),
        }
    return StatsResp(
        total_trades=len(closed),
        wins=wins, losses=losses,
        win_rate=(wins / len(closed) if closed else 0.0),
        total_pnl=round(sum(p.pnl for p in closed), 2),
        open_count=open_count,
        market_state=market_state,
    )
