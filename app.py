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
        self.ichimoku_classic_bull: list = []
        self.ichimoku_classic_bear: list = []
        self.ema_pullback_buy: list = []
        self.ema_pullback_sell: list = []
        self.futures_ready = False       
        self.ohlc_ready = False
        self.ema_ready = False
        self.last_ema_symbols = set()
        self.last_confluence_symbols = set()
        self.last_ichimoku_symbols = set()
        self.last_ichimoku_bear_symbols = set()
        self.last_ichimoku_classic_bull_symbols = set()
        self.last_ichimoku_classic_bear_symbols = set()
        self.last_ema_pullback_buy_symbols = set()
        self.last_ema_pullback_sell_symbols = set()

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

    def set_ichimoku_classic_bull(self, data: list):
        with self._lock: self.ichimoku_classic_bull = data

    def get_ichimoku_classic_bull(self) -> list:
        with self._lock: return list(self.ichimoku_classic_bull)

    def set_ichimoku_classic_bear(self, data: list):
        with self._lock: self.ichimoku_classic_bear = data

    def get_ichimoku_classic_bear(self) -> list:
        with self._lock: return list(self.ichimoku_classic_bear)

    def set_ema_pullback_buy(self, data: list):
        with self._lock: self.ema_pullback_buy = data

    def get_ema_pullback_buy(self) -> list:
        with self._lock: return list(self.ema_pullback_buy)

    def set_ema_pullback_sell(self, data: list):
        with self._lock: self.ema_pullback_sell = data

    def get_ema_pullback_sell(self) -> list:
        with self._lock: return list(self.ema_pullback_sell)

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



def try_ichimoku_classic_bull(symbol: str, bars: list) -> dict | None:
    """Ichimoku Classic bullish scan — pure signal, no RVOL/OI filter.

    Conditions on the last fully closed 5m bar (index i):
    1. TK Crossover: tenkan[i] > kijun[i]  AND  tenkan[i-1] <= kijun[i-1]
    2. Chikou (Lagging Span) above price 26 bars ago: close[i] > close[i-26]
    3. Cloud breakout: close[i] > senkou_a AND close[i] > senkou_b,
       AND close[i-1] was AT or BELOW the cloud top (just broke out)

    Cloud at bar i is calculated from j = i-26 (standard 26-bar displacement).
    Cloud at bar i-1 is calculated from j-1 = i-27.
    """
    if len(bars) < ICHIMOKU_MIN_INDEX + 2:
        return None
    i = _last_closed_bar_index(bars)
    if i is None or i < ICHIMOKU_MIN_INDEX + 1:
        return None

    highs  = [b["high"]  for b in bars]
    lows   = [b["low"]   for b in bars]
    closes = [b["close"] for b in bars]
    close_i  = closes[i]
    close_i1 = closes[i - 1]

    # --- Condition 1: TK crossover (Tenkan crosses above Kijun) ---
    ten_i  = _donchian_mid(highs, lows, i,     9)
    kij_i  = _donchian_mid(highs, lows, i,    26)
    ten_i1 = _donchian_mid(highs, lows, i - 1, 9)
    kij_i1 = _donchian_mid(highs, lows, i - 1, 26)
    if None in (ten_i, kij_i, ten_i1, kij_i1):
        return None
    if not (ten_i > kij_i and ten_i1 <= kij_i1):
        return None

    # --- Condition 2: Chikou (Lagging Span) above price 26 bars back ---
    if close_i <= closes[i - 26]:
        return None

    # --- Condition 3: Cloud breakout (current bar above cloud, prev bar at/below cloud) ---
    # Cloud applicable to bar i is built from j = i-26
    j = i - 26
    ta_j  = _donchian_mid(highs, lows, j, 9)
    kj_j  = _donchian_mid(highs, lows, j, 26)
    sb_j  = _donchian_mid(highs, lows, j, 52)
    if None in (ta_j, kj_j, sb_j):
        return None
    senkou_a = (ta_j + kj_j) / 2.0
    senkou_b = sb_j
    cloud_top = max(senkou_a, senkou_b)

    # Current bar must be above both cloud boundaries
    if not (close_i > senkou_a and close_i > senkou_b):
        return None

    # Cloud applicable to bar i-1 is built from j-1 = i-27
    j1 = i - 27
    ta_j1 = _donchian_mid(highs, lows, j1, 9)
    kj_j1 = _donchian_mid(highs, lows, j1, 26)
    sb_j1 = _donchian_mid(highs, lows, j1, 52)
    if None in (ta_j1, kj_j1, sb_j1):
        return None
    sa_prev = (ta_j1 + kj_j1) / 2.0
    sb_prev = sb_j1
    cloud_top_prev = max(sa_prev, sb_prev)

    # Previous bar must have been AT or INSIDE/BELOW the cloud (breakout just happened)
    if close_i1 > cloud_top_prev:
        return None

    return {
        "symbol":   symbol,
        "close":    close_i,
        "mark_price": close_i,
        "senkou_a": round(senkou_a, 6),
        "senkou_b": round(senkou_b, 6),
        "tenkan":   round(ten_i, 6),
        "kijun":    round(kij_i, 6),
        "chikou_vs_price": round(closes[i - 26], 6),
    }


