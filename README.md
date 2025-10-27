# Solana Pump.fun Momentum Alert Bot

This is a high-performance Python bot designed to monitor the [pump.fun](https://pump.fun) platform in real-time. It connects directly to the PumpPortal WebSocket feed to detect new token mints, monitors their bonding curve on-chain via RPC, and sends immediate Telegram alerts when a token meets specific momentum criteria (e.g., 120% gain in 15 minutes).

This bot **does not auto-buy**. It is an advanced monitoring and alerting tool built with `asyncio`, `websockets`, `python-telegram-bot`, `asyncpg`, and `FastAPI`.

## Core Features

- **Real-Time Mint Detection:** Connects to the PumpPortal WebSocket (`wss://pumpportal.fun/api/data`) to capture new tokens the instant they are created.
- **On-Chain Price Monitoring:** Directly monitors the token's bonding curve account using Solana RPC (`getAccountInfo`) and decodes the data locally to track price changes.
- **Configurable Momentum Alerts:** Uses a flexible alert engine to trigger notifications based on custom rules (default: 120% gain within 15 minutes).
- **Telegram Bot Interface:** Full control via a Telegram bot with commands:
    - `/start`: Shows the main menu.
    - `/iniciar`: Starts the monitoring service.
    - `/detener`: Stops all monitoring tasks.
    - `/status`: Shows the current status, including connected clients and active monitors.
- **Database Persistence (Optional):** Uses `asyncpg` to log all monitored tokens and triggered alerts to a PostgreSQL database for later analysis.
- **Health Check Endpoint:** Includes a `FastAPI` server with a `/health` endpoint for easy monitoring and deployment checks.
- [cite_start]**Deployment Ready:** Comes with a `Procfile` [cite: 1] [cite_start]and `requirements.txt`[cite: 2], making it easy to deploy on platforms like Railway or Heroku.

## Tech Stack

- **Python 3.10+** (utilizing `asyncio`)
- **`websockets`:** For the real-time PumpPortal feed.
- **`python-telegram-bot` (v22+):** For the interactive Telegram bot interface.
- **`aiohttp` / `httpx`:** Used in the custom RPC client for on-chain data and DexScreener fallbacks.
- **`asyncpg`:** High-performance asyncio client for PostgreSQL.
- **`FastAPI` / `uvicorn`:** For the API/health check endpoint.
- **`threading`:** Used to run the Telegram polling in a separate thread to avoid conflicting with the main `asyncio` event loop.

## How It Works

1.  **WSS Client (`PumpPortalClient`):** Connects to the PumpPortal WebSocket. When a new token message is received, it passes the data to the `TokenManager`.
2.  **Token Manager (`TokenManager`):** Receives the new token (mint, symbol, name) and spawns a new `asyncio` task to monitor it, using a semaphore to limit concurrent tasks.
3.  **Monitor Task:** This task runs in a loop for a set duration (e.g., 30 mins):
    - It calls the `RPCClient` to fetch the token's bonding curve account info.
    - It decodes the account's data (virtual/real reserves, supply) to calculate the *current* on-chain price.
    - It checks for dump conditions (e.g., -50% from max price).
4.  **Alert Engine (`AlertEngine`):** The current price and elapsed time are passed to the alert engine, which checks against all defined rules.
5.  **Notification (`Notification`):** If a rule is triggered (e.g., 120% in 15 min), a formatted alert message is sent to your Telegram chat via the bot.
6.  **Persistence (`Database`):** The bot logs the new token to the `token_monitoring` table and any alerts to the `token_alerts` table.

## Installation & Setup

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git](https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git)
    cd YOUR_REPOSITORY
    ```

2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
    [cite_start]*(Based on the provided `requirements (5).txt` file)* [cite: 2]

3.  **Configure Environment Variables:**
    The bot is configured via environment variables. You can create a `.env` file or set them directly in your deployment service.

    ```ini
    # --- Required ---
    TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
    TELEGRAM_CHAT_ID="-100123456789..." # Your channel/group ID

    # --- RPC (At least one is needed) ---
    # Get one from QuickNode or Helius
    QUICKNODE_RPC_URL="[https://your-quicknode-url.solana-mainnet.discover.quiknode.pro/](https://your-quicknode-url.solana-mainnet.discover.quiknode.pro/)..."
    HELIUS_RPC_URL="[https://mainnet.helius-rpc.com/?api-key=](https://mainnet.helius-rpc.com/?api-key=)..."

    # --- Optional (But Recommended) ---
    ENABLE_DB="true"
    DATABASE_URL="postgresql://user:password@host:port/dbname"
    PUMPPORTAL_WSS="wss://pumpportal.fun/api/data" # Default
    HEALTH_PORT="8080" # For deployment health checks
    ```

## Running the Bot

### Locally

Simply run the main Python script:

```bash
python telegram_bot_final.py
```

### Deployment (Railway / Heroku)

This project is ready to be deployed. [cite_start]The `Procfile` [cite: 1] included will automatically run the bot using the `web` dyno.

```procfile
web: python telegram_bot_final.py
```

Just connect your GitHub repository to Railway or Heroku, and it will build and deploy automatically. Remember to set the environment variables in your deployment service's settings panel.

## Disclaimer

This tool is for educational and monitoring purposes only. It does not provide financial advice and does not perform any automated trading. The cryptocurrency market is extremely volatile. Use this tool at your own risk.
