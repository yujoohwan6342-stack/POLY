"""토큰 잔액 관리 + 봇 차감 API."""
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from . import config
from .db import get_session
from .models import User, TokenTx, Cycle
from .auth import get_current_user


router = APIRouter(prefix="/api/tokens", tags=["tokens"])


def credit(session: Session, user: User, delta: int, kind: str,
           ref_id: Optional[str] = None, note: Optional[str] = None) -> TokenTx:
    """토큰 부여/차감 + 이력 기록 (atomic)."""
    user.tokens += delta
    if user.tokens < 0:
        raise HTTPException(402, "insufficient tokens")
    session.add(user)
    tx = TokenTx(user_id=user.id, delta=delta, balance_after=user.tokens,
                 kind=kind, ref_id=ref_id, note=note)
    session.add(tx)
    session.commit()
    session.refresh(user)
    session.refresh(tx)
    return tx


class BalanceResp(BaseModel):
    tokens: int
    estimated_cycles_left: int       # 1 cycle = COST_PER_CYCLE tokens


class TxResp(BaseModel):
    id: int
    delta: int
    balance_after: int
    kind: str
    ref_id: Optional[str]
    note: Optional[str]
    created_at: datetime


@router.get("/balance", response_model=BalanceResp)
def get_balance(user: User = Depends(get_current_user)):
    return BalanceResp(
        tokens=user.tokens,
        estimated_cycles_left=user.tokens // max(1, config.COST_PER_CYCLE),
    )


@router.get("/history", response_model=List[TxResp])
def get_history(limit: int = 50, user: User = Depends(get_current_user),
                session: Session = Depends(get_session)):
    rows = session.exec(
        select(TokenTx).where(TokenTx.user_id == user.id)
        .order_by(TokenTx.created_at.desc()).limit(limit)
    ).all()
    return [TxResp(**r.dict()) for r in rows]


# ─── 봇 콜백 — 사이클 시도 직전 토큰 차감 ──────────────────────

class CycleConsumeReq(BaseModel):
    cycle_id: str
    market_slug: str


class CycleConsumeResp(BaseModel):
    ok: bool
    tokens_left: int
    error: Optional[str] = None


@router.post("/consume", response_model=CycleConsumeResp)
def consume_for_cycle(req: CycleConsumeReq,
                      user: User = Depends(get_current_user),
                      session: Session = Depends(get_session)):
    """봇이 매수 직전 호출. COST_PER_CYCLE 토큰 차감.
    이미 같은 cycle_id로 차감된 적 있으면 idempotent (중복 차감 방지)."""
    existing = session.exec(
        select(TokenTx).where(
            TokenTx.user_id == user.id,
            TokenTx.kind == "cycle",
            TokenTx.ref_id == req.cycle_id,
        )
    ).first()
    if existing:
        return CycleConsumeResp(ok=True, tokens_left=user.tokens)
    if user.tokens < config.COST_PER_CYCLE:
        return CycleConsumeResp(
            ok=False, tokens_left=user.tokens,
            error="insufficient_tokens",
        )
    credit(session, user, -config.COST_PER_CYCLE, "cycle",
           ref_id=req.cycle_id, note=req.market_slug)
    return CycleConsumeResp(ok=True, tokens_left=user.tokens)


class CycleReportReq(BaseModel):
    cycle_id: str
    market_slug: str
    side: str
    entry_price: float
    exit_price: float
    shares: float
    pnl: float
    exit_reason: str


@router.post("/cycle_report")
def report_cycle(req: CycleReportReq,
                 user: User = Depends(get_current_user),
                 session: Session = Depends(get_session)):
    """봇이 사이클 종료 후 결과 보고 (선택, 통계용)."""
    existing = session.exec(
        select(Cycle).where(Cycle.cycle_id == req.cycle_id)
    ).first()
    if existing:
        return {"ok": True, "deduped": True}
    cyc = Cycle(user_id=user.id, **req.dict())
    session.add(cyc)
    session.commit()
    return {"ok": True}