def try_ichimoku_classic_bear(symbol: str, bars: list) -> dict | None:
    """Ichimoku Classic bearish scan — exact mirror of the bullish scan.

    Conditions on the last fully closed 5m bar (index i):
    1. TK Crossover: tenkan[i] < kijun[i]  AND  tenkan[i-1] >= kijun[i-1]
    2. Chikou (Lagging Span) below price 26 bars ago: close[i] < close[i-26]
    3. Cloud breakdown: close[i] < senkou_a AND close[i] < senkou_b,
       AND close[i-1] was AT or ABOVE the cloud bottom (just broke down)
    """
    if len(bars) < ICHIMOKU_MIN_INDEX + 2:
        return None
    i = _last_closed_bar_index(bars)
    if i is None or i < ICHIMOKU_MIN_INDEX + 1:
        return None

    highs  = [b["high"]  for b in bars]
    lows   = [b["low"]   for b in bars]
    closes = [b["close"] for b in bars]
    close_i  = closes[i]
    close_i1 = closes[i - 1]

    # --- Condition 1: TK crossover (Tenkan crosses below Kijun) ---
    ten_i  = _donchian_mid(highs, lows, i,     9)
    kij_i  = _donchian_mid(highs, lows, i,    26)
    ten_i1 = _donchian_mid(highs, lows, i - 1, 9)
    kij_i1 = _donchian_mid(highs, lows, i - 1, 26)
    if None in (ten_i, kij_i, ten_i1, kij_i1):
        return None
    if not (ten_i < kij_i and ten_i1 >= kij_i1):
        return None

    # --- Condition 2: Chikou (Lagging Span) below price 26 bars back ---
    if close_i >= closes[i - 26]:
        return None

    # --- Condition 3: Cloud breakdown (current bar below cloud, prev bar at/above cloud) ---
    j = i - 26
    ta_j = _donchian_mid(highs, lows, j, 9)
    kj_j = _donchian_mid(highs, lows, j, 26)
    sb_j = _donchian_mid(highs, lows, j, 52)
    if None in (ta_j, kj_j, sb_j):
        return None
    senkou_a = (ta_j + kj_j) / 2.0
    senkou_b = sb_j
    cloud_bot = min(senkou_a, senkou_b)

    # Current bar must be below both cloud boundaries
    if not (close_i < senkou_a and close_i < senkou_b):
        return None

    # Cloud applicable to bar i-1 is built from j-1 = i-27
    j1 = i - 27
    ta_j1 = _donchian_mid(highs, lows, j1, 9)
    kj_j1 = _donchian_mid(highs, lows, j1, 26)
    sb_j1 = _donchian_mid(highs, lows, j1, 52)
    if None in (ta_j1, kj_j1, sb_j1):
        return None
    sa_prev = (ta_j1 + kj_j1) / 2.0
    sb_prev = sb_j1
    cloud_bot_prev = min(sa_prev, sb_prev)

    # Previous bar must have been AT or INSIDE/ABOVE the cloud (breakdown just happened)
    if close_i1 < cloud_bot_prev:
        return None

    return {
        "symbol":   symbol,
        "close":    close_i,
        "mark_price": close_i,
        "senkou_a": round(senkou_a, 6),
        "senkou_b": round(senkou_b, 6),
        "tenkan":   round(ten_i, 6),
        "kijun":    round(kij_i, 6),
        "chikou_vs_price": round(closes[i - 26], 6),
    }


