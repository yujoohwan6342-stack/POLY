"""DB models — SQLModel."""
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    firebase_uid: str = Field(index=True, unique=True)
    email: Optional[str] = Field(default=None, index=True)
    display_name: Optional[str] = Field(default=None)
    auth_method: str = Field(default="anonymous")           # anonymous / google / upgraded
    referral_code: str = Field(index=True, unique=True)
    referred_by_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    tokens: int = Field(default=0)
    locale: str = Field(default="en")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_active: datetime = Field(default_factory=datetime.utcnow)


class TokenTx(SQLModel, table=True):
    """토큰 변동 이력."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    delta: int
    balance_after: int
    kind: str                                               # signup_anon / signup_google / upgrade / ref_l1 / ref_l2 / referred / cycle / ad
    ref_id: Optional[str] = Field(default=None)
    note: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class TradingConfig(SQLModel, table=True):
    """사용자별 자동 매매 설정 (활성/비활성, 전략)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True, unique=True)
    strategy: str = Field(default="lead")                   # low / lead
    active: bool = Field(default=False)
    max_cycles: int = Field(default=0)                      # 0 = 무제한 (잔액까지)
    cycles_consumed: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Position(SQLModel, table=True):
    """플랫폼 봇이 사용자 대신 진입한 가상 포지션."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    market_id: str = Field(index=True)                      # e.g. "btc-5m-2026-05-10T13:30Z"
    market_label: str                                       # 사람용 라벨
    strategy: str
    side: str                                               # YES / NO
    entry_price: float                                      # 0~1 (Polymarket 가격)
    size: float                                             # shares
    target_price: Optional[float] = None
    status: str = Field(default="open", index=True)         # open / closed
    exit_price: Optional[float] = None
    pnl: float = Field(default=0.0)                         # USD-equivalent
    exit_reason: Optional[str] = None                       # tp / expiry / loss
    opened_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    closed_at: Optional[datetime] = None


class Cycle(SQLModel, table=True):
    """봇이 보고한 매매 사이클."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    cycle_id: str = Field(index=True, unique=True)
    market_slug: str
    side: str
    entry_price: float
    exit_price: float
    shares: float
    pnl: float
    exit_reason: str
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
