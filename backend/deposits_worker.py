"""USDC 입금 폴링 워커.

DEPOSIT_ADDRESS 로 들어오는 모든 USDC 트랜잭션 감지 → from_address가 등록된
사용자라면 토큰 부여 (1 cent = 1 token).

블록체인 RPC 폴링 (eth_getLogs) 으로 동작.
백그라운드 task로 실행 (FastAPI startup hook).
"""
import asyncio
import json
import logging
import ssl
import certifi
from typing import Optional

import aiohttp
from sqlmodel import Session, select

from . import config
from .db import engine
from .models import User, Deposit, IndexerCursor
from .tokens import credit


log = logging.getLogger("deposits")

# ERC20 Transfer event signature
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _addr_to_topic(addr: str) -> str:
    """0x... → 0x000...0addr (32-byte hex topic)."""
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def _topic_to_addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


async def _rpc_call(session: aiohttp.ClientSession, method: str, params: list) -> dict:
    """여러 RPC fallback 시도."""
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    last_err = None
    for rpc in config.POLYGON_RPCS:
        try:
            async with session.post(rpc, json=payload, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json()
            if "error" in d:
                last_err = d["error"]
                continue
            return d
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"all RPCs failed: {last_err}")


async def _get_latest_block(session: aiohttp.ClientSession) -> int:
    d = await _rpc_call(session, "eth_blockNumber", [])
    return int(d["result"], 16)


async def _get_logs(session: aiohttp.ClientSession,
                    from_block: int, to_block: int) -> list:
    params = [{
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
        "address": config.USDC_CONTRACT,
        "topics": [TRANSFER_TOPIC, None, _addr_to_topic(config.DEPOSIT_ADDRESS)],
    }]
    d = await _rpc_call(session, "eth_getLogs", params)
    return d.get("result", [])


def _process_log(db_session: Session, log_entry: dict) -> bool:
    """입금 로그 1건 처리. True = 처리됨, False = 스킵."""
    tx_hash = log_entry["transactionHash"]
    if db_session.exec(select(Deposit).where(Deposit.tx_hash == tx_hash)).first():
        return False  # 이미 처리됨

    from_addr = _topic_to_addr(log_entry["topics"][1])
    amount_raw = int(log_entry["data"], 16)
    amount_usdc = amount_raw / 1e6  # USDC has 6 decimals
    block_num = int(log_entry["blockNumber"], 16)

    # from_addr가 등록된 사용자인지 확인
    user = db_session.exec(select(User).where(User.address == from_addr)).first()
    if not user:
        log.info(f"deposit from unknown address {from_addr} ${amount_usdc:.2f} ignored")
        return False

    # 1 cent = 1 token. $1 = 100 tokens.
    tokens = int(amount_usdc * 100)
    if tokens <= 0:
        return False

    credit(db_session, user, tokens, "topup", ref_id=tx_hash,
           note=f"${amount_usdc:.4f} USDC")
    db_session.add(Deposit(
        user_id=user.id, tx_hash=tx_hash, block_number=block_num,
        from_address=from_addr, amount_usdc=amount_usdc,
        tokens_credited=tokens,
    ))
    db_session.commit()
    log.info(f"[DEPOSIT] {from_addr} → +{tokens} tokens (${amount_usdc:.2f}) tx={tx_hash}")
    return True


async def deposit_poller():
    """주기적으로 USDC Transfer event 폴링."""
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    while True:
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=ssl_ctx)
            ) as http:
                latest = await _get_latest_block(http)

                with Session(engine) as db:
                    cursor = db.exec(
                        select(IndexerCursor).where(IndexerCursor.name == "usdc_deposit")
                    ).first()
                    if not cursor:
                        cursor = IndexerCursor(name="usdc_deposit", last_block=latest - 100)
                        db.add(cursor)
                        db.commit()
                        db.refresh(cursor)

                    from_block = cursor.last_block + 1
                    to_block = min(latest, from_block + 1000)  # 최대 1000블록씩
                    if from_block > to_block:
                        await asyncio.sleep(15)
                        continue

                    logs = await _get_logs(http, from_block, to_block)
                    if logs:
                        log.info(f"got {len(logs)} USDC deposit log(s) blocks {from_block}-{to_block}")
                    for entry in logs:
                        try:
                            _process_log(db, entry)
                        except Exception as e:
                            log.exception(f"process log error: {e}")

                    cursor.last_block = to_block
                    db.add(cursor)
                    db.commit()

        except Exception as e:
            log.exception(f"poller error: {e}")

        await asyncio.sleep(15)  # 15초마다 폴링
