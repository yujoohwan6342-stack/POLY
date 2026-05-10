"""사이트 누적 통계 — 방문 기록 + 일별 스냅샷 + 공개 카운터.

- /api/stats/visit (POST, no auth): 페이지 방문 기록. 30분 dedup.
- /api/stats/public (GET, no auth): 랜딩 카운터용 요약.
- /api/stats/timeline (GET, no auth): 최근 N일 일별 차트.

데이터는 누적이며 절대 자동 삭제하지 않습니다.
"""
from __future__ import annotations
import hashlib
import logging
import os
from datetime import datetime, timedelta, date as date_cls, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlmodel import Session, select, func

from .db import get_session, engine
from .models import User, Visit, DailyStat, Cycle, TokenTx


log = logging.getLogger("stats")
router = APIRouter(prefix="/api/stats", tags=["stats"])

_SALT = os.getenv("VISIT_SALT", "streak-default-salt-change-me")
_DEDUP_WINDOW = timedelta(minutes=30)


def _hash_visitor(ip: str, ua: str) -> str:
    raw = f"{ip}|{ua}|{_SALT}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ──────────────────────────────────────────────────────────────────
# Visit 기록
# ──────────────────────────────────────────────────────────────────
class VisitReq(BaseModel):
    page: Optional[str] = "landing"
    lang: Optional[str] = None


class VisitResp(BaseModel):
    ok: bool
    deduped: bool


@router.post("/visit", response_model=VisitResp)
def record_visit(req: VisitReq,
                 request: Request,
                 referer: Optional[str] = Header(None),
                 user_agent: Optional[str] = Header(None),
                 cf_ipcountry: Optional[str] = Header(None),
                 x_forwarded_for: Optional[str] = Header(None),
                 session: Session = Depends(get_session)):
    """방문 기록. 같은 visitor_hash+page 가 30분 내 있으면 dedup."""
    ip = (x_forwarded_for or request.client.host if request.client else "0.0.0.0").split(",")[0].strip()
    ua = (user_agent or "")[:200]
    vh = _hash_visitor(ip, ua)
    page = (req.page or "landing")[:64]

    cutoff = datetime.utcnow() - _DEDUP_WINDOW
    existing = session.exec(
        select(Visit).where(
            Visit.visitor_hash == vh,
            Visit.page == page,
            Visit.created_at >= cutoff,
        )
    ).first()
    if existing:
        return VisitResp(ok=True, deduped=True)

    # user_id 추출 시도 (Authorization 있는 경우)
    user_id = None
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        try:
            from .auth import _ensure_firebase
            from firebase_admin import auth as fb_auth
            if _ensure_firebase():
                decoded = fb_auth.verify_id_token(auth.split(" ", 1)[1])
                u = session.exec(select(User).where(User.firebase_uid == decoded["uid"])).first()
                if u:
                    user_id = u.id
        except Exception:
            pass

    v = Visit(
        visitor_hash=vh, user_id=user_id,
        page=page, referrer=(referer or "")[:500] or None,
        lang=req.lang, country=cf_ipcountry,
    )
    session.add(v)
    session.commit()
    return VisitResp(ok=True, deduped=False)


