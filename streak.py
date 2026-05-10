#!/usr/bin/env python3
"""
BTC 5분 마켓 10c -> 15c 전략 (시뮬레이션 전용)

전략:
  - 마켓 50% 경과 후 ~ 마감까지 모니터링
  - Up 또는 Down 가격이 entry_price(기본 10c)에 도달하면 매수
  - 즉시 tp_price(기본 15c) 리밋 매도 (익절)
  - sl_price(기본 5c) 도달시 매도 (손절)
  - 1마켓당 1사이클만 진행
  - 마켓 종료시 미체결 포지션은 자동 정산

사용법:
  python3 strategy_10c.py
  → 브라우저에서 http://localhost:8765 접속
"""

import asyncio
import csv
import json
import logging
import os
import ssl
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import aiohttp
import certifi

# ─── Config ─────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
# 서버는 stateless: 사용자 데이터는 브라우저 localStorage에 저장

CONFIG_FILE = BASE_DIR / 'strategy_config.json'
DASHBOARD_HTML = BASE_DIR / 'dashboard.html'

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
BINANCE_HOST = "https://api.binance.com"

DEFAULT_CONFIG = {
    "bet_size_usd": 1.0,            # 베팅 금액 ($)
    "entry_price": 0.10,            # 리밋 매수 가격
    "tp_price": 0.15,               # 리밋 매도 (익절) 가격
    "sl_price": 0.05,               # 손절 가격
    "entry_tolerance": 0.01,        # entry_price ± tolerance 범위에서 리밋 매수 시도
    "tradeable_pct": 0.60,          # 마켓 진행률이 이 값 미만일 때만 매수 허용 (early cutoff)
    "buy_when_remaining_below_pct": 1.0,  # 남은시간이 이 값 이하일 때만 매수 (기본 1.0 = 100% = 항상 허용)
    "entry_mode": "low_target",     # 'low_target' (10c±tol exact) | 'high_lead' (>=entry_price, leading side)
    "max_entry_price": 0.85,        # high_lead 모드: ask가 이 값 이하일 때만 매수 (너무 높으면 upside 없음)
    "buy_order_type": "limit",      # 매수 주문 타입: 'limit' | 'market'
    "sell_order_type": "limit",     # 매도 주문 타입: 'limit' | 'market'
    "max_cycles_per_session": 0,    # 세션당 최대 사이클 수 (0 = 무제한, 1 = 한 번만 매수 후 정지)
    "poll_interval_sec": 0.5,       # 가격 조회 주기 (초, 1초 이내)
    "taker_fee": 0.0,               # 폴리마켓 0% (실제 정산 기준)
    "maker_fee": 0.0,               # 폴리마켓 0%
    "http_port": 8765,
}

LOG_FMT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FMT)
log = logging.getLogger("strategy")

# ─── Helpers ────────────────────────────────────────────────────────

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            return merged
        except Exception as e:
            log.warning(f"Config load failed: {e}, using defaults")
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)


def append_csv(row):
    """No-op: 모든 거래 데이터는 클라이언트(브라우저 localStorage)에 저장.
    서버는 stateless. 클라이언트가 /api/status 폴링하며 새 cycle을 감지해서 본인 LS에 저장."""
    return

# ─── State ──────────────────────────────────────────────────────────

@dataclass
class Position:
    side: str = ""              # 'up' or 'down'
    shares: float = 0.0
    entry_price: float = 0.0
    cost: float = 0.0
    entry_time: float = 0.0
    cycle_id: str = ""
    # 리밋 매도 상태
    sell_limit_price: float = 0.0
    sell_placed_at: float = 0.0  # 리밋 매도 주문 placement 시각
    sell_order_id: str = ""      # 라이브 모드 실제 매도 주문 ID


@dataclass
class PendingBuy:
    """대기중인 리밋 매수 주문."""
    side: str = ""
    limit_price: float = 0.0
    shares: float = 0.0
    placed_at: float = 0.0
    cycle_id: str = ""
    order_id: str = ""           # 라이브 모드 실제 주문 ID


@dataclass
class Cycle:
    cycle_id: str
    slug: str
    side: str
    entry_price: float
    entry_time: float
    shares: float
    cost: float
    exit_price: float = 0.0
    exit_time: float = 0.0
    exit_reason: str = ""   # 'TP', 'SL', 'EXPIRY', 'WIN_PAYOUT'
    pnl: float = 0.0
    fee: float = 0.0


class State:
    def __init__(self):
        self.config = load_config()
        self.running = False
        self.current_slug = ""
        self.current_market_info = None  # {'slug', 'yes_token', 'no_token', 'end_ts'}
        self.position: Position | None = None
        self.pending_buy: PendingBuy | None = None
        self.cycles: list[Cycle] = []
        self.completed_markets = set()  # slugs where we've used our 1 cycle (buy 체결됨)
        self.recent_prices = deque(maxlen=600)
        self.realized_pnl = 0.0
        self.lock = threading.Lock()
        self.status_msg = "Idle"  # English-only UI
        self.last_update = 0.0
        # 라이브 모드 (메모리에만 저장, 디스크에 안 씀)
        self.live_mode = False
        self.private_key = ""
        self.clob_client = None
        self.wallet_address = ""
        self.wallet_balance = 0.0      # USDC 잔액
        self.wallet_allowance = 0.0    # USDC allowance
        self.funder = ""               # 폴리마켓 입금 주소 (Gnosis Safe)
        self.signature_type = 0        # 0=EOA, 2=POLY_GNOSIS_SAFE
        self.detect_label = ""         # 자동 감지된 라벨 ('EOA', 'Safe 0x...')

    def reset_session(self):
        with self.lock:
            self.cycles.clear()
            self.completed_markets.clear()
            self.recent_prices.clear()
            self.realized_pnl = 0.0
            self.position = None
            self.pending_buy = None
            self.current_slug = ""
            self.current_market_info = None
            self.status_msg = "Reset"


state = State()

# ─── Market Discovery ───────────────────────────────────────────────

async def find_current_btc_5m_market(session):
    """현재 진행중인 BTC 5분 마켓 찾기."""
    now = int(time.time())
    current_window = now - (now % 300)

    # 현재 + 다음 windows 시도
    for offset in [0, 300, 600]:
        ts = current_window + offset
        slug = f"btc-updown-5m-{ts}"
        url = f"{GAMMA_HOST}/events?slug={slug}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
            if not data or not isinstance(data, list):
                continue
            event = data[0]
            mkts = event.get('markets', [])
            if not mkts:
                continue
            m = mkts[0]
            if m.get('closed', False):
                continue
            tokens = m.get('clobTokenIds', '[]')
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            if len(tokens) < 2:
                continue

            end_ts = ts + 300
            if now >= end_ts:
                continue

            return {
                'slug': slug,
                'start_ts': ts,
                'end_ts': end_ts,
                'yes_token': tokens[0],   # YES = Up
                'no_token': tokens[1],    # NO = Down
                'question': m.get('question', ''),
            }
        except Exception as e:
            log.debug(f"market check {slug}: {e}")

    return None

