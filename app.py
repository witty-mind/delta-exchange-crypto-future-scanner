from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO
import os
import requests, datetime, time, threading, logging
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# ---------------------------------------------------------------------------
# Shared data cache
# ---------------------------------------------------------------------------

class DataCache:
    def __init__(self):
        self._lock = threading.RLock()
        self.futures: list = []          
        self.tickers: dict = {}          
        self.ohlc: dict = {}             
        self.emas: dict = {}             
        self.above_pdh: list = []        
        self.below_pdl: list = []        
        self.above_ema_5m: list = []     
        self.pdh_ema_confluence: list = []
        self.ichimoku_stack: list = []
        self.ichimoku_bear: list = []
        self.futures_ready = False       
        self.ohlc_ready = False
        self.ema_ready = False
        self.last_ema_symbols = set()
        self.last_confluence_symbols = set()
        self.last_ichimoku_symbols = set()
        self.last_ichimoku_bear_symbols = set()

    def set_futures(self, data: list):
        with self._lock: self.futures = data

    def set_tickers(self, data: dict):
        with self._lock: self.tickers = data

    def set_ohlc(self, data: dict):
        with self._lock: 
            self.ohlc = data
            self.ohlc_ready = True

    def set_emas(self, data: dict):
        with self._lock:
            self.emas = data
            self.ema_ready = True

    def set_above_pdh(self, data: list):
        with self._lock: self.above_pdh = data

    def set_below_pdl(self, data: list):
        with self._lock: self.below_pdl = data

    def set_above_ema(self, data: list):
        with self._lock: self.above_ema_5m = data
        
    def set_pdh_ema_confluence(self, data: list):
        with self._lock: self.pdh_ema_confluence = data

    def set_ichimoku_stack(self, data: list):
        with self._lock: self.ichimoku_stack = data

    def set_ichimoku_bear(self, data: list):
        with self._lock: self.ichimoku_bear = data

    def get_ichimoku_bear(self) -> list:
        with self._lock: return list(self.ichimoku_bear)

    def get_futures_table(self) -> list:
        with self._lock:
            full, pending = [], []
            tickers_loaded = bool(self.tickers)
            for f in self.futures:
                sym = f.get("symbol")
                if not sym:
                    continue
                t = self.tickers.get(sym)
                if t:
                    full.append({
                        "symbol": sym,
                        "mark_price": float(t.get("mark_price", 0) or 0),
                        "change_24h": _ticker_change_24h(t),
                        "high_24h": float(t.get("high", 0)),
                        "low_24h": float(t.get("low", 0)),
                    })
                else:
                    pending.append({
                        "symbol": sym,
                        "mark_price": None,
                        "change_24h": None,
                        "high_24h": None,
                        "low_24h": None,
                        "pending": True,
                    })
            full.sort(key=lambda x: x["change_24h"], reverse=True)
            pending.sort(key=lambda x: x["symbol"])
            return full + pending
            
    def get_market_breadth(self) -> dict:
        with self._lock:
            if not self.tickers or not self.futures:
                return {"gainers_pct": 0, "losers_pct": 0, "total": 0}
            perp_syms = {f.get("symbol") for f in self.futures if f.get("symbol")}
            tickers = {s: self.tickers[s] for s in perp_syms if s in self.tickers}
            if not tickers:
                return {"gainers_pct": 0, "losers_pct": 0, "total": 0}
            total = len(tickers)
            gainers = sum(1 for t in tickers.values() if _ticker_change_24h(t) > 0)
            losers = sum(1 for t in tickers.values() if _ticker_change_24h(t) < 0)
            return {
                "gainers_pct": round((gainers / total) * 100, 1) if total > 0 else 0,
                "losers_pct": round((losers / total) * 100, 1) if total > 0 else 0,
                "total": total,
            }

    def get_above_pdh(self) -> list:
        with self._lock: return list(self.above_pdh)

    def get_below_pdl(self) -> list:
        with self._lock: return list(self.below_pdl)

    def get_above_ema(self) -> list:
        with self._lock: return list(self.above_ema_5m)
        
    def get_pdh_ema_confluence(self) -> list:
        with self._lock: return list(self.pdh_ema_confluence)

    def get_ichimoku_stack(self) -> list:
        with self._lock: return list(self.ichimoku_stack)

    def snapshot_for_calculations(self):
        with self._lock:
            return dict(self.tickers), dict(self.ohlc), dict(self.emas)

    def has_perpetual_products(self) -> bool:
        with self._lock:
            return bool(self.futures)

