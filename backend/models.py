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