# ─── Price Fetching ─────────────────────────────────────────────────

async def fetch_book_best(session, token_id):
    """최우선 호가 (best bid/ask) 조회."""
    url = f"{CLOB_HOST}/book?token_id={token_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            ob = await resp.json()
        bids = [(float(b['price']), float(b['size'])) for b in ob.get('bids', [])]
        asks = [(float(a['price']), float(a['size'])) for a in ob.get('asks', [])]
        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 1.0
        return best_bid, best_ask
    except Exception as e:
        log.debug(f"book fetch error: {e}")
        return 0.0, 1.0


POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.rpc.subquery.network/public",
]
_chainlink_rpc_idx = 0  # 마지막에 성공한 RPC 인덱스 캐시


async def fetch_chainlink_btc(session):
    """Polygon 체인링크 BTC/USD 오라클 (latestRoundData)에서 최신 가격 조회.
    Polymarket 정산 기준이 Chainlink data stream이므로 가장 정확한 reference.
    여러 public RPC에 fallback.
    """
    global _chainlink_rpc_idx
    AGGREGATOR = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
    payload = {
        "jsonrpc": "2.0", "method": "eth_call",
        "params": [{"to": AGGREGATOR, "data": "0xfeaf968c"}, "latest"],
        "id": 1,
    }
    n = len(POLYGON_RPCS)
    for attempt in range(n):
        rpc = POLYGON_RPCS[(_chainlink_rpc_idx + attempt) % n]
        try:
            async with session.post(rpc, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=3)) as resp:
                d = await resp.json()
            result = d.get('result', '')
            if not result or len(result) < 2 + 64*2:
                continue
            # latestRoundData() returns 5 × int256
            answer_hex = result[2 + 64 : 2 + 64*2]
            answer = int(answer_hex, 16)
            if answer >= 2**255:
                answer -= 2**256
            _chainlink_rpc_idx = (_chainlink_rpc_idx + attempt) % n
            return answer / 1e8
        except Exception as e:
            log.debug(f"chainlink {rpc}: {e}")
            continue
    return 0.0


async def fetch_binance_btc_price(session):
    """Binance 현재 BTCUSDT 가격."""
    try:
        url = f"{BINANCE_HOST}/api/v3/ticker/price?symbol=BTCUSDT"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
            d = await resp.json()
        return float(d['price'])
    except Exception as e:
        log.debug(f"binance fetch: {e}")
        return 0.0


async def fetch_bybit_btc_price(session):
    """Bybit 현재 BTCUSDT 가격 (spot)."""
    try:
        url = "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
            d = await resp.json()
        # response: {"result": {"list": [{"lastPrice": "..."}]}}
        lst = d.get('result', {}).get('list', [])
        if lst:
            return float(lst[0].get('lastPrice', 0))
    except Exception as e:
        log.debug(f"bybit fetch: {e}")
    return 0.0


async def fetch_binance_kline_open(session, start_ts):
    """주어진 unix 시각의 5분 kline open 가격 (마켓 시작가)."""
    try:
        url = (f"{BINANCE_HOST}/api/v3/klines"
               f"?symbol=BTCUSDT&interval=5m"
               f"&startTime={start_ts*1000}&limit=1")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            d = await resp.json()
        if isinstance(d, list) and d:
            # [open_time, open, high, low, close, ...]
            return float(d[0][1])
    except Exception as e:
        log.debug(f"kline fetch: {e}")
    return 0.0


async def fetch_all_prices(session, mkt_info):
    """폴리마켓 + 바이낸스 + Bybit + Chainlink 가격을 병렬 조회."""
    yes_task = fetch_book_best(session, mkt_info['yes_token'])
    no_task = fetch_book_best(session, mkt_info['no_token'])
    bin_task = fetch_binance_btc_price(session)
    bb_task = fetch_bybit_btc_price(session)
    cl_task = fetch_chainlink_btc(session)
    (up_bid, up_ask), (down_bid, down_ask), btc_bin, btc_bb, btc_cl = await asyncio.gather(
        yes_task, no_task, bin_task, bb_task, cl_task)
    return {
        'up_bid': up_bid, 'up_ask': up_ask,
        'down_bid': down_bid, 'down_ask': down_ask,
        'btc_price': btc_bin,           # 하위 호환 (Binance)
        'btc_binance': btc_bin,
        'btc_bybit': btc_bb,
        'btc_chainlink': btc_cl,
    }

# ─── Wallet Auto-Detection (EOA / Gnosis Safe) ──────────────────────

def _find_safes_for_eoa(eoa: str) -> list:
    """Safe Transaction Service에서 해당 EOA가 owner인 Polygon Safe들 조회.

    구글/이메일 로그인 폴리마켓 계정 → Magic.link EOA → Polymarket 생성 Gnosis Safe.
    이 Safe 주소를 찾아낸다.
    """
    import ssl, certifi
    ctx = ssl.create_default_context(cafile=certifi.where())
    url = f"https://safe-transaction-polygon.safe.global/api/v1/owners/{eoa}/safes/"
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=5, context=ctx) as r:
            data = json.loads(r.read())
        return data.get('safes', [])
    except Exception as e:
        log.warning(f"Safe service lookup failed: {e}")
        return []