# ──────────────────────────────────────────────────────────────────
# 일별 스냅샷 — 자정 또는 첫 호출 시 생성
# ──────────────────────────────────────────────────────────────────
def ensure_daily_snapshot(session: Session, target_date: Optional[str] = None) -> DailyStat:
    """target_date(UTC) 의 스냅샷이 없으면 계산 후 INSERT.
    이미 있으면 skip (절대 덮어쓰지 않음)."""
    d = target_date or _today_utc()
    existing = session.exec(select(DailyStat).where(DailyStat.date == d)).first()
    if existing:
        return existing

    dt_start = datetime.fromisoformat(d).replace(tzinfo=timezone.utc).astimezone(timezone.utc).replace(tzinfo=None)
    dt_end = dt_start + timedelta(days=1)

    total_users = session.exec(select(func.count()).select_from(User)).one() or 0
    new_users = session.exec(
        select(func.count()).select_from(User).where(
            User.created_at >= dt_start, User.created_at < dt_end,
        )
    ).one() or 0
    total_visits = session.exec(select(func.count()).select_from(Visit)).one() or 0
    daily_visits = session.exec(
        select(func.count()).select_from(Visit).where(
            Visit.created_at >= dt_start, Visit.created_at < dt_end,
        )
    ).one() or 0
    unique_visitors = session.exec(
        select(func.count(func.distinct(Visit.visitor_hash))).where(
            Visit.created_at >= dt_start, Visit.created_at < dt_end,
        )
    ).one() or 0
    total_cycles = session.exec(select(func.count()).select_from(Cycle)).one() or 0
    daily_cycles = session.exec(
        select(func.count()).select_from(Cycle).where(
            Cycle.created_at >= dt_start, Cycle.created_at < dt_end,
        )
    ).one() or 0

    snap = DailyStat(
        date=d, total_users=total_users, new_users=new_users,
        total_visits=total_visits, daily_visits=daily_visits,
        unique_visitors=unique_visitors,
        total_cycles=total_cycles, daily_cycles=daily_cycles,
    )
    try:
        session.add(snap); session.commit(); session.refresh(snap)
    except Exception:
        session.rollback()
        existing = session.exec(select(DailyStat).where(DailyStat.date == d)).first()
        if existing: return existing
        raise
    return snap


# ──────────────────────────────────────────────────────────────────
# 공개 통계
# ──────────────────────────────────────────────────────────────────
class PublicStats(BaseModel):
    total_users: int
    total_visits: int
    total_cycles: int
    today_users: int
    today_visits: int
    today_unique: int
    today_cycles: int
    server_now: str


@router.get("/public", response_model=PublicStats)
def public_stats(session: Session = Depends(get_session)):
    today = _today_utc()
    dt_start = datetime.fromisoformat(today).replace(tzinfo=timezone.utc).replace(tzinfo=None)
    dt_end = dt_start + timedelta(days=1)

    total_users = session.exec(select(func.count()).select_from(User)).one() or 0
    total_visits = session.exec(select(func.count()).select_from(Visit)).one() or 0
    total_cycles = session.exec(select(func.count()).select_from(Cycle)).one() or 0

    today_users = session.exec(
        select(func.count()).select_from(User).where(
            User.created_at >= dt_start, User.created_at < dt_end,
        )
    ).one() or 0
    today_visits = session.exec(
        select(func.count()).select_from(Visit).where(
            Visit.created_at >= dt_start, Visit.created_at < dt_end,
        )
    ).one() or 0
    today_unique = session.exec(
        select(func.count(func.distinct(Visit.visitor_hash))).where(
            Visit.created_at >= dt_start, Visit.created_at < dt_end,
        )
    ).one() or 0
    today_cycles = session.exec(
        select(func.count()).select_from(Cycle).where(
            Cycle.created_at >= dt_start, Cycle.created_at < dt_end,
        )
    ).one() or 0

    return PublicStats(
        total_users=total_users, total_visits=total_visits, total_cycles=total_cycles,
        today_users=today_users, today_visits=today_visits,
        today_unique=today_unique, today_cycles=today_cycles,
        server_now=datetime.now(timezone.utc).isoformat(),
    )


# ──────────────────────────────────────────────────────────────────
# 타임라인 (차트)
# ──────────────────────────────────────────────────────────────────
class TimelinePoint(BaseModel):
    date: str
    total_users: int
    new_users: int
    daily_visits: int
    unique_visitors: int
    daily_cycles: int


@router.get("/timeline", response_model=List[TimelinePoint])
def timeline(days: int = 30, session: Session = Depends(get_session)):
    days = max(1, min(days, 365))
    today = datetime.now(timezone.utc).date()
    out: list[TimelinePoint] = []
    # 누락된 일자도 백필
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        snap = session.exec(select(DailyStat).where(DailyStat.date == d)).first()
        if not snap:
            try:
                snap = ensure_daily_snapshot(session, d)
            except Exception as e:
                log.warning("snapshot %s err: %s", d, e)
                continue
        out.append(TimelinePoint(
            date=snap.date, total_users=snap.total_users,
            new_users=snap.new_users, daily_visits=snap.daily_visits,
            unique_visitors=snap.unique_visitors, daily_cycles=snap.daily_cycles,
        ))
    return out
