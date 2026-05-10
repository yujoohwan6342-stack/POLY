# STREAK

Automated trading bot for **Polymarket BTC 5-min markets** with a web dashboard.

- 🐍 Single-file Python backend (no DB, no framework)
- 🌐 Browser dashboard (vanilla JS + Chart.js)
- 💾 All trade history in **browser localStorage** (server stays stateless)
- 🔑 Auto-detects EOA / POLY_PROXY / Gnosis Safe wallets
- 📈 Multiple price sources: Chainlink (settlement) + Binance + Bybit
- 🎮 Simulation & 💵 Live mode (real Polymarket orders via py-clob-client-v2)

## Quick start (local)

```bash
git clone https://github.com/yujoohwan6342-stack/POLY.git
cd POLY
pip install -r requirements.txt
python streak.py
```

→ browser opens automatically at http://localhost:8765

## Production deploy on Vultr

1. Create a Vultr Cloud Compute (Ubuntu 22.04, $5–$10/mo plan is enough)
2. SSH into the server: `ssh root@YOUR.SERVER.IP`
3. Run the one-shot deploy script:

```bash
curl -sSL https://raw.githubusercontent.com/yujoohwan6342-stack/POLY/main/deploy.sh | bash
# Or with custom domain + auto SSL:
curl -sSL https://raw.githubusercontent.com/yujoohwan6342-stack/POLY/main/deploy.sh | bash -s yourdomain.com
```

The script:
- Installs Python, nginx, certbot
- Clones this repo to `/opt/streak`
- Sets up systemd service (`streak.service`)
- Configures nginx reverse proxy
- (Optional) Issues Let's Encrypt SSL if domain provided

## Configuration

Settings are managed via the web UI and persisted to `strategy_config.json` on the server.

| Setting | Default | Description |
|---|---|---|
| `bet_size_usd` | $5 | Per-cycle bet size |
| `entry_mode` | `low_target` | `low_target` (10c±tol) or `high_lead` (≥entry) |
| `entry_price` | 0.10 | Min/target entry price |
| `max_entry_price` | 0.99 | Max entry price (high_lead only) |
| `tp_price` | 0.15 | Take profit price (≥1.0 = hold to expiry) |
| `sl_price` | 0.05 | Stop loss price |
| `tradeable_pct` | 0.6 | Buy only if progress < this |
| `buy_when_remaining_below_pct` | 1.0 | Buy only when remaining ≤ this |
| `buy_order_type` | `limit` | `limit` or `market` |
| `sell_order_type` | `limit` | `limit` or `market` |
| `max_cycles_per_session` | 0 | Auto-stop after N cycles (0 = unlimited) |

## Wallet support

- **MetaMask / EOA**: enter private key only
- **Polymarket Magic.link (Google/email login)**: enter private key + Polymarket Deposit Address
- Bot auto-detects POLY_PROXY (sig_type=1) and Gnosis Safe (sig_type=2) and uses whichever has USDC

## Disclaimer

⚠ This is experimental software. Trading carries financial risk. Polymarket is **blocked in the US**. Strategies shown have backtested results but **no guarantee of future profit**. Use small bet sizes for testing. The author is not liable for any losses.

## License

MIT
