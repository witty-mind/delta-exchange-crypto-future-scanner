import re

with open('app.py', 'r') as f:
    code = f.read()

# 1. Add sqlite3 import
code = code.replace("import requests, datetime, time, threading, logging", "import requests, datetime, time, threading, logging, sqlite3")

# 2. Add init_db and prune_db at the top (after DataCache definition)
init_db_code = """
# Database initialization
def init_db():
    try:
        with sqlite3.connect("delta_history.db", timeout=10) as conn:
            conn.execute(\"\"\"
                CREATE TABLE IF NOT EXISTS candles_5m (
                    symbol TEXT,
                    time INTEGER,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    PRIMARY KEY (symbol, time)
                )
            \"\"\")
    except Exception as e:
        log.error("DB Init Error: %s", e)

def prune_db():
    try:
        with sqlite3.connect("delta_history.db", timeout=10) as conn:
            cutoff = int(time.time()) - (48 * 60 * 60)
            conn.execute("DELETE FROM candles_5m WHERE time < ?", (cutoff,))
            conn.commit()
    except Exception as e:
        log.error("DB Pruning error: %s", e)
"""
# Insert after DataCache class
code = code.replace("    def get_futures_table(self) -> list:", init_db_code + "\n    def get_futures_table(self) -> list:")

# 3. Rewrite fetch_5m_ohlcv_one
old_fetch = """def fetch_5m_ohlcv_one(symbol, start, end):
    q = urlencode({"resolution": "5m", "symbol": symbol, "start": str(int(start)), "end": str(int(end))})
    url = f"{DELTA_BASE}/history/candles?{q}"
    data = _get(url)
    if not data or not data.get("result"):
        return symbol, []
    rows = []
    for c in data["result"]:
        try:
            rows.append({
                "time": int(c["time"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c.get("volume") or 0),
            })
        except (TypeError, ValueError, KeyError):
            continue
    rows.sort(key=lambda x: x["time"])
    return symbol, rows"""

new_fetch = """def fetch_5m_ohlcv_one(symbol, start, end):
    try:
        with sqlite3.connect("delta_history.db", timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM candles_5m WHERE symbol = ? AND time >= ? AND time <= ? ORDER BY time ASC", (symbol, int(start), int(end)))
            db_rows = cursor.fetchall()
            
            local_candles = []
            for r in db_rows:
                local_candles.append({
                    "time": r["time"],
                    "open": r["open"],
                    "high": r["high"],
                    "low": r["low"],
                    "close": r["close"],
                    "volume": r["volume"],
                })
                
            fetch_start = int(start)
            if local_candles:
                fetch_start = local_candles[-1]["time"] + (5 * 60)
                
            if fetch_start <= int(end):
                q = urlencode({"resolution": "5m", "symbol": symbol, "start": str(fetch_start), "end": str(int(end))})
                url = f"{DELTA_BASE}/history/candles?{q}"
                data = _get(url)
                
                new_candles = []
                if data and data.get("result"):
                    for c in data["result"]:
                        try:
                            new_candles.append({
                                "time": int(c["time"]),
                                "open": float(c["open"]),
                                "high": float(c["high"]),
                                "low": float(c["low"]),
                                "close": float(c["close"]),
                                "volume": float(c.get("volume") or 0),
                            })
                        except (TypeError, ValueError, KeyError):
                            continue
                            
                    if new_candles:
                        new_candles.sort(key=lambda x: x["time"])
                        cursor.executemany(\"\"\"
                            INSERT OR IGNORE INTO candles_5m (symbol, time, open, high, low, close, volume)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        \"\"\", [(symbol, c["time"], c["open"], c["high"], c["low"], c["close"], c["volume"]) for c in new_candles])
                        conn.commit()
                        local_candles.extend(new_candles)
                        
            return symbol, local_candles
    except Exception as e:
        log.error("DB Fetch error for %s: %s", symbol, e)
        return symbol, []"""

code = code.replace(old_fetch, new_fetch)

# 4. Call prune_db() at end of ema_refresh_loop
code = code.replace("cache.set_ema_pullback_sell(sorted(ema_pb_sell_list, key=lambda x: x[\"pct_below_100\"], reverse=True))", "cache.set_ema_pullback_sell(sorted(ema_pb_sell_list, key=lambda x: x[\"pct_below_100\"], reverse=True))\n                prune_db()")

# 5. Call init_db() in ensure_background_workers
code = code.replace("_bootstrap_tickers_once()", "init_db()\n    _bootstrap_tickers_once()")

with open('app.py', 'w') as f:
    f.write(code)
print("Done updating app.py for SQLite")
