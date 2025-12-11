# 🐱 PolyNeko v2

Automated trading bot for Polymarket 15-minute crypto prediction markets.

## Strategy

1. **Momentum-based entry** - Uses Binance price momentum to predict direction
2. **Trailing hedge** - If position moves against us, hedge with opposite side
3. **Auto-settlement** - Calculates P&L when slot ends

## Quick Start

```bash
# Install dependencies
pip install py-clob-client python-dotenv aiohttp

# Configure
nano .env  # Fill in your keys

# Run
python3 polyneko.py
```

## Configuration

| Variable | Description |
|----------|-------------|
| `PRIVATE_KEY` | Wallet private key |
| `POLYMARKET_FUNDER_ADDRESS` | Proxy wallet address from Polymarket |
| `SIGNATURE_TYPE` | 0=MetaMask, 1=Magic/Email, 2=Proxy |
| `SYMBOLS` | Coins to trade (BTC,ETH,SOL,XRP) |
| `BET_SIZE` | Base bet size in USD |
| `MAX_POSITION` | Max position per market |
| `SIMULATION_MODE` | true/false |

## Files

| File | Purpose |
|------|---------|
| `polyneko.py` | Main bot |
| `polyneko_dashboard.py` | Web dashboard (port 8080) |
| `polyneko.db` | Trade history (SQLite) |
| `polyneko.log` | Debug logs |

## Dashboard

```bash
# Standalone
python3 polyneko_dashboard.py

# Or enable in .env
DASHBOARD_ENABLED=true
DASHBOARD_PORT=8080
```

## Discord Notifications

Add webhook URL to `.env`:
```bash
DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
```

## ⚠️ Disclaimer

This bot trades real money. Use at your own risk. Start with `SIMULATION_MODE=true`.