def calc_emas(prices):
    if len(prices) < 100: return None
    def ema(p, n):
        m = 2 / (n + 1)
        val = sum(p[:n]) / n
        for x in p[n:]: val = (x - val) * m + val
        return val
    return {"ema_10": ema(prices, 10), "ema_20": ema(prices, 20), "ema_100": ema(prices, 100)}


def calc_ema_series(prices: list[float], n: int) -> list[float | None]:
    """Return an EMA value for every bar in *prices*.

    The first (n-1) entries are None (not enough data).  From index n-1
    onward, a standard EMA is computed.
    """
    out: list[float | None] = [None] * len(prices)
    if len(prices) < n:
        return out
    m = 2.0 / (n + 1)
    val = sum(prices[:n]) / n
    out[n - 1] = val
    for idx in range(n, len(prices)):
        val = (prices[idx] - val) * m + val
        out[idx] = val
    return out


PULLBACK_LOOKBACK = 4  # check last N fully-closed bars


def try_ema_pullback_buy(symbol: str, bars: list) -> dict | None:
    """EMA Pullback Buy — hardened, checks the LAST fully-closed 5m bar.

    Conditions (all must pass):
      1. Close above EMA 10, 20, 100
      2. EMA stacking: EMA 10 > EMA 20 > EMA 100 (clean uptrend)
      3. EMA 20 slope rising: ema20[i] > ema20[i-5]
      4. Low dips to or below EMA 10 (pullback into zone), close holds above
      5. Rejection candle: lower wick >= 1.5× body, close in upper 1/3 of bar
      6. Prior impulsive move: at least one large bullish bar in last 12 bars
    """
    if len(bars) < 108:
        return None
    i = _last_closed_bar_index(bars)
    if i is None or i < 107:
        return None

    closes = [b["close"] for b in bars]
    ema10s = calc_ema_series(closes, 10)
    ema20s = calc_ema_series(closes, 20)
    ema100s = calc_ema_series(closes, 100)

    e10 = ema10s[i]
    e20 = ema20s[i]
    e100 = ema100s[i]
    if e10 is None or e20 is None or e100 is None:
        return None

    bar = bars[i]
    close_i = bar["close"]
    open_i = bar["open"]
    low_i = bar["low"]
    high_i = bar["high"]

    # --- Condition 1: close above all 3 EMAs ---
    if not (close_i > e10 and close_i > e20 and close_i > e100):
        return None

    # --- Condition 2: EMA stacking order (clean uptrend) ---
    if not (e10 > e20 > e100):
        return None

    # --- Condition 3: EMA 20 slope — must be rising ---
    e20_prev = ema20s[i - 5] if i >= 5 else None
    if e20_prev is None or e20 <= e20_prev:
        return None

    # --- Condition 4: Pullback rejection at EMA zone ---
    if low_i > e10:
        return None
    if close_i <= e10:
        return None

    # --- Condition 5: Rejection candle shape + close position ---
    body = abs(close_i - open_i)
    lower_wick = min(close_i, open_i) - low_i
    bar_range = high_i - low_i
    if bar_range <= 0:
        return None
    if body > 0 and lower_wick < 1.5 * body:
        return None
    if body <= 0 and lower_wick < 0.5 * bar_range:
        return None
    # Close must be in upper 1/3 of bar range (strong rejection)
    close_position = (close_i - low_i) / bar_range
    if close_position < 0.66:
        return None

    # --- Condition 6: Prior impulsive bullish move in last 12 bars ---
    # Compute median bar range for context, then find a large bullish bar
    lookback_start = max(0, i - 12)
    ranges = [bars[j]["high"] - bars[j]["low"] for j in range(lookback_start, i)]
    if not ranges:
        return None
    median_range = sorted(ranges)[len(ranges) // 2]
    if median_range <= 0:
        return None
    has_impulse = False
    for j in range(lookback_start, i):
        b = bars[j]
        b_body = b["close"] - b["open"]  # positive = bullish
        if b_body > 0 and b_body >= 1.5 * median_range:
            has_impulse = True
            break
    if not has_impulse:
        return None

    # Determine candle pattern label
    if body <= 0:
        pattern = "Doji"
    elif close_i >= open_i:
        pattern = "Hammer"
    else:
        pattern = "Pinbar"

    return {
        "symbol": symbol, "close": close_i, "mark_price": close_i,
        "ema_10": e10, "ema_20": e20, "ema_100": e100,
        "candle_pattern": pattern,
        "pct_above_100": round(((close_i - e100) / e100) * 100, 2),
    }


def try_ema_pullback_sell(symbol: str, bars: list) -> dict | None:
    """EMA Pullback Sell — hardened, checks the LAST fully-closed 5m bar.

    Conditions (all must pass):
      1. Close below EMA 10, 20, 100
      2. EMA stacking: EMA 10 < EMA 20 < EMA 100 (clean downtrend)
      3. EMA 20 slope falling: ema20[i] < ema20[i-5]
      4. High reaches to or above EMA 10 (rally into zone), close stays below
      5. Rejection candle: upper wick >= 1.5× body, close in lower 1/3 of bar
      6. Prior impulsive move: at least one large bearish bar in last 12 bars
    """
    if len(bars) < 108:
        return None
    i = _last_closed_bar_index(bars)
    if i is None or i < 107:
        return None

    closes = [b["close"] for b in bars]
    ema10s = calc_ema_series(closes, 10)
    ema20s = calc_ema_series(closes, 20)
    ema100s = calc_ema_series(closes, 100)

    e10 = ema10s[i]
    e20 = ema20s[i]
    e100 = ema100s[i]
    if e10 is None or e20 is None or e100 is None:
        return None

    bar = bars[i]
    close_i = bar["close"]
    open_i = bar["open"]
    high_i = bar["high"]
    low_i = bar["low"]

    # --- Condition 1: close below all 3 EMAs ---
    if not (close_i < e10 and close_i < e20 and close_i < e100):
        return None

    # --- Condition 2: EMA stacking order (clean downtrend) ---
    if not (e10 < e20 < e100):
        return None

    # --- Condition 3: EMA 20 slope — must be falling ---
    e20_prev = ema20s[i - 5] if i >= 5 else None
    if e20_prev is None or e20 >= e20_prev:
        return None

    # --- Condition 4: Rally rejection at EMA zone ---
    if high_i < e10:
        return None
    if close_i >= e10:
        return None

    # --- Condition 5: Rejection candle shape + close position ---
    body = abs(close_i - open_i)
    upper_wick = high_i - max(close_i, open_i)
    bar_range = high_i - low_i
    if bar_range <= 0:
        return None
    if body > 0 and upper_wick < 1.5 * body:
        return None
    if body <= 0 and upper_wick < 0.5 * bar_range:
        return None
    # Close must be in lower 1/3 of bar range (strong rejection)
    close_position = (close_i - low_i) / bar_range
    if close_position > 0.34:
        return None

    # --- Condition 6: Prior impulsive bearish move in last 12 bars ---
    lookback_start = max(0, i - 12)
    ranges = [bars[j]["high"] - bars[j]["low"] for j in range(lookback_start, i)]
    if not ranges:
        return None
    median_range = sorted(ranges)[len(ranges) // 2]
    if median_range <= 0:
        return None
    has_impulse = False
    for j in range(lookback_start, i):
        b = bars[j]
        b_body = b["open"] - b["close"]  # positive = bearish
        if b_body > 0 and b_body >= 1.5 * median_range:
            has_impulse = True
            break
    if not has_impulse:
        return None

    if body <= 0:
        pattern = "Doji"
    elif close_i <= open_i:
        pattern = "Shooting Star"
    else:
        pattern = "Inv Hammer"

    return {
        "symbol": symbol, "close": close_i, "mark_price": close_i,
        "ema_10": e10, "ema_20": e20, "ema_100": e100,
        "candle_pattern": pattern,
        "pct_below_100": round(((e100 - close_i) / e100) * 100, 2),
    }

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
                ich_classic_bull_list = []
                ich_classic_bear_list = []
                ema_pb_buy_list = []
                ema_pb_sell_list = []

                def load_5m(sym):
                    _, bars = fetch_5m_ohlcv_one(sym, start, end)
                    closes = [b["close"] for b in bars]
                    e = calc_emas(closes) if len(closes) >= 100 else None
                    bull = try_ichimoku_stack_match(sym, bars, start, end)
                    bear = try_ichimoku_bear_match(sym, bars, start, end)
                    classic_bull = try_ichimoku_classic_bull(sym, bars)
                    classic_bear = try_ichimoku_classic_bear(sym, bars)
                    epb_buy = try_ema_pullback_buy(sym, bars)
                    epb_sell = try_ema_pullback_sell(sym, bars)
                    return sym, e, bull, bear, classic_bull, classic_bear, epb_buy, epb_sell

                with ThreadPoolExecutor(max_workers=20) as ex:
                    syms = [f.get("symbol") for f in cache.futures if f.get("symbol")]
                    f_map = {ex.submit(load_5m, sym): sym for sym in syms}
                    for fut in as_completed(f_map):
                        sym = f_map[fut]
                        try:
                            _, e, bull, bear, cb, cbear, epb_b, epb_s = fut.result()
                        except Exception as exc:
                            log.debug("5m batch %s: %s", sym, exc)
                            continue
                        if e:
                            res[sym] = e
                        if bull:
                            ich_list.append(bull)
                        if bear:
                            ich_bear_list.append(bear)
                        if cb:
                            ich_classic_bull_list.append(cb)
                        if cbear:
                            ich_classic_bear_list.append(cbear)
                        if epb_b:
                            ema_pb_buy_list.append(epb_b)
                        if epb_s:
                            ema_pb_sell_list.append(epb_s)
                cache.set_emas(res)
                cache.set_ichimoku_stack(sorted(ich_list, key=lambda x: x["rvol"], reverse=True))
                cache.set_ichimoku_bear(sorted(ich_bear_list, key=lambda x: x["rvol"], reverse=True))
                cache.set_ichimoku_classic_bull(sorted(ich_classic_bull_list, key=lambda x: x["close"], reverse=True))
                cache.set_ichimoku_classic_bear(sorted(ich_classic_bear_list, key=lambda x: x["close"], reverse=True))
                cache.set_ema_pullback_buy(sorted(ema_pb_buy_list, key=lambda x: x["pct_above_100"], reverse=True))
                cache.set_ema_pullback_sell(sorted(ema_pb_sell_list, key=lambda x: x["pct_below_100"], reverse=True))
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

                    # EMA Pullback Buy alerts
                    pb_buy_data = cache.get_ema_pullback_buy()
                    curr_pb_buy = {x["symbol"] for x in pb_buy_data}
                    new_pb_buy = curr_pb_buy if first_run else (curr_pb_buy - cache.last_ema_pullback_buy_symbols)
                    for s in new_pb_buy:
                        d = next(i for i in pb_buy_data if i["symbol"] == s)
                        socketio.emit("signal_alert", {
                            "type": "EMA PULLBACK BUY",
                            "symbol": s,
                            "price": d["close"],
                            "detail": f"{d['candle_pattern']} rejection at EMA zone",
                            "color": "var(--md-blue)",
                            "time": datetime.datetime.now().strftime("%H:%M:%S"),
                            "is_historical": first_run,
                        })

                    # EMA Pullback Sell alerts
                    pb_sell_data = cache.get_ema_pullback_sell()
                    curr_pb_sell = {x["symbol"] for x in pb_sell_data}
                    new_pb_sell = curr_pb_sell if first_run else (curr_pb_sell - cache.last_ema_pullback_sell_symbols)
                    for s in new_pb_sell:
                        d = next(i for i in pb_sell_data if i["symbol"] == s)
                        socketio.emit("signal_alert", {
                            "type": "EMA PULLBACK SELL",
                            "symbol": s,
                            "price": d["close"],
                            "detail": f"{d['candle_pattern']} rejection at EMA zone",
                            "color": "var(--md-red)",
                            "time": datetime.datetime.now().strftime("%H:%M:%S"),
                            "is_historical": first_run,
                        })

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

                    classic_bull_data = cache.get_ichimoku_classic_bull()
                    curr_classic_bull = {x["symbol"] for x in classic_bull_data}
                    new_classic_bull = (curr_classic_bull if first_run else (curr_classic_bull - cache.last_ichimoku_classic_bull_symbols))
                    for s in new_classic_bull:
                        d = next(i for i in classic_bull_data if i["symbol"] == s)
                        socketio.emit("signal_alert", {
                            "type": "ICHIMOKU CLASSIC BULL",
                            "symbol": s,
                            "price": d["close"],
                            "detail": "TK cross ↑ · Chikou above · Cloud breakout ↑",
                            "color": "var(--green)",
                            "time": datetime.datetime.now().strftime("%H:%M:%S"),
                            "is_historical": first_run,
                        })

                    classic_bear_data = cache.get_ichimoku_classic_bear()
                    curr_classic_bear = {x["symbol"] for x in classic_bear_data}
                    new_classic_bear = (curr_classic_bear if first_run else (curr_classic_bear - cache.last_ichimoku_classic_bear_symbols))
                    for s in new_classic_bear:
                        d = next(i for i in classic_bear_data if i["symbol"] == s)
                        socketio.emit("signal_alert", {
                            "type": "ICHIMOKU CLASSIC BEAR",
                            "symbol": s,
                            "price": d["close"],
                            "detail": "TK cross ↓ · Chikou below · Cloud breakdown ↓",
                            "color": "var(--red)",
                            "time": datetime.datetime.now().strftime("%H:%M:%S"),
                            "is_historical": first_run,
                        })

                    cache.last_ema_symbols = curr_ema
                    cache.last_confluence_symbols = curr_conf
                    cache.last_ichimoku_symbols = curr_ich
                    cache.last_ichimoku_bear_symbols = curr_bear
                    cache.last_ichimoku_classic_bull_symbols = curr_classic_bull
                    cache.last_ichimoku_classic_bear_symbols = curr_classic_bear
                    cache.last_ema_pullback_buy_symbols = curr_pb_buy
                    cache.last_ema_pullback_sell_symbols = curr_pb_sell
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

@app.route("/ema-pullback-buy")
def ema_pullback_buy_route():
    data = cache.get_ema_pullback_buy()
    tickers = cache.tickers
    live = []
    for d in data:
        t = tickers.get(d["symbol"])
        if not t:
            continue
        try:
            mp = float(t.get("mark_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if mp > d["ema_10"] and mp > d["ema_20"] and mp > d["ema_100"]:
            live.append({**d, "mark_price": mp})
    return jsonify({"status": "ok", "data": live})

@app.route("/ema-pullback-sell")
def ema_pullback_sell_route():
    data = cache.get_ema_pullback_sell()
    tickers = cache.tickers
    live = []
    for d in data:
        t = tickers.get(d["symbol"])
        if not t:
            continue
        try:
            mp = float(t.get("mark_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if mp < d["ema_10"] and mp < d["ema_20"] and mp < d["ema_100"]:
            live.append({**d, "mark_price": mp})
    return jsonify({"status": "ok", "data": live})

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

@app.route("/ichimoku-classic-bull")
def ichimoku_classic_bull_route():
    data = cache.get_ichimoku_classic_bull()
    tickers = cache.tickers
    live = []
    for d in data:
        t = tickers.get(d["symbol"])
        if not t:
            continue
        try:
            mp = float(t.get("mark_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        # Re-filter: live mark price still above both cloud boundaries
        if mp > d["senkou_a"] and mp > d["senkou_b"]:
            live.append({**d, "mark_price": mp})
    return jsonify({"status": "ok", "data": live})


@app.route("/ichimoku-classic-bear")
def ichimoku_classic_bear_route():
    data = cache.get_ichimoku_classic_bear()
    tickers = cache.tickers
    live = []
    for d in data:
        t = tickers.get(d["symbol"])
        if not t:
            continue
        try:
            mp = float(t.get("mark_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        # Re-filter: live mark price still below both cloud boundaries
        if mp < d["senkou_a"] and mp < d["senkou_b"]:
            live.append({**d, "mark_price": mp})
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
    socketio.run(app, host="0.0.0.0", port=5001)