"""Firebase Auth (Anonymous + Google) — ID token 검증 기반.

흐름:
  1. 클라이언트 = Firebase JS SDK로 익명 또는 Google 로그인
  2. 클라이언트 = ID token 받음, 모든 API 요청 시 Authorization 헤더에 포함
  3. 서버 = Firebase Admin SDK로 토큰 검증 → uid, email, provider 추출
  4. 서버 = uid로 User 조회/생성 (없으면 가입 보너스 + 추천 처리)
"""
import logging
import os
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlmodel import Session, select

from . import config
from .db import get_session
from .models import User


log = logging.getLogger("auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


# ─── Firebase Admin SDK 지연 초기화 (없어도 서버는 동작) ─────────

_firebase_initialized = False
_firebase_failed = False
_firebase_error = ""


def _ensure_firebase():
    """필요할 때만 초기화. 실패해도 예외 던지지 않고 사용 시점에 503 반환."""
    global _firebase_initialized, _firebase_failed, _firebase_error
    if _firebase_initialized:
        return True
    if _firebase_failed:
        return False
    try:
        import firebase_admin
        from firebase_admin import credentials

        cred_path = config.GOOGLE_APPLICATION_CREDENTIALS
        if cred_path and os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
        else:
            cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred, {"projectId": config.FIREBASE_PROJECT_ID})
        _firebase_initialized = True
        log.info(f"Firebase Admin initialized for {config.FIREBASE_PROJECT_ID}")
        return True
    except Exception as e:
        _firebase_failed = True
        _firebase_error = str(e)
        log.warning(f"Firebase Admin not available: {e} — auth endpoints will return 503")
        return False


def is_firebase_ready() -> bool:
    """공개 헬퍼 — config endpoint 등에서 사용."""
    return _ensure_firebase()


# ─── Dependency: 현재 사용자 (모든 인증 필요한 API에서 사용) ──

def get_current_user(
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
) -> User:
    """Firebase ID token 검증 → 내부 User 조회 또는 생성."""
    if not _ensure_firebase():
        raise HTTPException(503, f"firebase_not_configured: {_firebase_error}")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing token")
    id_token = authorization.split(" ", 1)[1]

    try:
        from firebase_admin import auth as fb_auth
        decoded = fb_auth.verify_id_token(id_token)
    except Exception as e:
        log.warning(f"token verify failed: {e}")
        raise HTTPException(401, f"invalid token: {e}")

    uid = decoded["uid"]
    email = decoded.get("email")
    name = decoded.get("name")
    provider = decoded.get("firebase", {}).get("sign_in_provider", "anonymous")
    is_anonymous = provider == "anonymous"

    user = session.exec(select(User).where(User.firebase_uid == uid)).first()
    if user:
        # 업그레이드 감지: 익명 → Google 전환
        if user.auth_method == "anonymous" and not is_anonymous:
            from .referrals import handle_upgrade
            handle_upgrade(session, user, email=email, display_name=name)
        user.last_active = datetime.utcnow()
        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    # 신규 가입은 /api/auth/register 에서만 (referral_code 받기 위해)
    raise HTTPException(404, "user_not_found_register_required")


# ─── 신규 가입 (referral_code 옵션) ────────────────────────────

class RegisterReq(BaseModel):
    referral_code: Optional[str] = None


class SessionResp(BaseModel):
    address: str           # firebase_uid (legacy field name kept for frontend)
    referral_code: str
    tokens: int
    locale: str
    auth_method: str
    email: Optional[str] = None


@router.post("/register", response_model=SessionResp)
def register(
    req: RegisterReq,
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    """첫 로그인 시 호출. 가입 보너스 + 추천 코드 처리."""
    if not _ensure_firebase():
        raise HTTPException(503, f"firebase_not_configured: {_firebase_error}")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing token")
    id_token = authorization.split(" ", 1)[1]

    try:
        from firebase_admin import auth as fb_auth
        decoded = fb_auth.verify_id_token(id_token)
    except Exception as e:
        raise HTTPException(401, f"invalid token: {e}")

    uid = decoded["uid"]
    email = decoded.get("email")
    name = decoded.get("name")
    provider = decoded.get("firebase", {}).get("sign_in_provider", "anonymous")
    is_anonymous = provider == "anonymous"

    user = session.exec(select(User).where(User.firebase_uid == uid)).first()
    if user:
        # 이미 등록됨 → 그냥 반환
        return _to_resp(user)

    # 신규 생성
    from .referrals import generate_referral_code, handle_signup_referral
    auth_method = "anonymous" if is_anonymous else "google"
    user = User(
        firebase_uid=uid,
        email=email,
        display_name=name,
        auth_method=auth_method,
        referral_code=generate_referral_code(session),
        tokens=0,  # bonus는 referral handler에서
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    handle_signup_referral(session, user, req.referral_code, is_anonymous=is_anonymous)
    session.commit()
    session.refresh(user)
    return _to_resp(user)


@router.get("/me", response_model=SessionResp)
def me(user: User = Depends(get_current_user)):
    return _to_resp(user)


def _to_resp(u: User) -> SessionResp:
    return SessionResp(
        address=u.firebase_uid,
        referral_code=u.referral_code,
        tokens=u.tokens,
        locale=u.locale,
        auth_method=u.auth_method,
        email=u.email,
    )
