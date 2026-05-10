"""Stateless Polymarket 실행 프록시.

설계 원칙 (변경 금지):
  ┌──────────────────────────────────────────────────────────────┐
  │ 1. PK 는 절대 디스크/DB/로그에 기록되지 않는다              │
  │ 2. 각 함수는 PK 를 인자로 받아 함수 종료 시 GC 에 맡긴다     │
  │ 3. 응답에 PK 또는 그 일부 (앞/뒤 4글자 등) 를 포함하지 않는다│
  │ 4. 모듈 전역에 PK 캐시/세션이 존재하지 않는다 (state-free)   │
  └──────────────────────────────────────────────────────────────┘

운영 추가 의무:
  - nginx: access log 에서 request body 제외 (default 그대로면 안전)
  - 모든 endpoint HTTPS only
  - 사용자에게 "전용 wallet 사용 + 거래 금액만 입금" 명시
"""
from __future__ import annotations
import json
import logging
import time
from typing import Optional, Any

import httpx

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

log = logging.getLogger("pmx")

# 안전장치: 로깅 시 PK 노출 방지를 위한 redact 헬퍼
def _redact(s: str) -> str:
    if not s or len(s) < 12:
        return "***"
    return "***" + s[-4:]    # 식별용 4자리만, PK 본체는 절대 X


# ──────────────────────────────────────────────────────────────────
# 공개 데이터 (PK 불필요)
# ──────────────────────────────────────────────────────────────────
def fetch_market(asset_slug_prefix: str, duration_min: int) -> Optional[dict]:
    """현재 진행 중인 마켓 메타데이터 (gamma)."""
    duration_sec = duration_min * 60
    now = int(time.time())
    current_window = now - (now % duration_sec)
    for offset in (0, duration_sec):
        ts = current_window + offset
        slug = f"{asset_slug_prefix}-{duration_min}m-{ts}"
        try:
            r = httpx.get(f"{GAMMA_HOST}/events", params={"slug": slug}, timeout=4.0)
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
                "neg_risk": m.get("negRisk", False),
                "tick_size": float(m.get("orderPriceMinTickSize", 0.01)),
            }
        except Exception as e:
            log.debug("market lookup %s: %s", slug, e)
    return None


def fetch_book(token_id: str) -> dict:
    try:
        r = httpx.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=3.0)
        ob = r.json()
        bids = sorted([(float(b["price"]), float(b["size"])) for b in ob.get("bids", [])],
                      key=lambda x: -x[0])
        asks = sorted([(float(a["price"]), float(a["size"])) for a in ob.get("asks", [])],
                      key=lambda x: x[0])
        return {
            "bids": bids[:5], "asks": asks[:5],
            "best_bid": bids[0][0] if bids else 0.0,
            "best_ask": asks[0][0] if asks else 1.0,
        }
    except Exception:
        return {"bids": [], "asks": [], "best_bid": 0.0, "best_ask": 1.0}


# ──────────────────────────────────────────────────────────────────
# 사용자 인증 (PK 인자, 함수 종료 시 release)
# ──────────────────────────────────────────────────────────────────
def _make_client(pk: str, funder: Optional[str] = None):
    """ClobClient 생성 — 호출자가 with 블록처럼 사용 후 즉시 폐기 권장."""
    from py_clob_client_v2.client import ClobClient
    if not pk.startswith("0x"):
        pk = "0x" + pk
    sig_type = 2 if funder else 0
    client = ClobClient(host=CLOB_HOST, chain_id=137, key=pk,
                        signature_type=sig_type, funder=funder or None)
    try:
        creds = client.derive_api_key()
    except Exception:
        creds = client.create_api_key()
    client.set_api_creds(creds)
    return client, sig_type


def get_address_balance(pk: str, funder: Optional[str] = None) -> dict:
    """PK 인증 + 주소/잔액 반환. PK 는 인자에서만 살아있음."""
    from eth_account import Account
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

    pk_in = pk if pk.startswith("0x") else "0x" + pk
    addr = Account.from_key(pk_in).address
    try:
        client, sig_type = _make_client(pk_in, funder)
        bal = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
        )
        balance = float(bal.get("balance", 0)) / 1e6
        allowance = float(bal.get("allowance", 0)) / 1e6
        return {
            "address": addr, "funder": funder, "balance_usdc": balance,
            "allowance_usdc": allowance, "sig_type": sig_type,
        }
    except Exception as e:
        log.warning("balance fetch %s err=%s", _redact(addr), e)
        return {"address": addr, "funder": funder, "balance_usdc": 0.0,
                "allowance_usdc": 0.0, "error": str(e)}
    # PK 는 함수 끝나면 GC


def execute_order(pk: str, *, action: str, token_id: str, price: float,
                  size: float, order_type: str = "limit",
                  funder: Optional[str] = None,
                  max_price: Optional[float] = None) -> dict:
    """단일 주문 실행. action='buy'|'sell', order_type='limit'|'market'.

    Returns: {ok, order_id?, status?, error?}
    PK 는 응답에 절대 포함되지 않는다.
    """
    from eth_account import Account
    from py_clob_client_v2.clob_types import (
        OrderArgs, MarketOrderArgs, OrderType
    )
    from py_clob_client_v2.order_builder.constants import BUY, SELL

    pk_in = pk if pk.startswith("0x") else "0x" + pk
    try:
        addr = Account.from_key(pk_in).address
    except Exception as e:
        return {"ok": False, "error": "invalid_pk"}

    side_const = BUY if action == "buy" else SELL

    try:
        client, _ = _make_client(pk_in, funder)
        if order_type == "market":
            args = MarketOrderArgs(
                token_id=token_id,
                amount=round(size if action == "buy" else size * price, 2),
                side=side_const,
                price=round(max_price or price, 2),
            )
            signed = client.create_market_order(args)
            resp = client.post_order(signed, OrderType.FOK)
        else:
            args = OrderArgs(
                token_id=token_id, price=round(price, 2),
                size=round(size, 2), side=side_const,
            )
            signed = client.create_order(args)
            resp = client.post_order(signed, OrderType.GTC)
        log.info("ORDER addr=%s %s token=%s..%s px=%.2f size=%.2f resp_keys=%s",
                 _redact(addr), action, token_id[:6], token_id[-4:],
                 price, size, list(resp.keys()) if isinstance(resp, dict) else type(resp))
        return {"ok": True, "address": addr, "raw": resp}
    except Exception as e:
        # 절대 PK 노출 X — addr 만
        log.warning("order fail addr=%s action=%s err=%s", _redact(addr), action, str(e)[:200])
        return {"ok": False, "error": str(e)[:200], "address": addr}
    # 함수 종료 시 client, signed, args 모두 GC