def authenticate_and_detect(pk: str, manual_funder: str = ""):
    """프라이빗 키로 인증하고 EOA / Gnosis Safe 자동 감지.

    Returns dict with: client, sig_type, funder, balance, allowance, label, address, candidates
    또는 None (모든 시도 실패시).
    """
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
    from eth_account import Account

    if not pk.startswith('0x'):
        pk = '0x' + pk
    eoa = Account.from_key(pk).address
    log.info(f"[DETECT] EOA: {eoa}")

    # 후보 목록 만들기
    candidates = [('EOA', 0, '')]  # 0 = signature_type EOA
    if manual_funder:
        if not manual_funder.startswith('0x'):
            manual_funder = '0x' + manual_funder
        # 수동 funder는 두 타입 모두 시도 (POLY_PROXY=1, POLY_GNOSIS_SAFE=2)
        candidates.append((f'POLY_PROXY {manual_funder[:8]}…', 1, manual_funder))
        candidates.append((f'Safe {manual_funder[:8]}…', 2, manual_funder))
    else:
        # 자동 조회: Safe Transaction Service에서 owner=EOA인 Safe들
        safes = _find_safes_for_eoa(eoa)
        for s in safes:
            candidates.append((f'Safe {s[:8]}…', 2, s))
        log.info(f"[DETECT] {len(safes)} Safe(s) found for EOA via Safe Service")

    # 각 후보 시도하며 잔액 조회
    best = None
    detail = []
    for label, sig_type, funder in candidates:
        try:
            client = ClobClient(host=CLOB_HOST, chain_id=137, key=pk,
                                signature_type=sig_type,
                                funder=funder if funder else None)
            try:
                creds = client.derive_api_key()
            except Exception:
                creds = client.create_api_key()
            client.set_api_creds(creds)

            bal = client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=sig_type,
                ))
            balance = float(bal.get('balance', 0)) / 1e6
            allowance = float(bal.get('allowance', 0)) / 1e6
            log.info(f"[DETECT] {label}: balance=${balance:.2f} allowance=${allowance:.2f}")
            detail.append({'label': label, 'balance': balance, 'allowance': allowance,
                           'funder': funder, 'sig_type': sig_type})

            # 잔액이 가장 많은 후보 선택
            if best is None or balance > best['balance']:
                best = {
                    'client': client, 'sig_type': sig_type, 'funder': funder,
                    'balance': balance, 'allowance': allowance,
                    'label': label, 'address': eoa,
                }
        except Exception as e:
            log.debug(f"[DETECT] {label} failed: {e}")
            detail.append({'label': label, 'error': str(e)})

    if best is None:
        return None
    best['candidates'] = detail
    log.info(f"[DETECT] ✓ Selected: {best['label']} (${best['balance']:.2f})")
    return best


# ─── Live Order Placement (py-clob-client) ──────────────────────────

def place_real_order(side, token_id, price, shares):
    """라이브 모드: py-clob-client-v2로 실제 리밋 주문 placement.

    side: 'BUY' or 'SELL'
    Returns order_id 또는 빈 문자열 (실패시).
    """
    if not state.live_mode or not state.clob_client:
        log.warning(f"[REAL-{side}] live_mode={state.live_mode} client={state.clob_client is not None} — skipped")
        return ""
    try:
        from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY, SELL

        args = OrderArgs(
            token_id=token_id,
            price=round(price, 2),
            size=round(shares, 2),
            side=BUY if side == 'BUY' else SELL,
        )
        # neg_risk와 tick_size는 client가 자동 조회 (BTC 5m: tick=0.01, neg_risk=False)
        opts = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        log.info(f"[REAL-{side}] placing: price={round(price,2)} size={round(shares,2)} token={token_id[:12]}...")
        signed = state.clob_client.create_order(args, options=opts)
        resp = state.clob_client.post_order(signed, order_type=OrderType.GTC)
        log.info(f"[REAL-{side}] response: {resp}")
        oid = resp.get('orderID') or resp.get('orderId') or ''
        if oid:
            log.info(f"[REAL-{side}] ✓ order_id={oid}")
        else:
            err = resp.get('errorMsg') or resp.get('error') or 'no orderID'
            log.warning(f"[REAL-{side}] ✗ failed: {err} | resp: {resp}")
        return oid
    except Exception as e:
        import traceback
        log.error(f"[REAL-{side}] EXCEPTION: {e}")
        log.error(traceback.format_exc())
        return ""


def cancel_real_order(order_id):
    """라이브 모드: 실제 주문 취소."""
    if not state.live_mode or not state.clob_client or not order_id:
        return False
    try:
        state.clob_client.cancel(order_id=order_id)
        log.info(f"[REAL-CANCEL] {order_id[:10]}...")
        return True
    except Exception as e:
        log.error(f"[REAL-CANCEL] failed: {e}")
        return False


def market_sell_real(token_id, shares, current_bid):
    """시장가 매도 (FOK). 현재 bid 기준으로 즉시 체결 시도."""
    if not state.live_mode or not state.clob_client:
        log.warning(f"[REAL-MKT-SELL] live_mode={state.live_mode} client={state.clob_client is not None} — skipped")
        return ""
    try:
        from py_clob_client_v2.clob_types import MarketOrderArgs
        from py_clob_client_v2.order_builder.constants import SELL
        args = MarketOrderArgs(
            token_id=token_id,
            amount=round(shares, 2),     # 매도시 amount = shares
            side=SELL,
            price=max(0.01, round(current_bid - 0.01, 2)),
        )
        log.info(f"[REAL-MKT-SELL] placing: shares={round(shares,2)} min_price={max(0.01,round(current_bid-0.01,2))} token={token_id[:12]}...")
        signed = state.clob_client.create_market_order(args)
        from py_clob_client_v2.clob_types import OrderType
        resp = state.clob_client.post_order(signed, order_type=OrderType.FOK)
        log.info(f"[REAL-MKT-SELL] response: {resp}")
        oid = resp.get('orderID') or resp.get('orderId') or ''
        if oid:
            log.info(f"[REAL-MKT-SELL] ✓ order_id={oid}")
        else:
            log.warning(f"[REAL-MKT-SELL] ✗ no orderID: {resp}")
        return oid
    except Exception as e:
        import traceback
        log.error(f"[REAL-MKT-SELL] EXCEPTION: {e}")
        log.error(traceback.format_exc())
        return ""


def market_buy_real(token_id, usd_amount, max_price):
    """시장가 매수 (FOK). USD 금액 기준."""
    if not state.live_mode or not state.clob_client:
        log.warning(f"[REAL-MKT-BUY] live_mode={state.live_mode} client={state.clob_client is not None} — skipped")
        return ""
    try:
        from py_clob_client_v2.clob_types import MarketOrderArgs, OrderType
        from py_clob_client_v2.order_builder.constants import BUY
        args = MarketOrderArgs(
            token_id=token_id,
            amount=round(usd_amount, 2),       # 매수시 amount = USD 금액
            side=BUY,
            price=round(max_price, 2),
        )
        log.info(f"[REAL-MKT-BUY] placing: amount=${round(usd_amount,2)} max_price={round(max_price,2)} token={token_id[:12]}...")
        signed = state.clob_client.create_market_order(args)
        resp = state.clob_client.post_order(signed, order_type=OrderType.FOK)
        log.info(f"[REAL-MKT-BUY] response: {resp}")
        oid = resp.get('orderID') or resp.get('orderId') or ''
        if oid:
            log.info(f"[REAL-MKT-BUY] ✓ order_id={oid}")
        else:
            log.warning(f"[REAL-MKT-BUY] ✗ no orderID: {resp}")
        return oid
    except Exception as e:
        import traceback
        log.error(f"[REAL-MKT-BUY] EXCEPTION: {e}")
        log.error(traceback.format_exc())
        return ""


