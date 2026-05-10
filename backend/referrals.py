"""다단계 추천 시스템 + 익명/Google 가입 보너스 + 업그레이드 처리.

토큰 정책:
  - 익명 가입:        +10
  - Google 가입:      +20
  - 익명 → Google:    +10 (총 20 도달)
  - 추천 코드 통한 가입:  추천인 +20, 가입자 추가 +20
  - L2 (다단계):      원 추천인 +10
"""
import secrets
import string
from typing import Optional, List
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from . import config
from .db import get_session
from .models import User, TokenTx
from .auth import get_current_user


router = APIRouter(prefix="/api/referrals", tags=["referrals"])


def generate_referral_code(session: Session, length: int = 8) -> str:
    chars = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(chars) for _ in range(length))
        if not session.exec(select(User).where(User.referral_code == code)).first():
            return code


def handle_signup_referral(session: Session, new_user: User,
                            referral_code: Optional[str], is_anonymous: bool = True):
    """신규 가입자 보너스 + 추천 처리."""
    from .tokens import credit

    # 1) 가입 보너스 (익명/구글 차등)
    if is_anonymous:
        credit(session, new_user, config.SIGNUP_BONUS_ANON, "signup_anon",
               note="welcome (anonymous)")
    else:
        credit(session, new_user, config.SIGNUP_BONUS_GOOGLE, "signup_google",
               note="welcome (Google)")

    if not referral_code:
        return

    # 2) 추천 코드 통한 가입 → L1 + L2 분배
    referrer = session.exec(
        select(User).where(User.referral_code == referral_code.upper())
    ).first()
    if not referrer or referrer.id == new_user.id:
        return

    new_user.referred_by_id = referrer.id
    session.add(new_user)
    session.commit()

    # 가입자에게 추가 보너스
    credit(session, new_user, config.REFERRED_BONUS_TOKENS, "referred",
           ref_id=str(referrer.id),
           note=f"invited by {referrer.referral_code}")

    # 추천인 (L1) 보너스
    credit(session, referrer, config.REFERRAL_L1_TOKENS, "ref_l1",
           ref_id=str(new_user.id),
           note=f"invited {new_user.firebase_uid[:10]}")

    # 다단계 (L2): 추천인의 추천인에게 +10
    if referrer.referred_by_id:
        l2 = session.get(User, referrer.referred_by_id)
        if l2:
            credit(session, l2, config.REFERRAL_L2_TOKENS, "ref_l2",
                   ref_id=str(new_user.id),
                   note=f"L2 via {referrer.firebase_uid[:10]}")


def handle_upgrade(session: Session, user: User,
                   email: Optional[str] = None,
                   display_name: Optional[str] = None):
    """익명 → Google 업그레이드시 +10 토큰."""
    from .tokens import credit

    if user.auth_method != "anonymous":
        return  # 이미 업그레이드됨 또는 처음부터 Google 가입

    user.email = email
    user.display_name = display_name
    user.auth_method = "upgraded"
    session.add(user)
    session.commit()

    credit(session, user, config.UPGRADE_BONUS, "upgrade",
           note=f"anon → Google ({email})")


# ─── API ──────────────────────────────────────────────────────────

class TreeNode(BaseModel):
    id: int
    short: str
    referral_code: str
    auth_method: str
    joined: str
    children: List["TreeNode"] = []


TreeNode.model_rebuild()


def _build_tree(session: Session, root: User, max_depth: int = 5,
                depth: int = 0) -> TreeNode:
    short = (root.email or root.firebase_uid)[:12] + "..." if not root.email else root.email
    node = TreeNode(
        id=root.id,
        short=short,
        referral_code=root.referral_code,
        auth_method=root.auth_method,
        joined=root.created_at.isoformat()[:10],
    )
    if depth >= max_depth:
        return node
    children = session.exec(
        select(User).where(User.referred_by_id == root.id)
    ).all()
    node.children = [_build_tree(session, c, max_depth, depth + 1) for c in children]
    return node


@router.get("/tree", response_model=TreeNode)
def get_tree(user: User = Depends(get_current_user),
             session: Session = Depends(get_session)):
    return _build_tree(session, user)


class ReferralStats(BaseModel):
    referral_code: str
    invite_url: str
    direct_count: int
    indirect_count: int
    tokens_earned: int


class ApplyCodeReq(BaseModel):
    code: str


class ApplyCodeResp(BaseModel):
    ok: bool
    tokens: int
    bonus: int
    referrer_code: Optional[str] = None


@router.post("/apply_code", response_model=ApplyCodeResp)
def apply_code(req: ApplyCodeReq,
               user: User = Depends(get_current_user),
               session: Session = Depends(get_session)):
    """이미 가입된 사용자가 추천 코드를 사후 입력. 1회만 허용.

    가입자: +REFERRED_BONUS_TOKENS, 추천인: +REFERRAL_L1_TOKENS, L2: +REFERRAL_L2_TOKENS
    """
    from fastapi import HTTPException
    from .tokens import credit

    if user.referred_by_id:
        raise HTTPException(400, "already_referred")

    code = (req.code or "").strip().upper()
    if not code or len(code) < 4:
        raise HTTPException(400, "invalid_code")

    referrer = session.exec(
        select(User).where(User.referral_code == code)
    ).first()
    if not referrer:
        raise HTTPException(404, "code_not_found")
    if referrer.id == user.id:
        raise HTTPException(400, "self_referral")

    user.referred_by_id = referrer.id
    session.add(user)
    session.commit()

    credit(session, user, config.REFERRED_BONUS_TOKENS, "referred",
           ref_id=str(referrer.id),
           note=f"applied code {referrer.referral_code}")
    credit(session, referrer, config.REFERRAL_L1_TOKENS, "ref_l1",
           ref_id=str(user.id),
           note=f"code applied by {user.firebase_uid[:10]}")
    if referrer.referred_by_id:
        l2 = session.get(User, referrer.referred_by_id)
        if l2:
            credit(session, l2, config.REFERRAL_L2_TOKENS, "ref_l2",
                   ref_id=str(user.id),
                   note=f"L2 via {referrer.firebase_uid[:10]}")

    session.refresh(user)
    return ApplyCodeResp(
        ok=True, tokens=user.tokens,
        bonus=config.REFERRED_BONUS_TOKENS,
        referrer_code=referrer.referral_code,
    )


@router.get("/stats", response_model=ReferralStats)
def get_stats(user: User = Depends(get_current_user),
              session: Session = Depends(get_session)):
    direct = session.exec(
        select(User).where(User.referred_by_id == user.id)
    ).all()
    direct_count = len(direct)

    indirect_count = 0
    for d in direct:
        ind = session.exec(select(User).where(User.referred_by_id == d.id)).all()
        indirect_count += len(ind)

    earned_rows = session.exec(
        select(TokenTx).where(
            TokenTx.user_id == user.id,
            TokenTx.kind.in_(["ref_l1", "ref_l2", "referred", "upgrade"])
        )
    ).all()
    tokens_earned = sum(t.delta for t in earned_rows)

    # invite URL: SIWE_URI 대신 환경변수 또는 frontend가 알아서
    base = "https://hedam.io"
    return ReferralStats(
        referral_code=user.referral_code,
        invite_url=f"{base}/?ref={user.referral_code}",
        direct_count=direct_count,
        indirect_count=indirect_count,
        tokens_earned=tokens_earned,
    )
