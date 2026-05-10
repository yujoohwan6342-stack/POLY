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


def _classify_error(msg: str) -> str:
    """폴리마켓 에러 메시지 → 사용자 친화 코드."""
    m = (msg or "").lower()
    if "insufficient" in m and ("balance" in m or "fund" in m): return "insufficient_balance"
    if "allowance" in m: return "allowance_required"
    if "min order" in m or "minimum" in m: return "below_min_order"
    if "tick" in m: return "invalid_tick_size"
    if "neg risk" in m or "neg_risk" in m: return "neg_risk_mismatch"
    if "rejected" in m or "not enough liquidity" in m: return "no_liquidity"
    if "timeout" in m or "deadline" in m: return "timeout"
    if "nonce" in m: return "nonce_error"
    if "signature" in m: return "signature_error"
    return "order_failed"


def execute_order(pk: str, *, action: str, token_id: str, price: float,
                  size: float, order_type: str = "limit",
                  funder: Optional[str] = None,
                  max_price: Optional[float] = None,
                  neg_risk: bool = False,
                  tick_size: float = 0.01,
                  preflight: bool = True) -> dict:
    """단일 주문 실행. action='buy'|'sell', order_type='limit'|'market'.

    안전장치:
      - preflight=True: 발주 전 잔액/allowance 사전 검증 → 무의미한 실패 차단
      - tick_size 정렬 (0.01 또는 0.001 단위)
      - price 0.01~0.99 클램프 (Polymarket은 1.0/0.0 거부)
      - 1회 retry on transient errors (timeout, nonce)

    Returns: {ok, order_id?, error?, error_code?, address}
    PK 는 응답에 절대 포함되지 않는다.
    """
    from eth_account import Account
    from py_clob_client_v2.clob_types import (
        OrderArgs, MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType
    )
    from py_clob_client_v2.order_builder.constants import BUY, SELL

    pk_in = pk if pk.startswith("0x") else "0x" + pk
    try:
        addr = Account.from_key(pk_in).address
    except Exception:
        return {"ok": False, "error": "invalid_pk", "error_code": "invalid_pk", "address": None}

    # 가격 클램프 + tick 정렬
    if tick_size <= 0: tick_size = 0.01
    price = max(0.01, min(0.99, round(price / tick_size) * tick_size))
    price = round(price, 4)
    if max_price is not None:
        max_price = max(0.01, min(0.99, round(max_price / tick_size) * tick_size))
        max_price = round(max_price, 4)

    # 사이즈 가드
    if size < 5.0 and action == "buy":
        # Polymarket 최소 5 shares 또는 $1 — 작은 주문은 거부됨
        return {"ok": False, "error": "size_too_small", "error_code": "below_min_order", "address": addr}

    side_const = BUY if action == "buy" else SELL

    try:
        client, sig_type = _make_client(pk_in, funder)

        # ── Pre-flight: 잔액 + allowance 확인 ─────────────────
        if preflight and action == "buy":
            try:
                bal = client.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
                )
                balance_usd = float(bal.get("balance", 0)) / 1e6
                allowance_usd = float(bal.get("allowance", 0)) / 1e6
                needed = round(price * size, 2)
                if balance_usd < needed:
                    return {"ok": False, "error_code": "insufficient_balance",
                            "error": f"need ${needed:.2f}, have ${balance_usd:.2f}",
                            "address": addr, "balance": balance_usd}
                if allowance_usd < needed:
                    return {"ok": False, "error_code": "allowance_required",
                            "error": f"approve Polymarket allowance for at least ${needed:.2f} on app.polymarket.com",
                            "address": addr, "allowance": allowance_usd}
            except Exception as e:
                log.warning("preflight balance check failed addr=%s: %s", _redact(addr), e)
                # 사전체크 실패해도 발주는 시도 (네트워크 일시 오류 가능)

        # ── 주문 발주 (1회 retry) ─────────────────────────────
        last_err = None
        for attempt in range(2):
            try:
                if order_type == "market":
                    args = MarketOrderArgs(
                        token_id=token_id,
                        amount=round(size * price, 2) if action == "buy" else round(size, 2),
                        side=side_const,
                        price=round(max_price if max_price is not None else price, 4),
                    )
                    signed = client.create_market_order(args)
                    resp = client.post_order(signed, OrderType.FOK)
                else:
                    args = OrderArgs(
                        token_id=token_id, price=round(price, 4),
                        size=round(size, 2), side=side_const,
                    )
                    signed = client.create_order(args)
                    resp = client.post_order(signed, OrderType.GTC)
                log.info("ORDER addr=%s %s token=%s..%s px=%.4f size=%.2f attempt=%d resp_keys=%s",
                         _redact(addr), action, token_id[:6], token_id[-4:],
                         price, size, attempt + 1,
                         list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__)

                # 응답 검증 — Polymarket 은 success+errorMsg 패턴 가능
                if isinstance(resp, dict):
                    err_msg = resp.get("errorMsg") or resp.get("error_msg") or resp.get("error")
                    if err_msg:
                        ec = _classify_error(err_msg)
                        if ec in ("timeout", "nonce_error") and attempt == 0:
                            last_err = err_msg
                            continue
                        return {"ok": False, "error": str(err_msg)[:200],
                                "error_code": ec, "address": addr, "raw": resp}
                return {"ok": True, "address": addr, "raw": resp}
            except Exception as e:
                msg = str(e)[:300]
                ec = _classify_error(msg)
                if ec in ("timeout", "nonce_error") and attempt == 0:
                    last_err = msg
                    continue
                log.warning("order fail addr=%s action=%s code=%s err=%s",
                            _redact(addr), action, ec, msg[:200])
                return {"ok": False, "error": msg[:200], "error_code": ec, "address": addr}
        return {"ok": False, "error": last_err or "unknown", "error_code": "order_failed", "address": addr}
    except Exception as e:
        log.warning("client init fail addr=%s err=%s", _redact(addr), str(e)[:200])
        return {"ok": False, "error": str(e)[:200],
                "error_code": _classify_error(str(e)), "address": addr}
    # 함수 종료 시 client, signed, args 모두 GC