cache = DataCache()
SESSION = requests.Session()
DELTA_BASE = "https://api.india.delta.exchange/v2"

# ---------------------------------------------------------------------------
# API & Calculation Helpers
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 10) -> dict | None:
    try:
        r = SESSION.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("GET %s failed: %s", url, e)
        return None

def get_ist_timestamps():
    ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    now = datetime.datetime.now(ist)
    today_ist = now.date()
    yesterday_ist = today_ist - datetime.timedelta(days=1)
    start_dt = datetime.datetime(yesterday_ist.year, yesterday_ist.month, yesterday_ist.day, 5, 30, tzinfo=ist)
    end_dt = datetime.datetime(today_ist.year, today_ist.month, today_ist.day, 5, 29, tzinfo=ist)
    return int(start_dt.timestamp()), int(end_dt.timestamp())

def fetch_perpetual_futures():
    """Only live perpetual listings. Without `states=live`, /products includes expired/settled
    definitions; intersecting those symbols with /tickers inflated counts (~900+) vs tradable perps."""
    all_rows = []
    after = None
    while True:
        params = [
            ("contract_types", "perpetual_futures"),
            ("states", "live"),
            ("page_size", "200"),
        ]
        if after:
            params.append(("after", after))
        data = _get(f"{DELTA_BASE}/products?{urlencode(params)}")
        if not data:
            break
        batch = data.get("result") or []
        all_rows.extend(batch)
        meta = data.get("meta") or {}
        after = meta.get("after")
        if not after or not batch:
            break
    return all_rows

def _ticker_change_24h(t: dict) -> float:
    """Delta REST may expose mark or LTP 24h change under different keys."""
    for key in ("mark_change_24h", "m24hc", "ltp_change_24h"):
        v = t.get(key)
        if v is not None and v != "":
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def fetch_all_tickers():
    data = _get(f"{DELTA_BASE}/tickers")
    return {t["symbol"]: t for t in data.get("result", [])} if data else {}

def fetch_ohlc_one(symbol, start, end):
    url = f"{DELTA_BASE}/history/candles?resolution=1d&symbol={symbol}&start={start}&end={end}"
    data = _get(url)
    if data and data.get("result"):
        c = data["result"][0]
        return symbol, {"high": float(c["high"]), "low": float(c["low"])}
    return symbol, None

def fetch_5m_ohlcv_one(symbol, start, end):
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
    return symbol, rows


def fetch_5m_candles_one(symbol, start, end):
    sym, rows = fetch_5m_ohlcv_one(symbol, start, end)
    return sym, [r["close"] for r in rows]


CANDLE_5M_SEC = 5 * 60


def _last_closed_bar_index(bars: list, now_ts: int | None = None) -> int | None:
    if not bars:
        return None
    now_ts = int(now_ts or time.time())
    for i in range(len(bars) - 1, -1, -1):
        t0 = bars[i]["time"]
        if t0 + CANDLE_5M_SEC <= now_ts:
            return i
    return None


def _donchian_mid(highs: list[float], lows: list[float], idx: int, length: int) -> float | None:
    start = idx - length + 1
    if start < 0 or idx >= len(highs):
        return None
    chunk_h = highs[start : idx + 1]
    chunk_l = lows[start : idx + 1]
    return (max(chunk_h) + min(chunk_l)) / 2.0



def _rvol_prev50(volumes: list[float], i: int) -> float | None:
    if i < 50:
        return None
    prev = volumes[i - 50 : i]
    avg = sum(prev) / 50.0
    if avg <= 0:
        return None
    cur = volumes[i]
    return cur / avg


ICHIMOKU_MIN_INDEX = 77


