"""
Kindle Stock Ticker — Flask server
Serves a single Kindle-compatible HTML page showing stock quotes from Finnhub.

Config is injected via environment variables defined in docker-compose.yml.
For local development without Docker, export the variables in your shell.
Quotes are cached in memory.  The Finnhub API is called at market open (+30 s),
every REFRESH_INTERVAL seconds during trading hours, and once at market close.
Run with a single Gunicorn worker to preserve the in-memory cache.
"""

import os
import time
import requests
from datetime import datetime, timedelta

import pytz
from flask import Flask, render_template

app = Flask(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
FINNHUB_API_KEY  = os.getenv("FINNHUB_API_KEY", "")
MAIN_TICKER      = os.getenv("MAIN_TICKER", "SPY").strip().upper()
TICKERS_RAW      = os.getenv("TICKERS", "SPY,AAPL,MSFT,GOOGL,AMZN,META,TSLA,NVDA")
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", "300"))   # seconds
MARKET_OPEN_STR  = os.getenv("MARKET_OPEN",  "09:30")          # 24h, Eastern Time
MARKET_CLOSE_STR = os.getenv("MARKET_CLOSE", "16:00")          # 24h, Eastern Time

ET = pytz.timezone("America/New_York")

# Derive ticker lists
_raw_tickers  = [t.strip().upper() for t in TICKERS_RAW.split(",") if t.strip()]
if MAIN_TICKER not in _raw_tickers:
    _raw_tickers.insert(0, MAIN_TICKER)
OTHER_TICKERS = [t for t in _raw_tickers if t != MAIN_TICKER][:7]

# ── In-memory cache ────────────────────────────────────────────────────────────
# NOTE: requires a single Gunicorn worker (--workers 1)
_cache:    dict  = {}    # { "AAPL": {"price": 1.23, "change": 0.45, "pct_change": 0.38} }
_cache_ts: float = 0.0   # Unix timestamp of last successful API fetch

# Track the date of the forced at-open and at-close fetches so each fires
# exactly once per trading day regardless of cache age.
_open_fetch_date  = None  # date of last at-open  forced fetch
_close_fetch_date = None  # date of last at-close forced fetch


# ── Market-hours helper ────────────────────────────────────────────────────────
def is_market_open() -> bool:
    """
    Returns True if the current Eastern Time falls within the configured
    trading window on a weekday.  Does NOT check for market holidays.
    """
    now = datetime.now(ET)
    if now.weekday() >= 5:   # 5 = Saturday, 6 = Sunday
        return False
    oh, om = map(int, MARKET_OPEN_STR.split(":"))
    ch, cm = map(int, MARKET_CLOSE_STR.split(":"))
    t_open  = now.replace(hour=oh, minute=om, second=0, microsecond=0)
    t_close = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return t_open <= now <= t_close


# ── Finnhub API ────────────────────────────────────────────────────────────────
def _fetch_quote(symbol: str) -> dict:
    resp = requests.get(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": symbol, "token": FINNHUB_API_KEY},
        timeout=10,
    )
    resp.raise_for_status()
    d = resp.json()
    return {
        "price":      float(d.get("c") or 0),
        "change":     float(d.get("d") or 0),
        "pct_change": float(d.get("dp") or 0),
    }


def _refresh_cache() -> None:
    """Fetch fresh quotes from Finnhub for all configured tickers."""
    global _cache, _cache_ts
    new_data: dict = {}
    for sym in [MAIN_TICKER] + OTHER_TICKERS:
        try:
            new_data[sym] = _fetch_quote(sym)
        except Exception:
            # Keep the last known value on error; fall back to zeros if first run
            new_data[sym] = _cache.get(sym, {"price": 0.0, "change": 0.0, "pct_change": 0.0})
    _cache    = new_data
    _cache_ts = time.time()


def get_quotes() -> dict:
    """
    API call strategy (in priority order):

      1. First start (empty cache) — fetch once regardless of time; seed
         tracking dates so a restart mid-session doesn't double-fetch.
      2. At-open fetch — fires once per weekday on the first request at or
         after MARKET_OPEN + 30 s (the 30 s grace lets opening prices settle).
      3. Normal intraday refresh — while market is open, refresh whenever the
         cache is older than REFRESH_INTERVAL seconds.
      4. At-close fetch — fires once per weekday on the first request at or
         after MARKET_CLOSE, capturing the official closing price.
      5. All other times — serve the stale cache with no API call.
    """
    global _open_fetch_date, _close_fetch_date

    now_et = datetime.now(ET)
    today  = now_et.date()

    oh, om = map(int, MARKET_OPEN_STR.split(":"))
    ch, cm = map(int, MARKET_CLOSE_STR.split(":"))
    t_open        = now_et.replace(hour=oh, minute=om, second=0, microsecond=0)
    t_open_plus30 = t_open + timedelta(seconds=30)
    t_close       = now_et.replace(hour=ch, minute=cm, second=0, microsecond=0)

    # ── 1. First start ────────────────────────────────────────────────────
    if not _cache:
        _refresh_cache()
        # Seed tracking dates so a mid-session restart doesn't re-fire these
        if now_et.weekday() < 5 and now_et >= t_open_plus30:
            _open_fetch_date = today
        if now_et.weekday() < 5 and now_et >= t_close:
            _close_fetch_date = today
        return _cache

    # ── 2. At-open fetch (09:30:30, once per weekday) ─────────────────────
    if (now_et.weekday() < 5
            and now_et >= t_open_plus30
            and _open_fetch_date != today):
        _refresh_cache()
        _open_fetch_date = today
        return _cache

    # ── 3. Normal intraday refresh ────────────────────────────────────────
    if is_market_open() and (time.time() - _cache_ts) >= REFRESH_INTERVAL:
        _refresh_cache()
        if now_et >= t_close:       # fired right at close boundary — count it
            _close_fetch_date = today
        return _cache

    # ── 4. At-close fetch (16:00, once per weekday) ───────────────────────
    if (now_et.weekday() < 5
            and now_et >= t_close
            and _close_fetch_date != today):
        _refresh_cache()
        _close_fetch_date = today
        return _cache

    # ── 5. Outside all active windows — serve cache as-is ─────────────────
    return _cache


# ── Jinja2 filter ──────────────────────────────────────────────────────────────
@app.template_filter("f2")
def fmt2(value) -> str:
    """Format a numeric value to 2 decimal places, or '--' on error."""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "--"


# ── Route ──────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    quotes      = get_quotes()
    market_open = is_market_open()

    last_update = (
        datetime.fromtimestamp(_cache_ts, ET).strftime("%b %d, %Y  %I:%M:%S %p ET")
        if _cache_ts else "No data yet"
    )

    # During market hours use the configured interval; outside hours slow down
    # to 12× that interval (e.g. 300 s → 3600 s = once per hour) to avoid
    # pointless reloads overnight and on weekends.
    display_refresh = REFRESH_INTERVAL if market_open else REFRESH_INTERVAL * 12

    return render_template(
        "index.html",
        main_ticker      = MAIN_TICKER,
        other_tickers    = OTHER_TICKERS,
        quotes           = quotes,
        refresh_interval = display_refresh,
        last_update      = last_update,
        market_open      = market_open,
    )


# ── Entry point (local dev only; Docker uses Gunicorn via CMD in Dockerfile) ──
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)
