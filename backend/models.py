"""DB models — SQLModel (= SQLAlchemy + Pydantic)."""
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field, Relationship


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    address: str = Field(index=True, unique=True)         # 0x... 소문자
    referral_code: str = Field(index=True, unique=True)   # 자기 자신 추천 코드
    referred_by_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    tokens: int = Field(default=0)                        # 토큰 잔액 (정수, 1=1cent)
    locale: str = Field(default="en")                      # ko / en / zh
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_active: datetime = Field(default_factory=datetime.utcnow)


class TokenTx(SQLModel, table=True):
    """토큰 변동 이력 (충전/차감/추천보상)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    delta: int                                          # +충전 -사용 등
    balance_after: int
    kind: str                                           # 'signup' | 'topup' | 'cycle' | 'ref_l1' | 'ref_l2' | 'refund'
    ref_id: Optional[str] = Field(default=None)         # tx hash, cycle_id 등
    note: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class Cycle(SQLModel, table=True):
    """봇이 보고한 사이클 (실제 거래 1회)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    cycle_id: str = Field(index=True, unique=True)
    market_slug: str
    side: str                                           # 'up' / 'down'
    entry_price: float
    exit_price: float
    shares: float
    pnl: float
    exit_reason: str                                    # TP / SL / EXPIRY / TIMEOUT
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class Nonce(SQLModel, table=True):
    """SIWE nonce — 1회용."""
    id: Optional[int] = Field(default=None, primary_key=True)
    nonce: str = Field(index=True, unique=True)
    address: Optional[str] = Field(default=None)
    consumed: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