def try_ichimoku_stack_match(symbol: str, bars: list, start: int, end: int) -> dict | None:
    """Last fully closed 5m bar must pass all criteria (Ichimoku 9/26/52, RVOL, OI)."""
    if len(bars) < ICHIMOKU_MIN_INDEX + 1:
        return None
    i = _last_closed_bar_index(bars)
    if i is None or i < ICHIMOKU_MIN_INDEX:
        return None

    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    close_i = closes[i]

    j = i - 26
    ta_j = _donchian_mid(highs, lows, j, 9)
    kj_j = _donchian_mid(highs, lows, j, 26)
    if ta_j is None or kj_j is None:
        return None
    senkou_a = (ta_j + kj_j) / 2.0
    senkou_b = _donchian_mid(highs, lows, j, 52)
    if senkou_b is None:
        return None
    if not (close_i > senkou_a and close_i > senkou_b):
        return None

    ten_i = _donchian_mid(highs, lows, i, 9)
    kij_i = _donchian_mid(highs, lows, i, 26)
    if ten_i is None or kij_i is None or ten_i < kij_i:
        return None

    if close_i <= closes[i - 26]:
        return None


    rv = _rvol_prev50(vols, i)
    if rv is None or rv <= 1.5:
        return None

    _, oi_rows = fetch_5m_ohlcv_one(f"OI:{symbol}", start, end)
    oi_map = {r["time"]: r["close"] for r in oi_rows}
    ti = bars[i]["time"]
    ti1 = bars[i - 1]["time"]
    oi_now = oi_map.get(ti)
    oi_prev = oi_map.get(ti1)
    if oi_now is None or oi_prev is None:
        return None
    if oi_now <= oi_prev:
        return None

    return {
        "symbol": symbol,
        "close": close_i,
        "mark_price": close_i,
        "senkou_a": senkou_a,
        "senkou_b": senkou_b,
        "tenkan": ten_i,
        "kijun": kij_i,
        "rvol": round(rv, 2),
        "oi": oi_now,
        "oi_prev": oi_prev,
    }


def try_ichimoku_bear_match(symbol: str, bars: list, start: int, end: int) -> dict | None:
    """Bearish mirror: last closed 5m bar below cloud, TK inverted, Chikou below, RVOL, OI expanding."""
    if len(bars) < ICHIMOKU_MIN_INDEX + 1:
        return None
    i = _last_closed_bar_index(bars)
    if i is None or i < ICHIMOKU_MIN_INDEX:
        return None

    highs = [b["high"] for b in bars]
    lows  = [b["low"]  for b in bars]
    closes = [b["close"] for b in bars]
    vols   = [b["volume"] for b in bars]
    close_i = closes[i]

    j = i - 26
    ta_j = _donchian_mid(highs, lows, j, 9)
    kj_j = _donchian_mid(highs, lows, j, 26)
    if ta_j is None or kj_j is None:
        return None
    senkou_a = (ta_j + kj_j) / 2.0
    senkou_b = _donchian_mid(highs, lows, j, 52)
    if senkou_b is None:
        return None
    # Price must be BELOW both cloud boundaries
    if not (close_i < senkou_a and close_i < senkou_b):
        return None

    ten_i = _donchian_mid(highs, lows, i, 9)
    kij_i = _donchian_mid(highs, lows, i, 26)
    # Bearish TK: Tenkan < Kijun
    if ten_i is None or kij_i is None or ten_i > kij_i:
        return None

    # Chikou below price 26 bars ago
    if close_i >= closes[i - 26]:
        return None

    rv = _rvol_prev50(vols, i)
    if rv is None or rv <= 1.5:
        return None

    _, oi_rows = fetch_5m_ohlcv_one(f"OI:{symbol}", start, end)
    oi_map = {r["time"]: r["close"] for r in oi_rows}
    ti  = bars[i]["time"]
    ti1 = bars[i - 1]["time"]
    oi_now  = oi_map.get(ti)
    oi_prev = oi_map.get(ti1)
    if oi_now is None or oi_prev is None:
        return None
    if oi_now <= oi_prev:
        return None

    return {
        "symbol": symbol,
        "close": close_i,
        "mark_price": close_i,
        "senkou_a": senkou_a,
        "senkou_b": senkou_b,
        "tenkan": ten_i,
        "kijun": kij_i,
        "rvol": round(rv, 2),
        "oi": oi_now,
        "oi_prev": oi_prev,
    }


def calc_emas(prices):
    if len(prices) < 100: return None
    def ema(p, n):
        m = 2 / (n + 1)
        val = sum(p[:n]) / n
        for x in p[n:]: val = (x - val) * m + val
        return val
    return {"ema_10": ema(prices, 10), "ema_20": ema(prices, 20), "ema_100": ema(prices, 100)}

