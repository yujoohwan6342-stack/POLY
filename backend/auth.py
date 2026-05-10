"""SIWE (Sign-In With Ethereum) 인증 + JWT 세션."""
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from jose import jwt, JWTError
from sqlmodel import Session, select
from siwe import SiweMessage

from . import config
from .db import get_session
from .models import User, Nonce
# referrals는 lazy import (circular dep 방지)


router = APIRouter(prefix="/api/auth", tags=["auth"])


class NonceResp(BaseModel):
    nonce: str
    domain: str
    uri: str
    chain_id: int
    issued_at: str


class VerifyReq(BaseModel):
    message: str
    signature: str
    referral_code: Optional[str] = None


class SessionResp(BaseModel):
    token: str
    address: str
    referral_code: str
    tokens: int
    locale: str


def _create_jwt(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "addr": user.address,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=config.JWT_TTL_HOURS)).timestamp()),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALG)


def get_current_user(
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALG])
    except JWTError:
        raise HTTPException(401, "invalid token")
    user = session.get(User, int(payload["sub"]))
    if not user:
        raise HTTPException(401, "user not found")
    user.last_active = datetime.utcnow()
    session.add(user)
    session.commit()
    return user


@router.get("/nonce", response_model=NonceResp)
def get_nonce(session: Session = Depends(get_session)):
    """클라이언트가 메타마스크로 서명할 챌린지 발급."""
    nonce = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    session.add(Nonce(nonce=nonce))
    session.commit()
    return NonceResp(
        nonce=nonce,
        domain=config.SIWE_DOMAIN,
        uri=config.SIWE_URI,
        chain_id=config.CHAIN_ID,
        issued_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post("/verify", response_model=SessionResp)
def verify(req: VerifyReq, session: Session = Depends(get_session)):
    """SIWE 메시지 + 서명 검증 → 세션 발급."""
    try:
        msg = SiweMessage(message=req.message)
        msg.verify(req.signature, nonce=msg.nonce, domain=msg.domain)
    except Exception as e:
        raise HTTPException(400, f"siwe verify failed: {e}")

    # nonce 1회용 처리
    nrow = session.exec(select(Nonce).where(Nonce.nonce == msg.nonce)).first()
    if not nrow or nrow.consumed:
        raise HTTPException(400, "invalid or used nonce")
    age = (datetime.utcnow() - nrow.created_at).total_seconds()
    if age > config.NONCE_TTL_SEC:
        raise HTTPException(400, "nonce expired")
    nrow.consumed = True
    session.add(nrow)

    address = msg.address.lower()
    user = session.exec(select(User).where(User.address == address)).first()
    if not user:
        # 신규가입 — 추천 코드 처리 + 가입 보너스 (lazy import로 circular 회피)
        from .referrals import generate_referral_code, handle_signup_referral
        user = User(
            address=address,
            referral_code=generate_referral_code(session),
            tokens=0,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        handle_signup_referral(session, user, req.referral_code)

    session.commit()
    session.refresh(user)
    token = _create_jwt(user)
    return SessionResp(
        token=token,
        address=user.address,
        referral_code=user.referral_code,
        tokens=user.tokens,
        locale=user.locale,
    )


@router.get("/me", response_model=SessionResp)
def me(user: User = Depends(get_current_user)):
    """현재 세션 정보."""
    return SessionResp(
        token="",
        address=user.address,
        referral_code=user.referral_code,
        tokens=user.tokens,
        locale=user.locale,
    )
