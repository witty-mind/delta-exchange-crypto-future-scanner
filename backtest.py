"""
Ichimoku 5m Backtest — Delta Exchange
======================================
Tests all four Ichimoku scans over the last 6 months of 5m candle data.

Scans tested
------------
1. ICHIMOKU STACK  (bull) – above cloud + TK bullish + Chikou above + RVOL > 1.5
2. ICHIMOKU BEAR          – below cloud + TK bearish + Chikou below + RVOL > 1.5
3. ICHIMOKU CLASSIC BULL  – TK crossover ↑ + Chikou above price + cloud breakout ↑
4. ICHIMOKU CLASSIC BEAR  – TK crossover ↓ + Chikou below price + cloud breakdown ↓

Forward-return horizons measured after each signal
---------------------------------------------------
  1 bar   =   5 minutes
  3 bars  =  15 minutes
  6 bars  =  30 minutes
 12 bars  =   1 hour
 24 bars  =   2 hours
 48 bars  =   4 hours
144 bars  =  12 hours
288 bars  =  24 hours

Usage
-----
  # Default: top 30 perpetuals by 24h volume, last 6 months
  python backtest.py

  # Specific symbols
  python backtest.py --symbols BTCUSDT ETHUSDT SOLUSDT

  # Custom look-back
  python backtest.py --months 3 --symbols BTCUSDT

  # All live perpetuals (slow, ~30-60 min)
  python backtest.py --all

Output
------
  backtest_results/
    signals_<timestamp>.csv   – every signal with forward returns
    summary_<timestamp>.txt   – per-scan win-rate & avg-return table
"""

import argparse
import csv
import datetime
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DELTA_BASE = "https://api.india.delta.exchange/v2"
SESSION = requests.Session()

CANDLE_5M_SEC = 5 * 60
ICHIMOKU_MIN_INDEX = 77   # minimum bars needed before first ichimoku value

# Forward-return horizons (in bars; 1 bar = 5 min)
HORIZONS = [1, 3, 6, 12, 24, 48, 144, 288]
HORIZON_LABELS = ["5m", "15m", "30m", "1h", "2h", "4h", "12h", "24h"]

OUTPUT_DIR = "backtest_results"

# Delta API returns at most ~500 candles per call for 5m resolution.
# We chunk requests into 2-day windows to stay well inside that limit.
CHUNK_DAYS = 2

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 15) -> dict | None:
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 2:
                log.debug("GET %s failed after 3 attempts: %s", url, e)
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_perpetual_futures() -> list:
    """Fetch all live perpetual futures symbols."""
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


def fetch_5m_candles_chunked(symbol: str, start_ts: int, end_ts: int) -> list:
    """
    Fetch all 5m OHLCV bars from start_ts to end_ts using chunked requests.
    Returns a sorted list of bar dicts: {time, open, high, low, close, volume}.
    """
    all_bars = []
    chunk_sec = CHUNK_DAYS * 86400
    chunk_start = start_ts

    while chunk_start < end_ts:
        chunk_end = min(chunk_start + chunk_sec, end_ts)
        q = urlencode({
            "resolution": "5m",
            "symbol": symbol,
            "start": str(chunk_start),
            "end": str(chunk_end),
        })
        data = _get(f"{DELTA_BASE}/history/candles?{q}")
        if data and data.get("result"):
            for c in data["result"]:
                try:
                    all_bars.append({
                        "time":   int(c["time"]),
                        "open":   float(c["open"]),
                        "high":   float(c["high"]),
                        "low":    float(c["low"]),
                        "close":  float(c["close"]),
                        "volume": float(c.get("volume") or 0),
                    })
                except (TypeError, ValueError, KeyError):
                    continue
        chunk_start = chunk_end
        time.sleep(0.12)   # gentle rate limiting (~8 req/sec max)

    # De-duplicate and sort
    seen = set()
    unique = []
    for b in all_bars:
        if b["time"] not in seen:
            seen.add(b["time"])
            unique.append(b)
    unique.sort(key=lambda x: x["time"])
    return unique


# ---------------------------------------------------------------------------
# Ichimoku calculation helpers  (identical logic to app.py)
# ---------------------------------------------------------------------------

def _donchian_mid(highs, lows, idx, length):
    start = idx - length + 1
    if start < 0 or idx >= len(highs):
        return None
    return (max(highs[start: idx + 1]) + min(lows[start: idx + 1])) / 2.0