def compute_pdh_pdl(tickers, ohlc):
    above, below = [], []
    for sym, candle in ohlc.items():
        t = tickers.get(sym)
        if not t: continue
        mp = float(t["mark_price"])
        chg = _ticker_change_24h(t)
        if mp > candle["high"]:
            p = round(((mp - candle["high"]) / candle["high"]) * 100, 2)
            above.append({"symbol": sym, "mark_price": mp, "prev_high": candle["high"], "pct_above": p, "change_24h": chg})
        elif mp < candle["low"]:
            p = round(((candle["low"] - mp) / candle["low"]) * 100, 2)
            below.append({"symbol": sym, "mark_price": mp, "prev_low": candle["low"], "pct_below": p, "change_24h": chg})
    return sorted(above, key=lambda x: x["pct_above"], reverse=True), sorted(below, key=lambda x: x["pct_below"], reverse=True)

def compute_ema_screener(tickers, emas, filter_symbols=None):
    res = []
    for sym, e in emas.items():
        if filter_symbols and sym not in filter_symbols: continue
        t = tickers.get(sym)
        if not t: continue
        mp = float(t["mark_price"])
        if mp > e["ema_10"] and mp > e["ema_20"] and mp > e["ema_100"]:
            p100 = round(((mp - e["ema_100"]) / e["ema_100"]) * 100, 2)
            res.append({
                "symbol": sym,
                "mark_price": mp,
                "ema_10": e["ema_10"],
                "ema_20": e["ema_20"],
                "ema_100": e["ema_100"],
                "pct_above_100": p100,
            })
    return sorted(res, key=lambda x: x.get("pct_above_100", 0), reverse=True)

# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------

def ohlc_refresh_loop():
    while True:
        try:
            futs = fetch_perpetual_futures()
            if futs:
                cache.set_futures(futs)
                s, e = get_ist_timestamps()
                res = {}
                with ThreadPoolExecutor(max_workers=20) as ex:
                    f_map = {
                        ex.submit(fetch_ohlc_one, sym, s, e): sym
                        for f in futs
                        if (sym := f.get("symbol"))
                    }
                    for f in as_completed(f_map):
                        sym, candle = f.result()
                        if candle: res[sym] = candle
                cache.set_ohlc(res)
        except Exception as err: log.error("OHLC Error: %s", err)
        time.sleep(300)

def ema_refresh_loop():
    while True:
        try:
            if cache.futures:
                end = int(time.time())
                start = end - (200 * 5 * 60)
                res = {}
                ich_list = []
                ich_bear_list = []

                def load_5m(sym):
                    _, bars = fetch_5m_ohlcv_one(sym, start, end)
                    closes = [b["close"] for b in bars]
                    e = calc_emas(closes) if len(closes) >= 100 else None
                    bull = try_ichimoku_stack_match(sym, bars, start, end)
                    bear = try_ichimoku_bear_match(sym, bars, start, end)
                    return sym, e, bull, bear

                with ThreadPoolExecutor(max_workers=20) as ex:
                    syms = [f.get("symbol") for f in cache.futures if f.get("symbol")]
                    f_map = {ex.submit(load_5m, sym): sym for sym in syms}
                    for fut in as_completed(f_map):
                        sym = f_map[fut]
                        try:
                            _, e, bull, bear = fut.result()
                        except Exception as exc:
                            log.debug("5m batch %s: %s", sym, exc)
                            continue
                        if e:
                            res[sym] = e
                        if bull:
                            ich_list.append(bull)
                        if bear:
                            ich_bear_list.append(bear)
                cache.set_emas(res)
                cache.set_ichimoku_stack(sorted(ich_list, key=lambda x: x["rvol"], reverse=True))
                cache.set_ichimoku_bear(sorted(ich_bear_list, key=lambda x: x["rvol"], reverse=True))
        except Exception as err: log.error("EMA Error: %s", err)
        time.sleep(300)

