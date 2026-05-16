# Delta Exchange Crypto Futures Scanner

A real-time **perpetual futures screener** for [Delta Exchange India](https://www.delta.exchange/), built with Python/Flask and WebSockets. It scans all live perpetual contracts every 5 minutes using technical indicators and surfaces momentum breakout setups directly in the browser — no login or API key required.

---

## ✨ Features

### 📊 Market Overview
- **Market Breadth Bar** — real-time ratio of gainers vs losers across all live perpetual contracts
- **All Perpetual Futures Table** — live mark price, 24h change, 24h high/low for every tradable contract, sortable gainers/losers first

### 🔍 Scanner Panels (auto-refresh every 10s)
Each scanner panel shows the **top 20 results** with a live count badge and last-updated timestamp.

| Panel | What it finds |
|---|---|
| **Above Prior Day High** | Contracts whose current mark price has broken above yesterday's session high |
| **Below Prior Day Low** | Contracts trading below yesterday's session low |
| **Above 10 / 20 / 100 EMA** | Contracts where mark price > all three 5m EMAs simultaneously |
| **PDH + EMA Stack** | Intersection: above prior day high AND above all three 5m EMAs |
| **Ichimoku Stack (Bullish)** | Full bullish Ichimoku confluence (see below) |
| **Ichimoku Bear (Bearish)** | Full bearish Ichimoku confluence (mirror of bullish) |

### ⚡ Ichimoku Stack Scan — Conditions

**Bullish** — all must be true on the last closed 5m bar:
1. 🟡 **Above cloud** — close > Senkou A and Senkou B (9 / 26 / 52 Donchian, displaced +26)
2. 🔵 **TK Cross up** — Tenkan-sen (9) > Kijun-sen (26)
3. 🟢 **Chikou confirmation** — current close > close 26 bars ago
4. 📊 **RVOL surge** — current bar volume > 1.5× average of prior 50 bars
5. 📈 **OI expansion** — open interest at current bar > previous bar

**Bearish** — exact mirror:
1. 🔴 **Below cloud** — close < Senkou A and Senkou B
2. ⬇️ **TK Cross down** — Tenkan < Kijun
3. ⬇️ **Chikou below** — current close < close 26 bars ago
4. 📊 **RVOL surge** — same threshold (1.5×)
5. 📈 **OI expansion** — new positions being opened into the move

> **Live re-filter:** The Ichimoku endpoints re-apply the cloud check against the latest live mark price on every request, so stale results that have drifted back into the cloud are automatically removed.

### 🔔 Live Alert Feed
- Real-time WebSocket alerts for every new signal: **PDH BREAKOUT**, **PDL BREAKDOWN**, **SUPER MOMENTUM**, **CONFLUENCE**, **ICHIMOKU STACK**, **ICHIMOKU BEAR**
- Browser toast notifications + audio alert for new (non-historical) signals
- Feed capped at 20 rows; older entries are trimmed automatically

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────┐
│                  Browser (index.html)            │
│  REST polling every 10s + WebSocket (Socket.IO)  │
└─────────────────┬───────────────────────────────┘
                  │ HTTP / WS
┌─────────────────▼───────────────────────────────┐
│          Flask + Flask-SocketIO (app.py)          │
│                                                   │
│  Background threads (daemon):                     │
│  ├── ohlc_refresh_loop      — every 5 min        │
│  │    └── fetches prior-day H/L for all perps    │
│  ├── ema_refresh_loop       — every 5 min        │
│  │    └── fetches 200×5m candles + OI per symbol │
│  │    └── computes EMA 10/20/100 + Ichimoku scans│
│  ├── ticker_refresh_loop    — every 10 s         │
│  │    └── fetches live mark prices               │
│  │    └── computes PDH/PDL, EMA screener         │
│  │    └── emits WebSocket alerts for new signals │
│  └── breakout_monitor_loop  — every 10 s         │
│       └── emits PDH/PDL breakout alerts          │
└─────────────────┬───────────────────────────────┘
                  │ HTTPS REST
┌─────────────────▼───────────────────────────────┐
│       Delta Exchange India Public API v2          │
│  api.india.delta.exchange/v2                      │
│  ├── /products  (perpetual listing)               │
│  ├── /tickers   (live mark prices)                │
│  └── /history/candles (OHLCV + OI candles)       │
└─────────────────────────────────────────────────┘
```

---

## 🚀 Installation

### Prerequisites
- Python **3.10+** (3.11 or 3.12 recommended)
- `pip` package manager
- Internet access to reach `api.india.delta.exchange`

### 1. Clone the repository

```bash
git clone https://github.com/witty-mind/delta-exchange-crypto-future-scanner.git
cd delta-exchange-crypto-future-scanner
```

### 2. Create a virtual environment (recommended)

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS / Linux
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

`requirements.txt` installs:
| Package | Purpose |
|---|---|
| `flask` | Web framework |
| `flask-socketio` | WebSocket support |
| `requests` | HTTP client for Delta API |
| `gunicorn` | Production WSGI server (for deployment) |

### 4. Run the development server

```bash
python app.py
```

The server starts on **http://127.0.0.1:5000** and will also be accessible on your local network at `http://<your-ip>:5000`.

**First-run behaviour:**
- Perpetual futures list and live tickers load within ~5 seconds
- EMA and Ichimoku data populate after the first full 5-minute scan cycle
- Prior-day OHLC data loads within the first minute

---

## 📁 Project Structure

```
delta-exchange-crypto-future-scanner/
├── app.py                  # Main Flask application + all backend logic
├── requirements.txt        # Python dependencies
├── Procfile                # Heroku / Railway deployment config
├── runtime.txt             # Python version pin for deployment
├── templates/
│   └── index.html          # Single-page frontend (HTML + CSS + JS)
└── static/
    ├── css/
    │   └── style.css
    ├── js/
    │   └── main.js
    └── sounds/
        └── README.txt
```

---

## 🌐 API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard UI |
| `GET /market-breadth` | Gainers/losers ratio |
| `GET /futures` | All live perpetual contracts with mark prices |
| `GET /above-pdh` | Symbols above prior day high |
| `GET /below-pdl` | Symbols below prior day low |
| `GET /above-ema` | Symbols above 5m EMA 10/20/100 |
| `GET /pdh-ema-confluence` | PDH + EMA stack intersection |
| `GET /ichimoku-stack` | Bullish Ichimoku stack matches (live-filtered) |
| `GET /ichimoku-bear` | Bearish Ichimoku stack matches (live-filtered) |

---

## ☁️ Deployment (Free Tier)

### Render.com
1. Push to GitHub
2. Create a new **Web Service** on [render.com](https://render.com)
3. Set **Build Command:** `pip install -r requirements.txt`
4. Set **Start Command:** `gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 app:app`
5. Deploy — free tier spins down after inactivity; use UptimeRobot to keep it alive

### Railway.app
1. Connect your GitHub repo
2. Railway auto-detects the `Procfile` and deploys with one click

### Heroku
```bash
heroku create your-app-name
git push heroku master
```

---

## ⚠️ Notes & Limitations

- **No API key required** — uses Delta Exchange public endpoints only
- **Rate limiting:** the app uses a connection pool of 10 and fetches in parallel batches of 20. Avoid running multiple instances against the same IP.
- **OI data availability:** the Ichimoku scan requires `OI:<symbol>` candle history from Delta. If OI data is unavailable for a symbol, it is excluded from the Ichimoku lists.
- **5m scan latency:** the full Ichimoku scan takes ~1–3 minutes to complete depending on the number of active contracts (~150–200 as of 2026).
- **Timeframe:** all scans are based on **5-minute candles**. The prior-day levels use IST (UTC+5:30) session boundaries (05:30–05:29 next day).

---

## 📄 License

MIT License — free to use, modify, and distribute.

---

## 🤝 Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.