def get_token_id(side, mkt):
    return mkt['yes_token'] if side == 'up' else mkt['no_token']


def check_max_cycles_and_stop(cfg):
    """max_cycles_per_session 도달시 봇 자동 정지."""
    limit = cfg.get('max_cycles_per_session', 0)
    if limit > 0 and len(state.cycles) >= limit:
        log.info(f"[MAX-CYCLES] {len(state.cycles)} cycles done (limit {limit}) → stopping bot")
        state.status_msg = f"Max cycles reached ({len(state.cycles)}/{limit}) — stopped"
        state.running = False
        return True
    return False


# ─── Strategy Logic ─────────────────────────────────────────────────

def get_side_prices(prices, side):
    """포지션 방향의 (best_bid, best_ask) 반환."""
    if side == 'up':
        return prices['up_bid'], prices['up_ask']
    return prices['down_bid'], prices['down_ask']


def should_place_limit_buy(prices, cfg):
    """매수 진입 조건. entry_mode에 따라 다름.

    'low_target' (기본 10c±tol):
        Up/Down 중 ask가 entry_price ± tolerance 범위면 진입. 둘 다면 None (드뭄).
    'high_lead' (70c+ 우위한쪽):
        Up/Down 중 ask >= entry_price 인 사이드 매수 (둘 다면 더 높은 쪽).
    """
    mode = cfg.get('entry_mode', 'low_target')

    if mode == 'high_lead':
        target = cfg['entry_price']
        max_entry = cfg.get('max_entry_price', 0.99)  # 기본 0.99 (clamp 한계)
        candidates = []
        # ask가 [target, max_entry] 범위 안에 있는 사이드만 후보
        if target <= prices['up_ask'] <= max_entry:
            candidates.append(('up', prices['up_ask']))
        if target <= prices['down_ask'] <= max_entry:
            candidates.append(('down', prices['down_ask']))
        if not candidates:
            return None
        # 둘 다 만족하면 더 비싼 (= 더 우위한) 쪽 선택
        return max(candidates, key=lambda x: x[1])[0]

    # default: low_target
    target = cfg['entry_price']
    tol = cfg['entry_tolerance']
    lo = target - tol
    hi = target + tol
    if lo <= prices['up_ask'] <= hi:
        return 'up'
    if lo <= prices['down_ask'] <= hi:
        return 'down'
    return None


def check_limit_buy_fill(pb: 'PendingBuy', prices):
    """리밋 매수 체결 시뮬레이션 (메이커 가정).

    리밋 매수가 P 일 때, best_ask <= P 가 되면 누군가가 우리 호가까지 내려와서 체결.
    체결가는 우리 limit price (P) 그대로.
    """
    _, ask = get_side_prices(prices, pb.side)
    if 0 < ask <= pb.limit_price:
        return pb.limit_price
    return None


def check_limit_sell_fill(pos: 'Position', prices):
    """리밋 매도 체결 시뮬레이션.

    리밋 매도가 P 일 때, best_bid >= P 가 되면 체결된다.
    P >= 1.0 이면 절대 체결되지 않음 (만료까지 hold).
    """
    if pos.sell_limit_price >= 1.0:
        return None  # hold to expiry
    bid, _ = get_side_prices(prices, pos.side)
    if bid >= pos.sell_limit_price:
        return max(pos.sell_limit_price, bid)
    return None


def place_limit_buy(side, prices, cfg, slug, mkt):
    """리밋 매수 주문 placement. 라이브 모드면 실제 주문도 전송.

    high_lead 모드: limit_price = 현재 ask (즉시 체결되는 가격)
    low_target 모드: limit_price = entry_price (10c)
    """
    if cfg.get('entry_mode', 'low_target') == 'high_lead':
        # 우위 사이드의 현재 ask 가격으로 매수 (즉시 체결되는 taker 동작)
        ask = prices['up_ask'] if side == 'up' else prices['down_ask']
        limit_price = ask
    else:
        limit_price = cfg['entry_price']
    # 폴리마켓 CLOB 한계 (0.01~0.99) 안으로 clamp
    limit_price = max(0.02, min(0.99, limit_price))
    shares = cfg['bet_size_usd'] / limit_price
    cycle_id = f"{slug}_{int(time.time()*1000)}"

    order_id = ""
    if state.live_mode:
        order_id = place_real_order('BUY', get_token_id(side, mkt), limit_price, shares)
        if not order_id:
            # 실거래 매수 실패 → SIM 포지션도 만들지 않음 (이전 버그: SIM이 가짜로 진행하던 것 차단)
            log.warning(f"[LIVE-LIMIT-BUY] real order failed — skipping cycle (no virtual position)")
            return None

    pb = PendingBuy(
        side=side, limit_price=limit_price, shares=shares,
        placed_at=time.time(), cycle_id=cycle_id, order_id=order_id,
    )
    mode = "LIVE" if state.live_mode else "SIM"
    log.info(f"[{mode}-LIMIT-BUY] {side.upper()} {shares:.2f}sh @ {limit_price:.3f} | {slug}")
    append_csv({
        'timestamp_iso': datetime.now(timezone.utc).isoformat(),
        'unix_ts': int(time.time()),
        'event': 'LIMIT_BUY_PLACED',
        'slug': slug, 'side': side,
        'shares': f"{shares:.4f}", 'price': f"{limit_price:.4f}",
        'cost': f"{cfg['bet_size_usd']:.4f}",
        'pnl': '', 'cycle_id': cycle_id, 'note': f"current_ask={prices.get('up_ask' if side=='up' else 'down_ask',0):.4f}",
    })
    return pb


def fill_limit_buy(pb: PendingBuy, fill_price, cfg, slug) -> Position:
    """리밋 매수 체결 처리."""
    cost = pb.shares * fill_price
    pos = Position(
        side=pb.side, shares=pb.shares, entry_price=fill_price,
        cost=cost, entry_time=time.time(), cycle_id=pb.cycle_id,
    )
    log.info(f"[BUY-FILLED] {pb.side.upper()} {pb.shares:.2f}sh @ {fill_price:.3f} "
             f"(cost ${cost:.2f}) | {slug}")
    append_csv({
        'timestamp_iso': datetime.now(timezone.utc).isoformat(),
        'unix_ts': int(time.time()),
        'event': 'BUY_FILLED',
        'slug': slug, 'side': pb.side,
        'shares': f"{pb.shares:.4f}", 'price': f"{fill_price:.4f}",
        'cost': f"{cost:.4f}", 'pnl': '',
        'cycle_id': pb.cycle_id, 'note': 'maker',
    })
    return pos