def breakout_monitor_loop():
    b_state, d_state = {}, {}
    first_run = True
    while True:
        try:
            if cache.ohlc_ready and cache.futures_ready:
                above = cache.get_above_pdh()
                for i in above:
                    s = i["symbol"]
                    if not b_state.get(s):
                        b_state[s] = True
                        socketio.emit("signal_alert", {"type": "PDH BREAKOUT", "symbol": s, "price": i["mark_price"], "detail": f"Above High ({i['prev_high']})", "color": "var(--md-green)", "time": datetime.datetime.now().strftime("%H:%M:%S"), "is_historical": first_run})
                
                below = cache.get_below_pdl()
                for i in below:
                    s = i["symbol"]
                    if not d_state.get(s):
                        d_state[s] = True
                        socketio.emit("signal_alert", {"type": "PDL BREAKDOWN", "symbol": s, "price": i["mark_price"], "detail": f"Below Low ({i['prev_low']})", "color": "var(--md-red)", "time": datetime.datetime.now().strftime("%H:%M:%S"), "is_historical": first_run})
                
                first_run = False
        except Exception as err: log.error("Breakout Error: %s", err)
        time.sleep(10)

def ticker_refresh_loop():
    first_run = True
    while True:
        try:
            ticks = fetch_all_tickers()
            if ticks:
                cache.set_tickers(ticks)
                cache.futures_ready = True
                t_snap, o_snap, e_snap = cache.snapshot_for_calculations()
                
                a_pdh, b_pdl = compute_pdh_pdl(t_snap, o_snap)
                cache.set_above_pdh(a_pdh)
                cache.set_below_pdl(b_pdl)
                
                if cache.ema_ready:
                    ema_data = compute_ema_screener(t_snap, e_snap)
                    cache.set_above_ema(ema_data)
                    conf_data = compute_ema_screener(t_snap, e_snap, filter_symbols={x["symbol"] for x in a_pdh})
                    cache.set_pdh_ema_confluence(conf_data)

                    curr_ema = {x["symbol"] for x in ema_data}
                    curr_conf = {x["symbol"] for x in conf_data}

                    new_conf = curr_conf if first_run else (curr_conf - cache.last_confluence_symbols)
                    for s in new_conf:
                        d = next(i for i in conf_data if i["symbol"] == s)
                        socketio.emit("signal_alert", {"type": "CONFLUENCE", "symbol": s, "price": d["mark_price"], "detail": "PDH + EMA Trend", "color": "var(--md-purple)", "time": datetime.datetime.now().strftime("%H:%M:%S"), "is_historical": first_run})

                    new_ema = (curr_ema if first_run else (curr_ema - cache.last_ema_symbols)) - curr_conf
                    for s in new_ema:
                        d = next(i for i in ema_data if i["symbol"] == s)
                        socketio.emit("signal_alert", {"type": "SUPER MOMENTUM", "symbol": s, "price": d["mark_price"], "detail": "Strong EMA Trend", "color": "var(--md-blue)", "time": datetime.datetime.now().strftime("%H:%M:%S"), "is_historical": first_run})

                    ich_data = cache.get_ichimoku_stack()
                    curr_ich = {x["symbol"] for x in ich_data}
                    new_ich = (curr_ich if first_run else (curr_ich - cache.last_ichimoku_symbols))
                    for s in new_ich:
                        d = next(i for i in ich_data if i["symbol"] == s)
                        socketio.emit("signal_alert", {
                            "type": "ICHIMOKU STACK",
                            "symbol": s,
                            "price": d["close"],
                            "detail": "5m close: cloud + TK + lagging + RVOL + OI",
                            "color": "var(--md-amber)",
                            "time": datetime.datetime.now().strftime("%H:%M:%S"),
                            "is_historical": first_run,
                        })

                    bear_data = cache.get_ichimoku_bear()
                    curr_bear = {x["symbol"] for x in bear_data}
                    new_bear = (curr_bear if first_run else (curr_bear - cache.last_ichimoku_bear_symbols))
                    for s in new_bear:
                        d = next(i for i in bear_data if i["symbol"] == s)
                        socketio.emit("signal_alert", {
                            "type": "ICHIMOKU BEAR",
                            "symbol": s,
                            "price": d["close"],
                            "detail": "5m close: below cloud + TK ↓ + lagging ↓ + RVOL + OI",
                            "color": "var(--md-red)",
                            "time": datetime.datetime.now().strftime("%H:%M:%S"),
                            "is_historical": first_run,
                        })

                    cache.last_ema_symbols = curr_ema
                    cache.last_confluence_symbols = curr_conf
                    cache.last_ichimoku_symbols = curr_ich
                    cache.last_ichimoku_bear_symbols = curr_bear
                    first_run = False
        except Exception as err: log.error("Ticker Error: %s", err)
        time.sleep(10)

