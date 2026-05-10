"""USDC/USDT/WETH/WBTC/MATIC 입금 폴링 워커.

DEPOSIT_ADDRESS 로 들어오는 ERC20 transfer 모두 감지 →
Chainlink 오라클로 USD 환산 → 토큰 부여 (1¢ = 1 token).

ETH (mainnet 네이티브) 는 별도 체인이라 미지원. 사용자는 Polygon에서 WETH로 입금.
"""
import asyncio
import logging
import ssl
from typing import Optional

import aiohttp
import certifi
from sqlmodel import Session, select

from . import config
from .db import engine
from .models import User, Deposit, IndexerCursor
from .tokens import credit


log = logging.getLogger("deposits")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# ─── Helpers ────────────────────────────────────────────────────────

def _addr_to_topic(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def _topic_to_addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


async def _rpc_call(session: aiohttp.ClientSession, method: str, params: list) -> dict:
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    last_err = None
    for rpc in config.POLYGON_RPCS:
        try:
            async with session.post(rpc, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json()
            if "error" in d:
                last_err = d["error"]; continue
            return d
        except Exception as e:
            last_err = e; continue
    raise RuntimeError(f"RPCs failed: {last_err}")


async def _get_latest_block(session: aiohttp.ClientSession) -> int:
    d = await _rpc_call(session, "eth_blockNumber", [])
    return int(d["result"], 16)


async def _get_logs(session: aiohttp.ClientSession,
                    contract: str, from_block: int, to_block: int) -> list:
    params = [{
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
        "address": contract,
        "topics": [TRANSFER_TOPIC, None, _addr_to_topic(config.DEPOSIT_ADDRESS)],
    }]
    d = await _rpc_call(session, "eth_getLogs", params)
    return d.get("result", [])


async def _get_chainlink_price(session: aiohttp.ClientSession,
                                aggregator: str, decimals: int = 8) -> float:
    """Chainlink AggregatorV3Interface.latestRoundData() 호출 → USD 가격."""
    payload = {
        "jsonrpc": "2.0", "method": "eth_call",
        "params": [{"to": aggregator, "data": "0xfeaf968c"}, "latest"], "id": 1,
    }
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    for rpc in config.POLYGON_RPCS:
        try:
            async with session.post(rpc, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=5)) as r:
                d = await r.json()
            res = d.get("result", "")
            if not res or len(res) < 2 + 64*2:
                continue
            answer_hex = res[2 + 64 : 2 + 64*2]
            answer = int(answer_hex, 16)
            if answer >= 2**255: answer -= 2**256
            return answer / (10 ** decimals)
        except Exception:
            continue
    return 0.0


# ─── Per-token processing ─────────────────────────────────────────

def _process_log(db_session: Session, token_cfg: dict,
                 log_entry: dict, usd_price: float) -> bool:
    """입금 로그 1건 처리 → 사용자 토큰 부여."""
    tx_hash = log_entry["transactionHash"]
    if db_session.exec(select(Deposit).where(Deposit.tx_hash == tx_hash)).first():
        return False  # 이미 처리됨

    from_addr = _topic_to_addr(log_entry["topics"][1])
    amount_raw = int(log_entry["data"], 16)
    amount_token = amount_raw / (10 ** token_cfg["decimals"])
    amount_usd = amount_token * usd_price
    block_num = int(log_entry["blockNumber"], 16)

    user = db_session.exec(select(User).where(User.address == from_addr)).first()
    if not user:
        log.info(f"deposit from unknown {from_addr}: {amount_token} {token_cfg['symbol']}"
                 f" (${amount_usd:.2f}) ignored")
        return False

    tokens = int(amount_usd * 100)  # 1¢ = 1 token
    if tokens <= 0:
        return False

    note = f"{amount_token:.6f} {token_cfg['symbol']} @ ${usd_price:.4f} = ${amount_usd:.4f}"
    credit(db_session, user, tokens, "topup", ref_id=tx_hash, note=note)
    db_session.add(Deposit(
        user_id=user.id, tx_hash=tx_hash, block_number=block_num,
        from_address=from_addr, amount_usdc=amount_usd,  # USD 가치로 저장 (필드명은 호환성)
        tokens_credited=tokens,
    ))
    db_session.commit()
    log.info(f"[DEPOSIT] {from_addr} → +{tokens} tokens ({note}) tx={tx_hash}")
    return True


# ─── Main poller loop ─────────────────────────────────────────────

async def deposit_poller():
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    log.info(f"watching {len(config.ACCEPTED_TOKENS)} tokens for deposits to {config.DEPOSIT_ADDRESS}")

    while True:
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=ssl_ctx)
            ) as http:
                latest = await _get_latest_block(http)

                with Session(engine) as db:
                    for token_cfg in config.ACCEPTED_TOKENS:
                        cursor_name = f"deposit_{token_cfg['symbol']}"
                        cursor = db.exec(
                            select(IndexerCursor).where(IndexerCursor.name == cursor_name)
                        ).first()
                        if not cursor:
                            cursor = IndexerCursor(name=cursor_name, last_block=latest - 100)
                            db.add(cursor); db.commit(); db.refresh(cursor)

                        from_block = cursor.last_block + 1
                        to_block = min(latest, from_block + 1000)
                        if from_block > to_block:
                            continue

                        try:
                            logs = await _get_logs(http, token_cfg["contract"],
                                                   from_block, to_block)
                        except Exception as e:
                            log.warning(f"{token_cfg['symbol']} get_logs error: {e}")
                            continue

                        if logs:
                            # 가격 1번만 조회 (배치 처리)
                            if token_cfg.get("stable"):
                                price_usd = 1.0
                            else:
                                price_usd = await _get_chainlink_price(
                                    http, token_cfg["chainlink_aggregator"],
                                    token_cfg.get("aggregator_decimals", 8))
                                if price_usd <= 0:
                                    log.warning(f"{token_cfg['symbol']} price feed failed, skip")
                                    continue
                            log.info(f"{token_cfg['symbol']} @ ${price_usd:.4f}, "
                                     f"{len(logs)} log(s) blocks {from_block}-{to_block}")

                            for entry in logs:
                                try:
                                    _process_log(db, token_cfg, entry, price_usd)
                                except Exception as e:
                                    log.exception(f"process_log {token_cfg['symbol']}: {e}")

                        cursor.last_block = to_block
                        db.add(cursor); db.commit()

        except Exception as e:
            log.exception(f"poller error: {e}")

        await asyncio.sleep(15)