def place_limit_sell(pos: Position, cfg, slug, mkt):
    """리밋 매도 주문 placement. 라이브 모드면 실제 주문도 전송.

    TP >= 1.0 이면 폴리마켓이 호가를 받아주지 않으므로 매도 주문 생략 →
    그냥 마켓 만료까지 hold (만료시 EXPIRY 처리로 정산).
    그 외 가격은 0.01~0.99 안으로 clamp.
    """
    requested_tp = cfg['tp_price']
    pos.sell_placed_at = time.time()

    if requested_tp >= 1.0:
        # 매도 주문 생략 — 만료까지 hold
        pos.sell_limit_price = 1.0
        log.info(f"[HOLD-TO-EXPIRY] TP={requested_tp:.2f} ≥ 1.0 → no sell order, will resolve at expiry")
        append_csv({
            'timestamp_iso': datetime.now(timezone.utc).isoformat(),
            'unix_ts': int(time.time()),
            'event': 'HOLD_TO_EXPIRY',
            'slug': slug, 'side': pos.side,
            'shares': f"{pos.shares:.4f}", 'price': '',
            'cost': '', 'pnl': '',
            'cycle_id': pos.cycle_id, 'note': f"TP={requested_tp:.2f}",
        })
        return

    pos.sell_limit_price = max(0.02, min(0.99, requested_tp))
    if state.live_mode:
        pos.sell_order_id = place_real_order(
            'SELL', get_token_id(pos.side, mkt), pos.sell_limit_price, pos.shares)
    mode = "LIVE" if state.live_mode else "SIM"
    log.info(f"[{mode}-LIMIT-SELL] {pos.side.upper()} {pos.shares:.2f}sh @ {pos.sell_limit_price:.3f} | {slug}")
    append_csv({
        'timestamp_iso': datetime.now(timezone.utc).isoformat(),
        'unix_ts': int(time.time()),
        'event': 'LIMIT_SELL_PLACED',
        'slug': slug, 'side': pos.side,
        'shares': f"{pos.shares:.4f}", 'price': f"{pos.sell_limit_price:.4f}",
        'cost': '', 'pnl': '',
        'cycle_id': pos.cycle_id, 'note': f"TP={pos.sell_limit_price:.3f}",
    })


def execute_sell(pos: Position, fill_price, reason, cfg, slug, is_maker=False):
    """매도 체결 처리 (TP 리밋 / SL / TIMEOUT 시장가 / EXPIRY)."""
    revenue = pos.shares * fill_price
    fee_rate = cfg.get('maker_fee', 0.0) if is_maker else cfg['taker_fee']
    fee = revenue * fee_rate
    pnl = revenue - pos.cost - fee

    log.info(f"[SELL/{reason}] {pos.side.upper()} {pos.shares:.2f}sh @ {fill_price:.3f} "
             f"| rev ${revenue:.2f} fee ${fee:.2f} pnl ${pnl:+.2f}"
             f" ({'maker' if is_maker else 'taker'})")

    cycle = Cycle(
        cycle_id=pos.cycle_id, slug=slug, side=pos.side,
        entry_price=pos.entry_price, entry_time=pos.entry_time,
        shares=pos.shares, cost=pos.cost,
        exit_price=fill_price, exit_time=time.time(),
        exit_reason=reason, pnl=pnl, fee=fee,
    )

    append_csv({
        'timestamp_iso': datetime.now(timezone.utc).isoformat(),
        'unix_ts': int(time.time()),
        'event': f'SELL_{reason}',
        'slug': slug, 'side': pos.side,
        'shares': f"{pos.shares:.4f}", 'price': f"{fill_price:.4f}",
        'cost': f"{revenue:.4f}", 'pnl': f"{pnl:.4f}",
        'cycle_id': pos.cycle_id, 'note': 'maker' if is_maker else 'taker',
    })
    return cycle

# ─── Main Strategy Loop ─────────────────────────────────────────────

