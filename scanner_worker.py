# scanner_worker.py
import os
import time
import math
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import traceback

import pandas as pd
import requests
from polygon import RESTClient   # matches your code
from supabase import create_client, Client

ET = ZoneInfo("America/New_York")

# env
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
RUN_INTERVAL_SEC = int(os.environ.get("RUN_INTERVAL_SEC", "30"))
DELETE_AT_4AM_ET = int(os.environ.get("DELETE_AT_4AM_ET", "1"))  # 1 = enable deletion

if not (POLYGON_API_KEY and SUPABASE_URL and SUPABASE_KEY):
    raise RuntimeError("POLYGON_API_KEY, SUPABASE_URL, SUPABASE_KEY required env vars")

polygon = RESTClient(POLYGON_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# thresholds (tweak as needed)
MIN_CUM_VOL = 2_000_000
THRESH_VOL = 100_000
GAIN_THRESHOLD = 0.05  # default 5% (you can vary)
VOL_930_940_THRESHOLD = 10_000_000

# utility DB wrappers (using supabase client)
def upsert_p1_ticker(trade_date: str, ticker: str, data: dict):
    # upsert using PostgREST RPC or direct SQL through Supabase client.
    supabase.table("p1_tickers").upsert(
        {**{"trade_date": trade_date, "ticker": ticker}, **data}
    ).execute()

def insert_p1_series(trade_date: str, ticker: str, ts_et_iso: str, data: dict):
    supabase.table("p1_series").upsert(
        {**{"trade_date": trade_date, "ticker": ticker, "ts_et": ts_et_iso}, **data}
    ).execute()

def insert_p1_event(trade_date: str, ts_et_iso: str, ticker: str, event_type: str, details: str):
    supabase.table("p1_events").insert(
        {"trade_date": trade_date, "ts_et": ts_et_iso, "ticker": ticker, "event_type": event_type, "details": details}
    ).execute()

def insert_p3_alert(trade_date: str, ts_et_iso: str, ticker: str, reason: str, vol_delta: int, window_min: int, price: float):
    supabase.table("p3_alerts").upsert(
        {"trade_date": trade_date, "ts_et": ts_et_iso, "ticker": ticker, "reason": reason, "vol_delta": vol_delta, "window_min": window_min, "price": price}
    ).execute()

def upsert_p2_state(trade_date: str, ticker: str, data: dict):
    supabase.table("p2_ticker_state").upsert(
        {**{"trade_date": trade_date, "ticker": ticker}, **data}
    ).execute()

def insert_p2_buy_for_user(trade_date: str, email: str, ts_et_iso: str, ticker: str, price: float, rsi: float, boll_lower: float, pattern: str, details: str):
    supabase.table("p2_buy_signals").upsert(
        {"trade_date": trade_date, "email": email, "ts_et": ts_et_iso, "ticker": ticker, "price": price, "rsi": rsi, "boll_lower": boll_lower, "pattern": pattern, "details": details}
    ).execute()

def update_heartbeat():
    supabase.table("scanner_heartbeat").upsert({"id":1,"last_seen": datetime.now(timezone.utc).isoformat()}).execute()

def delete_everything_for_date(date_str: str):
    # wipe p1_tickers, p1_series, p1_events, p2_ticker_state, p2_buy_signals, p3_alerts, user_watchlist for that date
    tables = ["p1_tickers", "p1_series", "p1_events", "p2_ticker_state", "p2_buy_signals", "p3_alerts", "user_watchlist"]
    for t in tables:
        supabase.table(t).delete().eq("trade_date", date_str).execute()
    # update heartbeat
    update_heartbeat()

# helpers for polygon fetching
def fetch_snapshot_for_ticker(ticker: str):
    try:
        snap = polygon.get_snapshot_ticker("stocks", ticker)
    except Exception:
        return None
    m = getattr(snap, "min", None)
    if m is None:
        return None
    price = getattr(m, "close", None) or getattr(m, "c", None)
    av = getattr(m, "accumulated_volume", None) or getattr(m, "av", None)
    ts = getattr(m, "timestamp", None) or getattr(m, "t", None)
    try:
        return float(price) if price is not None else None, float(av) if av is not None else None, int(ts) if ts is not None else None
    except Exception:
        return None

def get_1m_aggs(ticker: str, from_dt: datetime, to_dt: datetime):
    """
    Get 1m aggs between two aware datetimes.
    """
    try:
        aggs = polygon.get_aggs(ticker=ticker, multiplier=1, timespan="minute", from_=from_dt, to=to_dt, limit=10000)
    except Exception:
        return []
    return aggs

def acc_vol_from_aggs(aggs):
    total = 0.0
    rows = []
    for a in aggs:
        v = getattr(a, "volume", None) or getattr(a, "v", None) or 0
        t = getattr(a, "timestamp", None) or getattr(a, "t", None)
        if t is None:
            continue
        total += float(v)
        rows.append((int(t), float(v)))
    return rows

# indicators (simple)
def compute_rsi14_from_closes(closes: pd.Series):
    period = 14
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def boll_lower(close: pd.Series, period: int = 20, num_std: float = 2.0):
    ma = close.rolling(period).mean()
    sd = close.rolling(period).std()
    return ma - num_std * sd

# helper: return ET-aware now
def now_et():
    return datetime.now(tz=ET)

# main scanning iteration
def run_once():
    now = now_et()
    trade_date = now.date().isoformat()

    # perform deletion at 04:00 ET if requested
    if DELETE_AT_4AM_ET and now.hour == 4 and now.minute == 0:
        print("Deleting today's tables for fresh start at 04:00 ET")
        delete_everything_for_date(trade_date)
        return

    update_heartbeat()

    # union tickers to process:
    # - all tickers in p1_tickers (yesterday/today) and
    # - all tickers in user_watchlist for today
    union_tickers = set()

    # fetch today's user_watchlist (all users)
    res = supabase.table("user_watchlist").select("ticker").eq("trade_date", trade_date).execute()
    if res and res.data:
        for r in res.data:
            union_tickers.add(r["ticker"].upper())

    # also include existing p1_tickers for continuity
    res2 = supabase.table("p1_tickers").select("ticker").eq("trade_date", trade_date).execute()
    if res2 and res2.data:
        for r in res2.data:
            union_tickers.add(r["ticker"].upper())

    # if union empty, early exit
    if not union_tickers:
        print("No tickers to process this run.")
        return

    # Loop tickers once, call polygon per ticker
    for ticker in sorted(union_tickers):
        try:
            # 1) fetch current snapshot
            snap = fetch_snapshot_for_ticker(ticker)
            if not snap:
                continue
            price, acc_av, ts_ms = snap
            ts_et = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(ET) if ts_ms else now
            # compute 2m & 10m deltas via aggs (1-minute aggs)
            to_dt = ts_et
            from_15m_dt = ts_et - timedelta(minutes=15)
            aggs = get_1m_aggs(ticker, from_15m_dt, to_dt)
            rows = acc_vol_from_aggs(aggs)
            # rows are (t_ms, v) per minute. compute last-minute cumulative window sums
            if not rows:
                continue
            # Convert to DataFrame
            df = pd.DataFrame(rows, columns=["t_ms", "v"])
            df["dt_et"] = pd.to_datetime(df["t_ms"], unit="ms", utc=True).dt.tz_convert(ET)
            df = df.sort_values("dt_et")
            # compute rolling sums for 2 & 10 min
            df["v"] = df["v"].astype(float)
            # We compute delta of accumulated volume by summing last N mins
            last_time = df["dt_et"].iloc[-1]
            # compute 2-min and 10-min sum ending at last_time inclusive
            mask2 = df["dt_et"] > (last_time - timedelta(minutes=2))
            mask10 = df["dt_et"] > (last_time - timedelta(minutes=10))
            vol2 = float(df.loc[mask2, "v"].sum())
            vol10 = float(df.loc[mask10, "v"].sum())

            # Determine hits
            hit2 = vol2 >= 5_000_000  # use default threshold or manage dynamically
            hit10 = vol10 >= 10_000_000

            # Cum vol (today)
            cum_vol = None
            # try to pull cumulative from snapshot 'day' if available
            try:
                s2 = polygon.get_snapshot_ticker("stocks", ticker)
                day = getattr(s2, "day", None)
                cum_vol = getattr(day, "v", None) or getattr(day, "volume", None)
                cum_vol = int(cum_vol) if cum_vol else int(vol10 + vol2)
            except Exception:
                cum_vol = int(vol10 + vol2)

            # Post-open check 9:30-9:40 rule
            # We'll compute the difference of accumulated values between 9:30 and 9:40 if we have full day aggs
            vol_930_940_sum = 0
            if now.hour >= 9:
                df_1m_day = get_1m_aggs(ticker, ts_et.replace(hour=9, minute=30, second=0, microsecond=0), ts_et.replace(hour=9, minute=40, second=0, microsecond=0))
                vol_930_940_sum = sum([getattr(a, "volume", 0) or getattr(a, "v", 0) or 0 for a in df_1m_day]) if df_1m_day else 0

            # Persist p1_ticker row
            upsert_p1_ticker(trade_date, ticker, {
                "baseline_8am": None,
                "start_price": price,
                "current_price": price,
                "cum_vol": int(cum_vol or 0),
                "max_gain_window": 0.0,
                "hit_gain": False,
                "qualified_locked": False,
                "bursts_5m": 0,
                "bursts_10m": 0,
                "bursts_total": 0,
                "last_5m_counted_t": None,
                "last_5m_vol": vol2,
                "last_10m_vol": vol10,
                "updated_at": datetime.now(timezone.utc).isoformat()
            })

            # If hit -> insert p3 alert (global)
            if hit2 or hit10:
                window_min = 2 if hit2 else 10
                reason = f"ΔVol {window_min}m hit"
                insert_p3_alert(trade_date, ts_et.isoformat(), ticker, reason, int(vol2 if hit2 else vol10), window_min, float(price or 0.0))

            # For page2: compute indicators for this ticker (for closed candle logic)
            # fetch 1m ohlc lookback (e.g., last 240 minutes)
            df_ohlc = []
            try:
                aggs_full = polygon.get_aggs(ticker=ticker, multiplier=1, timespan="minute", from_=now - timedelta(minutes=300), to=now, limit=5000)
                for a in aggs_full:
                    t = getattr(a, "timestamp", None) or getattr(a, "t", None)
                    o = getattr(a, "open", None) or getattr(a, "o", None)
                    h = getattr(a, "high", None) or getattr(a, "h", None)
                    l = getattr(a, "low", None) or getattr(a, "l", None)
                    c = getattr(a, "close", None) or getattr(a, "c", None)
                    v = getattr(a, "volume", None) or getattr(a, "v", None)
                    if None in (t, o, h, l, c):
                        continue
                    df_ohlc.append([int(t), float(o), float(h), float(l), float(c), float(v or 0)])
            except Exception:
                df_ohlc = []

            if df_ohlc:
                df_ohlc = pd.DataFrame(df_ohlc, columns=["t_ms", "open", "high", "low", "close", "volume"])
                df_ohlc["dt_et"] = pd.to_datetime(df_ohlc["t_ms"], unit="ms", utc=True).dt.tz_convert(ET)
                df_ohlc = df_ohlc.sort_values("dt_et").set_index("dt_et")

                closes = df_ohlc["close"]
                rsi14 = compute_rsi14_from_closes(closes).iloc[-1] if len(closes) >= 15 else None
                boll_l = boll_lower(closes).iloc[-1] if len(closes) >= 20 else None

                # simple pattern detection (closed candle = -2)
                if len(df_ohlc) >= 2:
                    closed = df_ohlc.iloc[-2]
                    o, h, l, c = closed["open"], closed["high"], closed["low"], closed["close"]
                    rng = h - l
                    body = abs(c - o) if abs(c - o) > 0 else rng * 0.0001
                    lower_wick = min(o, c) - l
                    upper_wick = h - max(o, c)
                    hammer = (lower_wick >= 2.0 * body) and (upper_wick <= 0.5 * body)
                    strong_green = (c > o) and ((c - o) >= 0.7 * rng)
                    pattern = "HAMMER" if hammer else ("STRONG_GREEN" if strong_green else "")
                    boll_touch = (closed["low"] <= (boll_l if boll_l is not None else -999999))
                else:
                    rsi14 = None
                    boll_l = None
                    pattern = ""
                    boll_touch = False

                upsert_p2_state(trade_date, ticker, {
                    "closed_minute_et": df_ohlc.index[-2].to_pydatetime().isoformat() if len(df_ohlc) >= 2 else None,
                    "rsi14": float(rsi14) if rsi14 is not None else None,
                    "boll_lower": float(boll_l) if boll_l is not None else None,
                    "pattern": pattern,
                    "boll_touch": bool(boll_touch),
                    "price": float(price) if price is not None else None,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })

                # Page 2 per-user buy signal generation:
                # For each user with this ticker in today's watchlist, check their rules and insert buy if matched.
                # For simplicity: load all users who have this ticker today
                watch_res = supabase.table("user_watchlist").select("*").eq("trade_date", trade_date).eq("ticker", ticker).execute()
                if watch_res and watch_res.data:
                    for row in watch_res.data:
                        email = row["email"]
                        # define conditions: RSI <= 30 and boll_touch and pattern present
                        cond_rsi = (rsi14 is not None) and (rsi14 <= 30)
                        cond_boll = bool(boll_touch)
                        cond_pattern = bool(pattern)
                        if cond_rsi and cond_boll and cond_pattern:
                            closed_time_iso = df_ohlc.index[-2].to_pydatetime().isoformat()
                            details = f"RSI={rsi14:.2f}; BOLL_touch={boll_touch}; pattern={pattern}"
                            insert_p2_buy_for_user(trade_date, email, closed_time_iso, ticker, float(price or 0.0), float(rsi14 or 0.0), float(boll_l or 0.0), pattern, details)

            # Insert a series sample for charts
            insert_p1_series(trade_date, ticker, ts_et.isoformat(), {
                "gain_pct": 0.0,
                "cum_vol": int(cum_vol or 0),
                "last_5m_vol": int(vol2),
                "last_10m_vol": int(vol10),
                "bursts_total": 1 if (hit2 or hit10) else 0
            })

        except Exception as e:
            print(f"Error processing {ticker}: {e}")
            traceback.print_exc()

    # update heartbeat at end
    update_heartbeat()

if __name__ == "__main__":
    print("Scanner worker starting. Interval:", RUN_INTERVAL_SEC, "s")
    while True:
        try:
            run_once()
        except Exception as e:
            print("Run error:", e)
            traceback.print_exc()
        time.sleep(RUN_INTERVAL_SEC)