def _rvol_prev50(volumes, i):
    if i < 50:
        return None
    prev = volumes[i - 50: i]
    avg = sum(prev) / 50.0
    if avg <= 0:
        return None
    return volumes[i] / avg


# ---------------------------------------------------------------------------
# Scan functions  (walk-forward on a full bar array, returns all signals)
# ---------------------------------------------------------------------------

def scan_ichimoku_stack_bull(bars: list) -> list[dict]:
    """
    Ichimoku Stack Bullish: identical to live scanner but walks ALL bars.
    Conditions at bar i (all bars, not just last-closed):
      - close[i] above cloud (Senkou A & B displaced -26)
      - Tenkan >= Kijun (TK bullish)
      - close[i] > close[i-26]  (Chikou above)
      - RVOL vs prior 50 bars > 1.5
    Note: OI filter omitted in backtest (historical OI data not available).
    """
    signals = []
    highs  = [b["high"]   for b in bars]
    lows   = [b["low"]    for b in bars]
    closes = [b["close"]  for b in bars]
    vols   = [b["volume"] for b in bars]

    for i in range(ICHIMOKU_MIN_INDEX, len(bars)):
        close_i = closes[i]

        # Cloud (displaced 26 back)
        j = i - 26
        ta_j = _donchian_mid(highs, lows, j, 9)
        kj_j = _donchian_mid(highs, lows, j, 26)
        sb_j = _donchian_mid(highs, lows, j, 52)
        if None in (ta_j, kj_j, sb_j):
            continue
        senkou_a = (ta_j + kj_j) / 2.0
        senkou_b = sb_j
        if not (close_i > senkou_a and close_i > senkou_b):
            continue

        # TK
        ten_i = _donchian_mid(highs, lows, i, 9)
        kij_i = _donchian_mid(highs, lows, i, 26)
        if ten_i is None or kij_i is None or ten_i < kij_i:
            continue

        # Chikou
        if close_i <= closes[i - 26]:
            continue

        # RVOL
        rv = _rvol_prev50(vols, i)
        if rv is None or rv <= 1.5:
            continue

        signals.append({
            "scan":     "ICHIMOKU_STACK_BULL",
            "bar_idx":  i,
            "time":     bars[i]["time"],
            "close":    close_i,
            "senkou_a": round(senkou_a, 6),
            "senkou_b": round(senkou_b, 6),
            "tenkan":   round(ten_i, 6),
            "kijun":    round(kij_i, 6),
            "rvol":     round(rv, 3),
        })

    return signals


def scan_ichimoku_stack_bear(bars: list) -> list[dict]:
    """Ichimoku Stack Bearish – exact mirror of bullish."""
    signals = []
    highs  = [b["high"]   for b in bars]
    lows   = [b["low"]    for b in bars]
    closes = [b["close"]  for b in bars]
    vols   = [b["volume"] for b in bars]

    for i in range(ICHIMOKU_MIN_INDEX, len(bars)):
        close_i = closes[i]

        j = i - 26
        ta_j = _donchian_mid(highs, lows, j, 9)
        kj_j = _donchian_mid(highs, lows, j, 26)
        sb_j = _donchian_mid(highs, lows, j, 52)
        if None in (ta_j, kj_j, sb_j):
            continue
        senkou_a = (ta_j + kj_j) / 2.0
        senkou_b = sb_j
        if not (close_i < senkou_a and close_i < senkou_b):
            continue

        ten_i = _donchian_mid(highs, lows, i, 9)
        kij_i = _donchian_mid(highs, lows, i, 26)
        if ten_i is None or kij_i is None or ten_i > kij_i:
            continue

        if close_i >= closes[i - 26]:
            continue

        rv = _rvol_prev50(vols, i)
        if rv is None or rv <= 1.5:
            continue

        signals.append({
            "scan":     "ICHIMOKU_STACK_BEAR",
            "bar_idx":  i,
            "time":     bars[i]["time"],
            "close":    close_i,
            "senkou_a": round(senkou_a, 6),
            "senkou_b": round(senkou_b, 6),
            "tenkan":   round(ten_i, 6),
            "kijun":    round(kij_i, 6),
            "rvol":     round(rv, 3),
        })

    return signals