async def strategy_loop():
    state.status_msg = "Searching for current live market..."
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
        while state.running:
            try:
                # 1) 마켓 확인 / 갱신
                if (state.current_market_info is None
                        or time.time() >= state.current_market_info['end_ts']):
                    if state.current_market_info:
                        # Market expired - close any open position
                        prices = await fetch_all_prices(session, state.current_market_info)
                        slug = state.current_market_info['slug']
                        if state.position:
                            bid, _ = get_side_prices(prices, state.position.side)
                            if state.live_mode:
                                if state.position.sell_order_id:
                                    cancel_real_order(state.position.sell_order_id)
                                market_sell_real(
                                    get_token_id(state.position.side, state.current_market_info),
                                    state.position.shares, bid)
                            cycle = execute_sell(state.position, bid, 'EXPIRY',
                                                 state.config, slug)
                            with state.lock:
                                state.cycles.append(cycle)
                                state.realized_pnl += cycle.pnl
                                state.position = None
                            state.status_msg = f"Market expired, closed at {bid:.3f}"
                            if check_max_cycles_and_stop(state.config):
                                break
                        if state.pending_buy:
                            if state.live_mode and state.pending_buy.order_id:
                                cancel_real_order(state.pending_buy.order_id)
                            log.info(f"[BUY-CANCEL] limit buy expired without fill | {slug}")
                            append_csv({
                                'timestamp_iso': datetime.now(timezone.utc).isoformat(),
                                'unix_ts': int(time.time()),
                                'event': 'BUY_CANCEL_EXPIRY',
                                'slug': slug, 'side': state.pending_buy.side,
                                'shares': f"{state.pending_buy.shares:.4f}",
                                'price': f"{state.pending_buy.limit_price:.4f}",
                                'cost': '', 'pnl': '',
                                'cycle_id': state.pending_buy.cycle_id,
                                'note': 'market expired',
                            })
                            with state.lock:
                                state.pending_buy = None

                    new_mkt = await find_current_btc_5m_market(session)
                    if new_mkt is None:
                        state.status_msg = "Waiting for next market..."
                        await asyncio.sleep(2)
                        continue

                    if new_mkt['slug'] != state.current_slug:
                        # 새 마켓 시작가: Binance 5m kline open + 시점 Chainlink (참고용)
                        btc_open, cl_now = await asyncio.gather(
                            fetch_binance_kline_open(session, new_mkt['start_ts']),
                            fetch_chainlink_btc(session),
                        )
                        new_mkt['btc_open'] = btc_open
                        new_mkt['btc_chainlink_at_start'] = cl_now
                        log.info(f"[MARKET] Switch -> {new_mkt['slug']} "
                                 f"(ends in {int(new_mkt['end_ts'] - time.time())}s, "
                                 f"BTC open ${btc_open:,.2f}, CL ${cl_now:,.2f})")
                        state.current_slug = new_mkt['slug']
                        state.current_market_info = new_mkt

                mkt = state.current_market_info
                cfg = state.config
                slug = mkt['slug']
                now = time.time()
                elapsed = now - mkt['start_ts']
                duration = mkt['end_ts'] - mkt['start_ts']
                progress = elapsed / duration

                # 2) 가격 조회 (폴리마켓 + 바이낸스 + Chainlink 병렬)
                prices = await fetch_all_prices(session, mkt)
                btc_open = mkt.get('btc_open', 0.0)
                cl_at_start = mkt.get('btc_chainlink_at_start', 0.0)
                btc_bin = prices['btc_binance']
                btc_bb = prices['btc_bybit']
                btc_cl = prices['btc_chainlink']
                bin_delta = btc_bin - btc_open if (btc_open > 0 and btc_bin > 0) else 0.0
                bb_delta = btc_bb - btc_open if (btc_open > 0 and btc_bb > 0) else 0.0
                cl_delta = btc_cl - cl_at_start if (cl_at_start > 0 and btc_cl > 0) else 0.0
                with state.lock:
                    state.recent_prices.append({
                        'ts': now, 'progress': progress,
                        'up_ask': prices['up_ask'], 'up_bid': prices['up_bid'],
                        'down_ask': prices['down_ask'], 'down_bid': prices['down_bid'],
                        'btc_price': btc_bin,  # 하위 호환
                        'btc_open': btc_open,
                        'btc_delta': bin_delta,
                        'btc_binance': btc_bin,
                        'btc_bybit': btc_bb,
                        'btc_bybit_delta': bb_delta,
                        'btc_chainlink': btc_cl,
                        'btc_chainlink_at_start': cl_at_start,
                        'btc_chainlink_delta': cl_delta,
                    })
                    state.last_update = now

                # ─── State Machine ───────────────────────────────
                # IDLE → PENDING_BUY → HOLDING → DONE

                # 3) HOLDING: 리밋 매도 체결 / SL / 타임아웃 체크
                if state.position:
                    pos = state.position
                    bid, _ = get_side_prices(prices, pos.side)

                    # SL check (priority)
                    if 0 < bid <= cfg['sl_price']:
                        if state.live_mode and pos.sell_order_id:
                            cancel_real_order(pos.sell_order_id)
                            market_sell_real(get_token_id(pos.side, mkt), pos.shares, bid)
                        cycle = execute_sell(pos, bid, 'SL', cfg, slug, is_maker=False)
                        with state.lock:
                            state.cycles.append(cycle)
                            state.realized_pnl += cycle.pnl
                            state.position = None
                        state.status_msg = f"SL hit @ {bid:.3f}"
                        if check_max_cycles_and_stop(cfg):
                            break
                    else:
                        if cfg.get('sell_order_type', 'limit') == 'market':
                            # MARKET sell when bid reaches TP
                            if bid >= cfg['tp_price']:
                                if state.live_mode:
                                    market_sell_real(get_token_id(pos.side, mkt), pos.shares, bid)
                                cycle = execute_sell(pos, bid, 'TP_MKT', cfg, slug, is_maker=False)
                                with state.lock:
                                    state.cycles.append(cycle)
                                    state.realized_pnl += cycle.pnl
                                    state.position = None
                                state.status_msg = f"TP market sold @ {bid:.3f}"
                                if check_max_cycles_and_stop(cfg):
                                    break
                            else:
                                held_sec = now - pos.sell_placed_at
                                state.status_msg = (f"Holding {pos.side.upper()} {pos.shares:.1f}sh "
                                                    f"@ {pos.entry_price:.3f} → MKT TP {cfg['tp_price']:.3f} "
                                                    f"(bid {bid:.3f}, held {held_sec:.0f}s)")
                        else:
                            sell_fill = check_limit_sell_fill(pos, prices)
                            if sell_fill is not None:
                                cycle = execute_sell(pos, sell_fill, 'TP', cfg, slug, is_maker=True)
                                with state.lock:
                                    state.cycles.append(cycle)
                                    state.realized_pnl += cycle.pnl
                                    state.position = None
                                state.status_msg = f"TP filled @ {sell_fill:.3f}"
                                if check_max_cycles_and_stop(cfg):
                                    break
                            else:
                                held_sec = now - pos.sell_placed_at
                                state.status_msg = (f"Holding {pos.side.upper()} {pos.shares:.1f}sh "
                                                    f"@ {pos.entry_price:.3f} → Limit TP {pos.sell_limit_price:.3f} "
                                                    f"(bid {bid:.3f}, held {held_sec:.0f}s)")

                # 4) PENDING_BUY: 리밋 매수 체결 체크
                elif state.pending_buy:
                    pb = state.pending_buy
                    fill = check_limit_buy_fill(pb, prices)
                    if fill is not None:
                        pos = fill_limit_buy(pb, fill, cfg, slug)
                        if cfg.get('sell_order_type', 'limit') == 'limit':
                            place_limit_sell(pos, cfg, slug, mkt)
                        else:
                            pos.sell_limit_price = cfg['tp_price']
                            pos.sell_placed_at = time.time()
                        with state.lock:
                            state.position = pos
                            state.pending_buy = None
                            state.completed_markets.add(slug)
                        state.status_msg = f"BUY filled @ {fill:.3f}, sell target {pos.sell_limit_price:.3f}"
                    else:
                        _, ask = get_side_prices(prices, pb.side)
                        state.status_msg = (f"Pending {pb.side.upper()} limit buy @ {pb.limit_price:.3f} "
                                            f"(ask {ask:.3f}, market {progress*100:.0f}%)")

                # 5) IDLE: 두 조건 모두 만족할 때만 매수 시도
                #   A) progress < tradeable_pct  (early cutoff)
                #   B) remaining ≤ buy_when_remaining_below_pct  (delay until late if set)
                remaining_sec = mkt['end_ts'] - now
                remaining_pct = 1.0 - progress
                in_tradeable = progress < cfg['tradeable_pct']
                in_remain_window = remaining_pct <= cfg.get('buy_when_remaining_below_pct', 1.0)
                if (in_tradeable and in_remain_window and slug not in state.completed_markets):
                    side = should_place_limit_buy(prices, cfg)
                    if side:
                        if cfg.get('buy_order_type', 'limit') == 'market':
                            # MARKET 매수: 즉시 시장가 (현재 ask가 entry_price 근처일 때만 트리거됨)
                            ask_now = prices['up_ask'] if side == 'up' else prices['down_ask']
                            shares = cfg['bet_size_usd'] / ask_now
                            cycle_id = f"{slug}_{int(time.time()*1000)}"
                            if state.live_mode:
                                # 슬리피지 한계: entry mode에 따라
                                max_p = ask_now + 0.02  # 현재 ask + 슬리피지 여유
                                real_oid = market_buy_real(get_token_id(side, mkt), cfg['bet_size_usd'], max_p)
                                if not real_oid:
                                    log.warning(f"[MKT-BUY] real order failed — skipping market")
                                    with state.lock:
                                        state.completed_markets.add(slug)
                                    state.status_msg = "Real MKT BUY failed — skipping this market"
                                    continue
                            pos = Position(
                                side=side, shares=shares, entry_price=ask_now,
                                cost=cfg['bet_size_usd'], entry_time=time.time(),
                                cycle_id=cycle_id,
                            )
                            log.info(f"[MKT-BUY] {side.upper()} ${cfg['bet_size_usd']:.2f} @ {ask_now:.3f} | {slug}")
                            append_csv({
                                'timestamp_iso': datetime.now(timezone.utc).isoformat(),
                                'unix_ts': int(time.time()), 'event': 'MKT_BUY',
                                'slug': slug, 'side': side,
                                'shares': f"{shares:.4f}", 'price': f"{ask_now:.4f}",
                                'cost': f"{cfg['bet_size_usd']:.4f}",
                                'pnl': '', 'cycle_id': cycle_id, 'note': 'market',
                            })
                            if cfg.get('sell_order_type', 'limit') == 'limit':
                                place_limit_sell(pos, cfg, slug, mkt)
                            else:
                                pos.sell_limit_price = cfg['tp_price']
                                pos.sell_placed_at = time.time()
                            with state.lock:
                                state.position = pos
                                state.completed_markets.add(slug)
                            state.status_msg = f"Market BUY filled @ {ask_now:.3f}, monitoring TP"
                        else:
                            pb = place_limit_buy(side, prices, cfg, slug, mkt)
                            if pb is None:
                                # 실거래 매수 실패 — 다음 마켓 대기
                                with state.lock:
                                    state.completed_markets.add(slug)
                                state.status_msg = "Real BUY failed — skipping this market"
                            else:
                                with state.lock:
                                    state.pending_buy = pb
                                state.status_msg = f"Limit BUY placed: {side.upper()} @ {pb.limit_price:.3f}"
                    else:
                        state.status_msg = (f"Monitoring entry ({progress*100:.0f}% elapsed, "
                                            f"{remaining_pct*100:.0f}% left) "
                                            f"Up={prices['up_ask']:.3f} "
                                            f"Down={prices['down_ask']:.3f}")
                else:
                    if slug in state.completed_markets:
                        state.status_msg = (f"Cycle done, waiting for next market ({int(remaining_sec)}s left)")
                    elif not in_tradeable:
                        state.status_msg = (f"Past tradeable window ({progress*100:.0f}% > "
                                            f"{cfg['tradeable_pct']*100:.0f}%) — waiting for next market")
                    elif not in_remain_window:
                        thr = cfg.get('buy_when_remaining_below_pct', 1.0) * 100
                        state.status_msg = (f"Waiting for entry trigger "
                                            f"({remaining_pct*100:.0f}% left, need ≤{thr:.0f}%)")

            except Exception as e:
                log.exception(f"loop error: {e}")
                state.status_msg = f"Error: {e}"

            await asyncio.sleep(state.config['poll_interval_sec'])

    state.status_msg = "Stopped"
    log.info("Strategy loop stopped")

