# STREAK

Polymarket BTC 5-min auto-trading SaaS.

## Architecture

```
┌─ Main Site (this repo /backend + /frontend) ──────────────┐
│  - MetaMask (SIWE) login                                   │
│  - Token system (1 cycle = 1 token, 1¢ = 1 token)          │
│  - USDC auto-deposit detection (Polygon, polling worker)   │
│  - Multi-level referrals (L1=10, L2=5)                     │
│  - i18n: en / ko / zh                                      │
│  - Toss/Apple-style minimal UI                             │
└────────────────────────────────────────────────────────────┘
                            ⇅ HTTPS
┌─ Bot (/bot, runs on user's own VPS) ──────────────────────┐
│  - Private key NEVER leaves user's server                 │
│  - Calls main /api/tokens/consume per cycle               │
│  - Auto-stops on token depletion                          │
└────────────────────────────────────────────────────────────┘
```

This split keeps user funds **safe** (PK only on user's own VPS) while enabling
multi-tenant token economy on the main site.

## Repo layout

```
poly/
├── backend/                # Main site backend (FastAPI)
│   ├── main.py             # entrypoint
│   ├── auth.py             # SIWE + JWT
│   ├── tokens.py           # balance + cycle consume
│   ├── referrals.py        # multi-level referral
│   ├── deposits_worker.py  # USDC deposit polling
│   ├── models.py           # SQLModel tables
│   ├── db.py               # DB engine
│   ├── config.py           # env config
│   └── requirements.txt
├── frontend/               # SPA (vanilla JS, mobile-first)
│   ├── index.html
│   ├── app.js
│   ├── styles.css          # Toss/Apple style
│   └── i18n/{en,ko,zh}.json
├── bot/                    # Trading bot (per-user)
│   ├── streak.py           # core bot logic
│   ├── dashboard.html      # local control panel
│   ├── deploy.sh           # bot deployer (Vultr-ready)
│   ├── requirements.txt
│   └── ...
├── deploy_main.sh          # main site deployer (Vultr-ready)
└── README.md
```

## Deploy main site (Vultr)

```bash
ssh root@YOUR.SERVER.IP
curl -sSL https://raw.githubusercontent.com/yujoohwan6342-stack/POLY/main/deploy_main.sh | bash -s yourdomain.com
```

The script installs Python, FastAPI, nginx, certbot (SSL), creates systemd
service, and starts a USDC deposit poller in the background.

Configure in `/etc/streak/main.env`:

| var | example |
|---|---|
| `JWT_SECRET` | auto-generated |
| `DEPOSIT_ADDRESS` | `0xCf99…` (where users send USDC) |
| `SIWE_DOMAIN` | `streak.yourname.com` |
| `DATABASE_URL` | `sqlite:///…` (or postgres) |

## Deploy user bot (Vultr)

Each user runs their own bot instance (privacy + key safety):

```bash
ssh root@USER.SERVER.IP
curl -sSL https://raw.githubusercontent.com/yujoohwan6342-stack/POLY/main/bot/deploy.sh | bash -s userdomain.com
```

Then on the bot dashboard, paste the JWT from the main site to enable token-gated cycles.

## Local development

Backend:
```bash
cd backend
pip install -r requirements.txt
JWT_SECRET=dev SIWE_DOMAIN=localhost:8000 SIWE_URI=http://localhost:8000 \
    python -m uvicorn backend.main:app --reload --port 8000
```

Frontend served by backend at `/`. Open http://localhost:8000.

Bot (separate terminal):
```bash
cd bot
pip install -r requirements.txt
python streak.py
```

## Token economy

| event | tokens |
|---|---|
| signup | +10 |
| invite L1 (your direct invite signs up) | +10 |
| invite L2 (your invite invites someone) | +5 |
| 1 trading cycle | -1 |
| top up $1 USDC (Polygon) | +100 |

Withdrawal of unspent tokens: not supported (tokens are service credit).

## Security highlights

- Private keys **never** stored or sent to main site
- SIWE signatures with one-time nonces (replay-proof)
- JWT with 7-day expiry; HTTPS only in production
- Idempotent token consume (cycle_id de-dupe)
- USDC deposit handler is idempotent (tx_hash unique)
- SQL injection-safe (SQLModel ORM)
- CORS strict (env-controlled)
- Rate limit per IP (slowapi)

## Disclaimer

⚠ Trading involves risk. Polymarket is not available in all jurisdictions (e.g., US).
Strategies have backtested results but no guarantee of future profit.
Author not liable for losses. MIT License.