def scan_ichimoku_classic_bull(bars: list) -> list[dict]:
    """
    Ichimoku Classic Bullish:
      1. TK crossover ↑: tenkan[i] > kijun[i] AND tenkan[i-1] <= kijun[i-1]
      2. Chikou above price 26 bars ago: close[i] > close[i-26]
      3. Cloud breakout: close[i] > senkou_a & senkou_b,
         AND close[i-1] <= cloud_top at bar i-1
    """
    signals = []
    highs  = [b["high"]   for b in bars]
    lows   = [b["low"]    for b in bars]
    closes = [b["close"]  for b in bars]

    for i in range(ICHIMOKU_MIN_INDEX + 1, len(bars)):
        close_i  = closes[i]
        close_i1 = closes[i - 1]

        # TK crossover
        ten_i  = _donchian_mid(highs, lows, i,     9)
        kij_i  = _donchian_mid(highs, lows, i,    26)
        ten_i1 = _donchian_mid(highs, lows, i - 1, 9)
        kij_i1 = _donchian_mid(highs, lows, i - 1, 26)
        if None in (ten_i, kij_i, ten_i1, kij_i1):
            continue
        if not (ten_i > kij_i and ten_i1 <= kij_i1):
            continue

        # Chikou
        if close_i <= closes[i - 26]:
            continue

        # Cloud at bar i
        j = i - 26
        ta_j = _donchian_mid(highs, lows, j, 9)
        kj_j = _donchian_mid(highs, lows, j, 26)
        sb_j = _donchian_mid(highs, lows, j, 52)
        if None in (ta_j, kj_j, sb_j):
            continue
        senkou_a = (ta_j + kj_j) / 2.0
        senkou_b = sb_j
        if not (close_i > senkou_a and close_i > senkou_b):
            continue

        # Cloud at bar i-1
        j1 = i - 27
        ta_j1 = _donchian_mid(highs, lows, j1, 9)
        kj_j1 = _donchian_mid(highs, lows, j1, 26)
        sb_j1 = _donchian_mid(highs, lows, j1, 52)
        if None in (ta_j1, kj_j1, sb_j1):
            continue
        sa_prev = (ta_j1 + kj_j1) / 2.0
        sb_prev = sb_j1
        cloud_top_prev = max(sa_prev, sb_prev)

        if close_i1 > cloud_top_prev:
            continue   # was already above cloud — not a breakout

        signals.append({
            "scan":     "ICHIMOKU_CLASSIC_BULL",
            "bar_idx":  i,
            "time":     bars[i]["time"],
            "close":    close_i,
            "senkou_a": round(senkou_a, 6),
            "senkou_b": round(senkou_b, 6),
            "tenkan":   round(ten_i, 6),
            "kijun":    round(kij_i, 6),
            "rvol":     None,
        })

    return signals


def scan_ichimoku_classic_bear(bars: list) -> list[dict]:
    """Ichimoku Classic Bearish – exact mirror."""
    signals = []
    highs  = [b["high"]   for b in bars]
    lows   = [b["low"]    for b in bars]
    closes = [b["close"]  for b in bars]

    for i in range(ICHIMOKU_MIN_INDEX + 1, len(bars)):
        close_i  = closes[i]
        close_i1 = closes[i - 1]

        ten_i  = _donchian_mid(highs, lows, i,     9)
        kij_i  = _donchian_mid(highs, lows, i,    26)
        ten_i1 = _donchian_mid(highs, lows, i - 1, 9)
        kij_i1 = _donchian_mid(highs, lows, i - 1, 26)
        if None in (ten_i, kij_i, ten_i1, kij_i1):
            continue
        if not (ten_i < kij_i and ten_i1 >= kij_i1):
            continue

        if close_i >= closes[i - 26]:
            continue

        j = i - 26
        ta_j = _donchian_mid(highs, lows, j, 9)
        kj_j = _donchian_mid(highs, lows, j, 26)
        sb_j = _donchian_mid(highs, lows, j, 52)
        if None in (ta_j, kj_j, sb_j):
            continue
        senkou_a = (ta_j + kj_j) / 2.0
        senkou_b = sb_j
        if not (close_i < senkou_a and close_i < senkou_b):
            continue

        j1 = i - 27
        ta_j1 = _donchian_mid(highs, lows, j1, 9)
        kj_j1 = _donchian_mid(highs, lows, j1, 26)
        sb_j1 = _donchian_mid(highs, lows, j1, 52)
        if None in (ta_j1, kj_j1, sb_j1):
            continue
        sa_prev = (ta_j1 + kj_j1) / 2.0
        sb_prev = sb_j1
        cloud_bot_prev = min(sa_prev, sb_prev)

        if close_i1 < cloud_bot_prev:
            continue

        signals.append({
            "scan":     "ICHIMOKU_CLASSIC_BEAR",
            "bar_idx":  i,
            "time":     bars[i]["time"],
            "close":    close_i,
            "senkou_a": round(senkou_a, 6),
            "senkou_b": round(senkou_b, 6),
            "tenkan":   round(ten_i, 6),
            "kijun":    round(kij_i, 6),
            "rvol":     None,
        })

    return signals


