"""다단계 추천 시스템 (L1=10토큰, L2=5토큰)."""
import secrets
import string
from typing import Optional, List
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from . import config
from .db import get_session
from .models import User, TokenTx


router = APIRouter(prefix="/api/referrals", tags=["referrals"])


def generate_referral_code(session: Session, length: int = 8) -> str:
    """짧고 유일한 추천 코드 생성."""
    chars = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(chars) for _ in range(length))
        if not session.exec(select(User).where(User.referral_code == code)).first():
            return code


def handle_signup_referral(session: Session, new_user: User,
                           referral_code: Optional[str]):
    """신규 가입자 처리: 본인 가입 보너스 + 추천인 보상 (L1, L2 분배)."""
    from .tokens import credit  # circular import 방지

    # 1) 가입 보너스
    credit(session, new_user, config.SIGNUP_BONUS_TOKENS, "signup",
           note="welcome bonus")

    if not referral_code:
        return

    # 2) L1 추천인
    referrer = session.exec(
        select(User).where(User.referral_code == referral_code.upper())
    ).first()
    if not referrer or referrer.id == new_user.id:
        return

    new_user.referred_by_id = referrer.id
    session.add(new_user)
    session.commit()

    credit(session, referrer, config.REFERRAL_L1_TOKENS, "ref_l1",
           ref_id=str(new_user.id), note=f"L1 ref of {new_user.address[:8]}")

    # 3) L2 추천인 (referrer를 추천한 사람)
    if referrer.referred_by_id:
        l2 = session.get(User, referrer.referred_by_id)
        if l2:
            credit(session, l2, config.REFERRAL_L2_TOKENS, "ref_l2",
                   ref_id=str(new_user.id),
                   note=f"L2 ref of {new_user.address[:8]} via {referrer.address[:8]}")


# ─── API ──────────────────────────────────────────────────────────

class TreeNode(BaseModel):
    id: int
    address: str
    short: str
    referral_code: str
    joined: str
    children: List["TreeNode"] = []


TreeNode.model_rebuild()


def _build_tree(session: Session, root: User, max_depth: int = 5,
                depth: int = 0) -> TreeNode:
    node = TreeNode(
        id=root.id,
        address=root.address,
        short=root.address[:6] + "..." + root.address[-4:],
        referral_code=root.referral_code,
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
def get_tree(user=Depends(__import__("backend.auth", fromlist=["get_current_user"]).get_current_user),
             session: Session = Depends(get_session)):
    """본인을 루트로 한 추천 트리 반환 (D3 시각화용)."""
    return _build_tree(session, user)


class ReferralStats(BaseModel):
    referral_code: str
    invite_url: str
    direct_count: int           # L1 (직접)
    indirect_count: int         # L2 (간접)
    tokens_earned: int          # 추천으로 받은 누적 토큰


@router.get("/stats", response_model=ReferralStats)
def get_stats(user=Depends(__import__("backend.auth", fromlist=["get_current_user"]).get_current_user),
              session: Session = Depends(get_session)):
    direct = session.exec(
        select(User).where(User.referred_by_id == user.id)
    ).all()
    direct_count = len(direct)

    indirect_count = 0
    for d in direct:
        ind = session.exec(
            select(User).where(User.referred_by_id == d.id)
        ).all()
        indirect_count += len(ind)

    earned_rows = session.exec(
        select(TokenTx).where(
            TokenTx.user_id == user.id,
            TokenTx.kind.in_(["ref_l1", "ref_l2"])
        )
    ).all()
    tokens_earned = sum(t.delta for t in earned_rows)

    return ReferralStats(
        referral_code=user.referral_code,
        invite_url=f"{config.SIWE_URI}/?ref={user.referral_code}",
        direct_count=direct_count,
        indirect_count=indirect_count,
        tokens_earned=tokens_earned,
    )