# ─── Async Runner Thread ────────────────────────────────────────────

_loop_thread = None
_async_loop = None

def start_strategy():
    global _loop_thread, _async_loop
    if state.running:
        return False
    state.running = True

    def run():
        global _async_loop
        _async_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_async_loop)
        try:
            _async_loop.run_until_complete(strategy_loop())
        except Exception as e:
            log.exception(f"thread error: {e}")
        finally:
            _async_loop.close()

    _loop_thread = threading.Thread(target=run, daemon=True)
    _loop_thread.start()
    return True


def stop_strategy():
    state.running = False
    return True

# ─── HTTP Server ────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get('Content-Length', '0') or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n).decode('utf-8')
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ('/', '/index.html', '/dashboard.html'):
            if not DASHBOARD_HTML.exists():
                self.send_error(404, "dashboard.html not found")
                return
            with open(DASHBOARD_HTML, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif url.path == '/api/status':
            with state.lock:
                cycles = [asdict(c) for c in state.cycles[-50:]]
                prices = list(state.recent_prices)[-300:]
                pos = asdict(state.position) if state.position else None
                pb = asdict(state.pending_buy) if state.pending_buy else None
                resp = {
                    'running': state.running,
                    'status_msg': state.status_msg,
                    'current_slug': state.current_slug,
                    'market_info': state.current_market_info,
                    'now_ts': time.time(),
                    'config': state.config,
                    'position': pos,
                    'pending_buy': pb,
                    'cycles': cycles,
                    'realized_pnl': state.realized_pnl,
                    'recent_prices': prices,
                    'completed_markets': len(state.completed_markets),
                    'last_update': state.last_update,
                    'live_mode': state.live_mode,
                    'wallet_address': state.wallet_address,
                    'wallet_balance': state.wallet_balance,
                    'wallet_allowance': state.wallet_allowance,
                    'funder': state.funder,
                    'signature_type': state.signature_type,
                    'detect_label': state.detect_label,
                    'authenticated': bool(state.private_key),
                }
            self._send_json(resp)

        elif url.path == '/api/config':
            self._send_json(state.config)

        # /api/trades, /api/history_dates 제거됨
        # 거래 내역은 브라우저 localStorage에 저장됨 (dashboard.html이 직접 관리)
        else:
            self.send_error(404)

    def do_POST(self):
        url = urlparse(self.path)
        body = self._read_body()

        if url.path == '/api/start':
            # body: {"live": bool, "private_key": "0x..." (실전 모드일 때만 필요)}
            want_live = bool(body.get('live', False))
            if want_live:
                pk = (body.get('private_key') or '').strip()
                if not pk and not state.private_key:
                    self._send_json({'ok': False,
                                     'error': '실전 모드는 프라이빗 키가 필요합니다'},
                                    status=400)
                    return
                # 새 키나 새 funder가 들어왔으면 인증
                manual_funder = (body.get('funder') or '').strip()
                need_reauth = (pk and (pk != state.private_key or manual_funder != state.funder))
                if need_reauth:
                    try:
                        result = authenticate_and_detect(pk, manual_funder)
                        if result is None:
                            self._send_json({'ok': False,
                                             'error': '인증/잔액 조회 실패. 키를 다시 확인하세요.'}, status=400)
                            return
                        state.private_key = pk if pk.startswith('0x') else '0x' + pk
                        state.clob_client = result['client']
                        state.wallet_address = result['address']
                        state.funder = result['funder']
                        state.signature_type = result['sig_type']
                        state.wallet_balance = result['balance']
                        state.wallet_allowance = result['allowance']
                        state.detect_label = result['label']
                    except Exception as e:
                        log.error(f"auth failed: {e}")
                        self._send_json({'ok': False,
                                         'error': f'인증 실패: {e}'}, status=400)
                        return
            state.live_mode = want_live
            ok = start_strategy()
            self._send_json({'ok': ok, 'running': state.running,
                             'live_mode': state.live_mode,
                             'wallet_address': state.wallet_address})

        elif url.path == '/api/stop':
            ok = stop_strategy()
            self._send_json({'ok': ok, 'running': state.running})

        elif url.path == '/api/config':
            with state.lock:
                for k, v in body.items():
                    if k in DEFAULT_CONFIG:
                        try:
                            state.config[k] = type(DEFAULT_CONFIG[k])(v)
                        except Exception:
                            pass
                save_config(state.config)
            self._send_json({'ok': True, 'config': state.config})

        elif url.path == '/api/reset':
            state.reset_session()
            self._send_json({'ok': True})

        elif url.path == '/api/auth':
            # 프라이빗 키 등록 + 자동 EOA/Safe 감지 (메모리 전용)
            pk = (body.get('private_key') or '').strip()
            manual_funder = (body.get('funder') or '').strip()
            if not pk:
                # 인증 해제
                state.private_key = ""
                state.clob_client = None
                state.wallet_address = ""
                state.funder = ""
                state.detect_label = ""
                state.signature_type = 0
                state.wallet_balance = 0.0
                state.wallet_allowance = 0.0
                state.live_mode = False
                self._send_json({'ok': True, 'authenticated': False})
                return
            try:
                result = authenticate_and_detect(pk, manual_funder)
                if result is None:
                    self._send_json({'ok': False,
                                     'error': '인증/잔액 조회 실패'}, status=400)
                    return
                state.private_key = pk if pk.startswith('0x') else '0x' + pk
                state.clob_client = result['client']
                state.wallet_address = result['address']
                state.funder = result['funder']
                state.signature_type = result['sig_type']
                state.wallet_balance = result['balance']
                state.wallet_allowance = result['allowance']
                state.detect_label = result['label']
                self._send_json({'ok': True, 'authenticated': True,
                                 'wallet_address': state.wallet_address,
                                 'funder': state.funder,
                                 'signature_type': state.signature_type,
                                 'balance': state.wallet_balance,
                                 'allowance': state.wallet_allowance,
                                 'detect_label': state.detect_label,
                                 'candidates': result.get('candidates', [])})
            except Exception as e:
                log.error(f"auth failed: {e}")
                self._send_json({'ok': False, 'error': str(e)}, status=400)

        elif url.path == '/api/test_order':
            # 진단용: 작은 테스트 주문 1건 시도. body: {"side": "BUY"|"SELL", "price": 0.05, "size": 5}
            if not state.clob_client:
                self._send_json({'ok': False,
                                 'error': '인증 안됨 — 먼저 Check Balance 누르세요'},
                                status=400)
                return
            if not state.current_market_info:
                self._send_json({'ok': False,
                                 'error': '활성 마켓 없음 — 봇을 SIM이나 Live로 시작해서 마켓이 잡히게 하세요'},
                                status=400)
                return
            try:
                from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
                from py_clob_client_v2.order_builder.constants import BUY, SELL

                side_str = (body.get('side') or 'BUY').upper()
                price = float(body.get('price', 0.05))
                size = float(body.get('size', 5.0))
                token_id = state.current_market_info['no_token']

                args = OrderArgs(
                    token_id=token_id,
                    price=round(price, 2), size=round(size, 2),
                    side=BUY if side_str == 'BUY' else SELL,
                )
                opts = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
                log.info(f"[TEST-ORDER] {side_str} {size}@{price} token={token_id[:12]}...")
                signed = state.clob_client.create_order(args, options=opts)
                resp = state.clob_client.post_order(signed, order_type=OrderType.GTC)
                log.info(f"[TEST-ORDER] response: {resp}")
                oid = resp.get('orderID') or resp.get('orderId') or ''
                self._send_json({
                    'ok': bool(oid),
                    'order_id': oid,
                    'response': resp,
                    'side': side_str,
                    'price': price, 'size': size,
                    'token_id': token_id,
                })
            except Exception as e:
                import traceback
                log.error(f"[TEST-ORDER] EXCEPTION: {e}")
                tb = traceback.format_exc()
                log.error(tb)
                self._send_json({'ok': False, 'error': str(e), 'traceback': tb}, status=500)

        elif url.path == '/api/live_mode':
            want = bool(body.get('live', False))
            if want and not state.private_key:
                self._send_json({'ok': False,
                                 'error': '프라이빗 키 인증이 먼저 필요합니다'}, status=400)
                return
            state.live_mode = want
            log.info(f"[LIVE_MODE] {'ON' if want else 'OFF'}")
            self._send_json({'ok': True, 'live_mode': state.live_mode})

        else:
            self.send_error(404)


def run_server(port):
    srv = HTTPServer(('127.0.0.1', port), Handler)
    log.info(f"Dashboard: http://localhost:{port}")
    srv.serve_forever()


def main():
    port = state.config.get('http_port', 8765)
    log.info("=== BTC 5분 마켓 10c→15c 전략 ===")
    log.info(f"Config: bet=${state.config['bet_size_usd']} "
             f"entry={state.config['entry_price']} "
             f"TP={state.config['tp_price']} "
             f"SL={state.config['sl_price']} "
             f"tradeable={state.config['tradeable_pct']*100:.0f}%")

    # 1초 후 브라우저 자동 실행
    if '--no-browser' not in sys.argv:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f'http://localhost:{port}')).start()

    try:
        run_server(port)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        state.running = False
        sys.exit(0)


if __name__ == '__main__':
    main()