# ---------------------------------------------------------------------------
# Forward-return measurement
# ---------------------------------------------------------------------------

BULL_SCANS = {"ICHIMOKU_STACK_BULL", "ICHIMOKU_CLASSIC_BULL"}
BEAR_SCANS = {"ICHIMOKU_STACK_BEAR", "ICHIMOKU_CLASSIC_BEAR"}


def measure_forward_returns(bars: list, signal: dict) -> dict:
    """
    For each horizon H, measure the return H bars after the signal bar.

    Bullish scans: positive return = win
    Bearish scans: negative return = win  (stored as-is so you can flip sign)
    """
    i = signal["bar_idx"]
    entry_price = signal["close"]
    closes = [b["close"] for b in bars]
    returns = {}
    for h, label in zip(HORIZONS, HORIZON_LABELS):
        target_idx = i + h
        if target_idx < len(closes):
            ret = (closes[target_idx] - entry_price) / entry_price * 100.0
            returns[f"ret_{label}"] = round(ret, 4)
        else:
            returns[f"ret_{label}"] = None
    return returns


# ---------------------------------------------------------------------------
# Per-symbol backtest
# ---------------------------------------------------------------------------

def backtest_symbol(symbol: str, bars: list) -> list[dict]:
    """Run all 4 scans on bars, measure forward returns, return all signal rows."""
    all_signals = []
    for scan_fn in [
        scan_ichimoku_stack_bull,
        scan_ichimoku_stack_bear,
        scan_ichimoku_classic_bull,
        scan_ichimoku_classic_bear,
    ]:
        for sig in scan_fn(bars):
            fwd = measure_forward_returns(bars, sig)
            row = {
                "symbol":   symbol,
                "scan":     sig["scan"],
                "datetime": datetime.datetime.utcfromtimestamp(sig["time"]).strftime("%Y-%m-%d %H:%M"),
                "close":    sig["close"],
                "senkou_a": sig["senkou_a"],
                "senkou_b": sig["senkou_b"],
                "tenkan":   sig["tenkan"],
                "kijun":    sig["kijun"],
                "rvol":     sig["rvol"],
                **fwd,
            }
            all_signals.append(row)
    return all_signals


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def build_summary(all_rows: list) -> str:
    """Print a per-scan win-rate and avg-return table."""
    from collections import defaultdict

    scans = ["ICHIMOKU_STACK_BULL", "ICHIMOKU_STACK_BEAR",
             "ICHIMOKU_CLASSIC_BULL", "ICHIMOKU_CLASSIC_BEAR"]

    lines = []
    lines.append("=" * 90)
    lines.append("ICHIMOKU BACKTEST SUMMARY  –  6 months of 5m data")
    lines.append("=" * 90)

    for scan in scans:
        rows = [r for r in all_rows if r["scan"] == scan]
        if not rows:
            lines.append(f"\n{scan}: no signals found\n")
            continue

        is_bear = scan in BEAR_SCANS
        lines.append(f"\n{'─'*70}")
        lines.append(f"  {scan}   (n={len(rows)}  symbols={len({r['symbol'] for r in rows})})")
        lines.append(f"{'─'*70}")
        header = f"  {'Horizon':>8}  {'Signals':>8}  {'Win %':>8}  {'Avg Ret':>9}  {'Med Ret':>9}  {'P&L>2%':>8}  {'P&L<-2%':>8}"
        lines.append(header)

        for label in HORIZON_LABELS:
            key = f"ret_{label}"
            vals = [r[key] for r in rows if r.get(key) is not None]
            if not vals:
                lines.append(f"  {label:>8}  {'N/A':>8}")
                continue

            sorted_vals = sorted(vals)
            n = len(vals)
            avg = sum(vals) / n
            median = sorted_vals[n // 2]

            if is_bear:
                # For bear signals, negative return is a win
                wins = sum(1 for v in vals if v < 0)
                big_win = sum(1 for v in vals if v < -2)
                big_loss = sum(1 for v in vals if v > 2)
            else:
                wins = sum(1 for v in vals if v > 0)
                big_win = sum(1 for v in vals if v > 2)
                big_loss = sum(1 for v in vals if v < -2)

            win_pct = wins / n * 100
            lines.append(
                f"  {label:>8}  {n:>8}  {win_pct:>7.1f}%  {avg:>+9.3f}%  {median:>+9.3f}%"
                f"  {big_win:>8}  {big_loss:>8}"
            )

    lines.append("\n" + "=" * 90)
    lines.append("Win = return > 0 for bull scans, return < 0 for bear scans")
    lines.append("P&L>2% = signals that moved >+2%  |  P&L<-2% = signals that moved <-2%")
    lines.append("=" * 90 + "\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ichimoku 5m Backtest – Delta Exchange")
    parser.add_argument("--symbols", nargs="+", default=[], help="Specific symbols to test (e.g. BTCUSDT ETHUSDT)")
    parser.add_argument("--months", type=int, default=6, help="Look-back months (default: 6)")
    parser.add_argument("--all", action="store_true", help="Test ALL live perpetuals (slow!)")
    parser.add_argument("--workers", type=int, default=8, help="Parallel symbol workers (default: 8)")
    args = parser.parse_args()

    # --- Time range ---
    end_ts   = int(time.time())
    start_ts = end_ts - args.months * 30 * 86400
    log.info("Backtest window: %s → %s  (%d months)",
             datetime.datetime.utcfromtimestamp(start_ts).strftime("%Y-%m-%d"),
             datetime.datetime.utcfromtimestamp(end_ts).strftime("%Y-%m-%d"),
             args.months)

    # --- Symbol list ---
    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
        log.info("Testing %d user-specified symbols: %s", len(symbols), symbols)
    elif args.all:
        log.info("Fetching all live perpetuals from Delta…")
        futs = fetch_perpetual_futures()
        symbols = [f["symbol"] for f in futs if f.get("symbol")]
        log.info("Found %d live perpetuals", len(symbols))
    else:
        # Default: a curated set of high-liquidity perpetuals
        symbols = [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
            "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
            "MATICUSDT", "LTCUSDT", "ATOMUSDT", "NEARUSDT", "INJUSDT",
            "SUIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "PEPEUSDT",
            "WIFUSDT", "BONKUSDT", "SHIBUSDT", "TRXUSDT", "TONUSDT",
            "TIAUSDT", "JUPUSDT", "STXUSDT", "RUNEUSDT", "FTMUSDT",
        ]
        log.info("Using default set of %d high-liquidity symbols", len(symbols))

    # --- Output setup ---
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUTPUT_DIR, f"signals_{ts_str}.csv")
    summary_path = os.path.join(OUTPUT_DIR, f"summary_{ts_str}.txt")

    csv_fields = [
        "symbol", "scan", "datetime", "close",
        "senkou_a", "senkou_b", "tenkan", "kijun", "rvol",
    ] + [f"ret_{lbl}" for lbl in HORIZON_LABELS]

    all_rows = []
    total = len(symbols)

    def process(sym):
        try:
            log.info("  Fetching %s …", sym)
            bars = fetch_5m_candles_chunked(sym, start_ts, end_ts)
            if len(bars) < ICHIMOKU_MIN_INDEX + 2:
                log.warning("  %s – only %d bars, skipping", sym, len(bars))
                return []
            log.info("  %s – %d bars fetched, running scans…", sym, len(bars))
            rows = backtest_symbol(sym, bars)
            log.info("  %s – %d signals found", sym, len(rows))
            return rows
        except Exception as e:
            log.error("  %s – error: %s", sym, e)
            return []

    log.info("Starting backtest for %d symbols with %d workers…", total, args.workers)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process, sym): sym for sym in symbols}
        done = 0
        for fut in as_completed(futures):
            done += 1
            rows = fut.result()
            all_rows.extend(rows)
            log.info("[%d/%d] complete", done, total)

    if not all_rows:
        log.warning("No signals found across all symbols.")
        return

    # --- Write CSV ---
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)
    log.info("Signals written → %s  (%d rows)", csv_path, len(all_rows))

    # --- Build and write summary ---
    summary = build_summary(all_rows)
    print(summary)
    with open(summary_path, "w") as f:
        f.write(summary)
    log.info("Summary written → %s", summary_path)


if __name__ == "__main__":
    main()