@app.route("/")
def index(): return render_template("index.html")

@app.route("/market-breadth")
def breadth(): return jsonify(cache.get_market_breadth())

@app.route("/futures")
def futs():
    rows = cache.get_futures_table()
    # If we only have placeholder rows, nudge ticker fetch (throttled)
    if rows and all(r.get("pending") for r in rows) and cache.has_perpetual_products():
        now = time.time()
        last = getattr(futs, "_last_ticker_pull", 0.0)
        if now - last >= 8.0:
            futs._last_ticker_pull = now
            _bootstrap_tickers_once()
            rows = cache.get_futures_table()
    elif not rows and cache.has_perpetual_products():
        now = time.time()
        last = getattr(futs, "_last_ticker_pull", 0.0)
        if now - last >= 8.0:
            futs._last_ticker_pull = now
            _bootstrap_tickers_once()
        rows = cache.get_futures_table()
    return jsonify(rows)

@app.route("/above-pdh")
def apdh(): return jsonify({"status": "ok", "data": cache.get_above_pdh()})

@app.route("/below-pdl")
def bpdl(): return jsonify({"status": "ok", "data": cache.get_below_pdl()})

@app.route("/above-ema")
def aema(): return jsonify({"status": "ok", "data": cache.get_above_ema()})

@app.route("/pdh-ema-confluence")
def conf(): return jsonify({"status": "ok", "data": cache.get_pdh_ema_confluence()})

@app.route("/ichimoku-stack")
def ichimoku_stack():
    data = cache.get_ichimoku_stack()
    tickers = cache.tickers  # live, refreshes every 10 s
    live = []
    for d in data:
        t = tickers.get(d["symbol"])
        if not t:
            continue
        try:
            mp = float(t.get("mark_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if mp > d["senkou_a"] and mp > d["senkou_b"]:
            live.append(d)
    return jsonify({"status": "ok", "data": live})

@app.route("/ichimoku-bear")
def ichimoku_bear_route():
    data = cache.get_ichimoku_bear()
    tickers = cache.tickers  # live, refreshes every 10 s
    live = []
    for d in data:
        t = tickers.get(d["symbol"])
        if not t:
            continue
        try:
            mp = float(t.get("mark_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        # Re-filter: live mark price must still be below the cloud
        if mp < d["senkou_a"] and mp < d["senkou_b"]:
            live.append(d)
    return jsonify({"status": "ok", "data": live})

# ---------------------------------------------------------------------------
# Background workers (must run for `flask run`, not only `python app.py`)
# ---------------------------------------------------------------------------

_bg_lock = threading.Lock()
_bg_started = False


def _should_start_background_workers() -> bool:
    """Start workers in every normal server process. Opt out only for tests/tools."""
    if os.environ.get("DELTA_DASH_NO_WORKERS", "").lower() in ("1", "true", "yes"):
        return False
    return True


def _bootstrap_tickers_once():
    """So /futures works on first browser request without waiting for the ticker thread."""
    try:
        ticks = fetch_all_tickers()
        if ticks:
            cache.set_tickers(ticks)
            cache.futures_ready = True
            log.info("Initial ticker cache: %d symbols", len(ticks))
        else:
            log.warning("Initial ticker fetch returned empty; ticker thread will retry")
    except Exception as err:
        log.warning("Initial ticker fetch failed: %s", err)


def ensure_background_workers():
    """Populate cache from Delta in background threads. Safe to call once per process."""
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True

    try:
        boot = fetch_perpetual_futures()
        if boot:
            cache.set_futures(boot)
    except Exception as err:
        log.exception("Bootstrap perpetual list failed: %s", err)

    _bootstrap_tickers_once()

    threading.Thread(target=ohlc_refresh_loop, daemon=True).start()
    threading.Thread(target=ema_refresh_loop, daemon=True).start()
    threading.Thread(target=ticker_refresh_loop, daemon=True).start()
    threading.Thread(target=breakout_monitor_loop, daemon=True).start()
    log.info("Delta dashboard background workers started")


if _should_start_background_workers():
    ensure_background_workers()

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)