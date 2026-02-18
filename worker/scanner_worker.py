import os
import time
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import deque, defaultdict

import requests
import numpy as np
import pandas as pd
import psycopg
from polygon import RESTClient

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# ---- Scheduling ----
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "30"))
RESET_HOUR_ET = int(os.getenv("RESET_HOUR_ET", "4"))  # 4am ET daily cleanup

# ---- Page 1 window ----
WINDOW_START = (8, 15)
WINDOW_END = (9, 45)

MIN_CUM_VOL = 2_000_000
THRESH_VOL = 100_000
GAIN_THRESHOLD = float(os.getenv("GAIN_THRESHOLD", "0.10"))  # 10% (match your worker constant)

# ---- RTH alerts window ----
RTH_START = (9, 30)
RTH_END = (16, 0)

VOL_10M_THRESHOLD = int(os.getenv("VOL_10M_THRESHOLD", "10000000"))
VOL_2M_THRESHOLD = int(os.getenv("VOL_2M_THRESHOLD", "5000000"))

# ---- Buy watcher defaults ----
RSI_THRESHOLD = float(os.getenv("RSI_THRESHOLD", "30"))
BOLL_PERIOD = int(os.getenv("BOLL_PERIOD", "20"))
BOLL_STD = float(os.getenv("BOLL_STD", "2.0"))
LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "240"))

# ---- Market cap filter support ----
FUNDAMENTALS_TTL_HOURS = int(os.getenv("FUNDAMENTALS_TTL_HOURS", "12"))

DATABASE_URL = os.environ["DATABASE_URL"]
POLYGON_API_KEY = os.environ["POLYGON_API_KEY"]

client = RESTClient(POLYGON_API_KEY)

# In-memory accumulated volume history for RTH alerts
# per ticker: deque[(ts_et(datetime), accumulated_volume(float))]
VOL_HIST = defaultdict(lambda: deque(maxlen=300))

# -------------------------
# DB helpers
# -------------------------
def pg():
    # Ensure SSL if your pooler requires it. Best is in DATABASE_URL: ?sslmode=require
    return psycopg.connect(DATABASE_URL, autocommit=True)

def is_between(now_dt: datetime, start_hm, end_hm) -> bool:
    start = now_dt.replace(hour=start_hm[0], minute=start_hm[1], second=0, microsecond=0)
    end = now_dt.replace(hour=end_hm[0], minute=end_hm[1], second=0, microsecond=0)
    return start <= now_dt < end

def is_rth(now_dt: datetime) -> bool:
    return is_between(now_dt, RTH_START, RTH_END)

def meta_get(con, k: str):
    cur = con.execute("select v from meta where k=%s", (k,))
    row = cur.fetchone()
    return row[0] if row else None

def meta_set(con, k: str, v: str):
    con.execute("""
      insert into meta(k,v) values (%s,%s)
      on conflict(k) do update set v=excluded.v
    """, (k, v))

def daily_reset_if_needed(now_et: datetime):
    today = now_et.date().isoformat()
    with pg() as con:
        last = meta_get(con, "last_reset_date")
        if now_et.hour == RESET_HOUR_ET and (last != today):
            # delete older than today (new day)
            con.execute("delete from scanner_tickers where trade_date < %s", (today,))
            con.execute("delete from scanner_series where trade_date < %s", (today,))
            con.execute("delete from scanner_events where trade_date < %s", (today,))
            con.execute("delete from buy_signals where trade_date < %s", (today,))
            con.execute("delete from user_watchlist where trade_date < %s", (today,))
            con.execute("delete from global_watchlist where trade_date < %s", (today,))
            con.execute("delete from rth_alerts where trade_date < %s", (today,))
            meta_set(con, "last_reset_date", today)

            # clear in-memory hist to avoid cross-day artifacts
            VOL_HIST.clear()
            print(f"[RESET] Completed daily reset for {today} @ {now_et}")

# -------------------------
# Polygon parsing
# -------------------------
def get_min_fields(s):
    m = getattr(s, "min", None)
    if not m:
        return None
    price = getattr(m, "close", None) or getattr(m, "c", None)
    high = getattr(m, "high", None) or getattr(m, "h", None)
    av = getattr(m, "accumulated_volume", None) or getattr(m, "av", None)
    ts_ms = getattr(m, "timestamp", None) or getattr(m, "t", None)
    if price is None or av is None or ts_ms is None:
        return None
    try:
        price = float(price)
        high = float(high) if high is not None else price
        av = float(av)
        ts_ms = int(ts_ms)
        return price, high, av, ts_ms
    except Exception:
        return None

def get_day_volume(s):
    d = getattr(s, "day", None)
    if not d:
        return 0.0
    v = getattr(d, "volume", None) or getattr(d, "v", None) or 0.0
    try:
        return float(v)
    except Exception:
        return 0.0

def fetch_exact_8am_baseline(ticker: str, day_et: datetime) -> float | None:
    start = day_et.replace(hour=8, minute=0, second=0, microsecond=0)
    end = day_et.replace(hour=8, minute=1, second=0, microsecond=0)
    try:
        aggs = client.get_aggs(ticker=ticker, multiplier=1, timespan="minute", from_=start, to=end, limit=5)
    except Exception:
        return None
    if not aggs:
        return None
    bar = aggs[0]
    o = getattr(bar, "open", None) or getattr(bar, "o", None)
    c = getattr(bar, "close", None) or getattr(bar, "c", None)
    try:
        return float(o) if o is not None else (float(c) if c is not None else None)
    except Exception:
        return None

def get_last_two_5m_bars(ticker: str, now_et: datetime):
    from_dt = now_et - timedelta(minutes=60)
    to_dt = now_et
    aggs = client.get_aggs(ticker=ticker, multiplier=5, timespan="minute", from_=from_dt, to=to_dt, limit=6)
    if not aggs:
        return None, 0.0, None, 0.0
    last = aggs[-1]
    prev = aggs[-2] if len(aggs) >= 2 else None

    last_t = getattr(last, "timestamp", None) or getattr(last, "t", None)
    last_v = getattr(last, "volume", None) or getattr(last, "v", 0.0)

    prev_t, prev_v = None, 0.0
    if prev is not None:
        prev_t = getattr(prev, "timestamp", None) or getattr(prev, "t", None)
        prev_v = getattr(prev, "volume", None) or getattr(prev, "v", 0.0)
    return int(last_t), float(last_v), (int(prev_t) if prev_t is not None else None), float(prev_v)

# -------------------------
# DB writes
# -------------------------
def upsert_scanner_ticker(con, trade_date, ticker, d):
    con.execute("""
    insert into scanner_tickers(
      trade_date, ticker, baseline_8am, start_price, current_price, cum_vol,
      max_gain_window, hit_gain, qualified_locked,
      bursts_5m, bursts_10m, bursts_total,
      last_5m_counted_t, last_5m_vol, last_10m_vol, updated_at
    ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
    on conflict (trade_date, ticker) do update set
      baseline_8am=excluded.baseline_8am,
      start_price=excluded.start_price,
      current_price=excluded.current_price,
      cum_vol=excluded.cum_vol,
      max_gain_window=excluded.max_gain_window,
      hit_gain=excluded.hit_gain,
      qualified_locked=excluded.qualified_locked,
      bursts_5m=excluded.bursts_5m,
      bursts_10m=excluded.bursts_10m,
      bursts_total=excluded.bursts_total,
      last_5m_counted_t=excluded.last_5m_counted_t,
      last_5m_vol=excluded.last_5m_vol,
      last_10m_vol=excluded.last_10m_vol,
      updated_at=now()
    """, (
      trade_date, ticker,
      d.get("baseline_8am"),
      d.get("start_price"),
      d.get("current_price"),
      d.get("cum_vol"),
      d.get("max_gain_window"),
      bool(d.get("hit_gain", False)),
      bool(d.get("qualified_locked", False)),
      int(d.get("bursts_5m", 0)),
      int(d.get("bursts_10m", 0)),
      int(d.get("bursts_total", 0)),
      d.get("last_5m_counted_t"),
      float(d.get("last_5m_vol", 0.0)),
      float(d.get("last_10m_vol", 0.0)),
    ))

def insert_series_point(con, trade_date, ticker, ts_et, d):
    con.execute("""
      insert into scanner_series(
        trade_date, ticker, ts_et, gain_pct, cum_vol, last_5m_vol, last_10m_vol, bursts_total
      ) values (%s,%s,%s,%s,%s,%s,%s,%s)
      on conflict do nothing
    """, (
      trade_date, ticker, ts_et,
      d.get("gain_pct"),
      d.get("cum_vol"),
      d.get("last_5m_vol"),
      d.get("last_10m_vol"),
      d.get("bursts_total"),
    ))

def insert_event(con, trade_date, ts_et, ticker, event_type, details):
    con.execute("""
      insert into scanner_events(trade_date, ts_et, ticker, event_type, details)
      values (%s,%s,%s,%s,%s)
    """, (trade_date, ts_et, ticker, event_type, details))

def upsert_global_watchlist(con, trade_date, ticker, source, first_seen_ts):
    con.execute("""
      insert into global_watchlist(trade_date, ticker, source, first_seen_ts_et)
      values (%s,%s,%s,%s)
      on conflict do nothing
    """, (trade_date, ticker, source, first_seen_ts))

def upsert_rth_alert(con, trade_date, ticker, window_min, first_seen_ts, reason, delta_shares, price):
    # dedupe: only once per day per ticker per window
    con.execute("""
      insert into rth_alerts(trade_date, ticker, window_min, first_seen_ts_et, reason, delta_shares, price)
      values (%s,%s,%s,%s,%s,%s,%s)
      on conflict (trade_date, ticker, window_min) do nothing
    """, (trade_date, ticker, window_min, first_seen_ts, reason, int(delta_shares), float(price) if price is not None else None))

def insert_buy_signal(con, trade_date, ts_et, ticker, price, rsi, boll_l, pattern, details):
    con.execute("""
      insert into buy_signals(trade_date, ts_et, ticker, price, rsi, boll_lower, pattern, details)
      values (%s,%s,%s,%s,%s,%s,%s,%s)
      on conflict do nothing
    """, (trade_date, ts_et, ticker, price, rsi, boll_l, pattern, details))

# -------------------------
# RTH delta computation
# -------------------------
def compute_delta(ticker: str, ts_et: datetime, av_now: float, minutes: int):
    dq = VOL_HIST[ticker]

    # dedupe within a single ticker timestamp
    if dq and dq[-1][0] == ts_et:
        return None

    dq.append((ts_et, av_now))
    cutoff = ts_et - timedelta(minutes=minutes)

    av_then = None
    # dq is time-ordered by append
    for t, av in dq:
        if t <= cutoff:
            av_then = av
        else:
            break

    if av_then is None:
        return None

    delta = av_now - av_then
    if delta < 0:
        return None
    return float(delta)

# -------------------------
# Buy signal logic (new)
# -------------------------
def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def boll_lower(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.Series:
    ma = close.rolling(period).mean()
    sd = close.rolling(period).std()
    return ma - num_std * sd

def is_big_green_vs_prev(prev_o, prev_c, o, c):
    return (c > o) and (abs(c - o) > abs(prev_c - prev_o))

def is_hammer_any_color(o, h, l, c):
    body = abs(c - o)
    lower_wick = min(o, c) - l
    return lower_wick > body

def is_piercing(prev_o, prev_c, o, c):
    # previous red, current green
    if not (prev_c < prev_o):
        return False
    if not (c > o):
        return False
    red_mid = (prev_o + prev_c) / 2.0
    return (o < prev_c) and (c > red_mid)

def fetch_1m_ohlc(ticker: str, now_et: datetime, lookback_minutes: int = 240) -> pd.DataFrame:
    from_dt = now_et - timedelta(minutes=lookback_minutes)
    to_dt = now_et
    try:
        aggs = client.get_aggs(ticker=ticker, multiplier=1, timespan="minute", from_=from_dt, to=to_dt, limit=5000)
    except Exception:
        return pd.DataFrame()

    if not aggs:
        return pd.DataFrame()

    rows = []
    for a in aggs:
        t = getattr(a, "timestamp", None) or getattr(a, "t", None)
        o = getattr(a, "open", None) or getattr(a, "o", None)
        h = getattr(a, "high", None) or getattr(a, "h", None)
        l = getattr(a, "low", None) or getattr(a, "l", None)
        c = getattr(a, "close", None) or getattr(a, "c", None)
        if t is None or o is None or h is None or l is None or c is None:
            continue
        rows.append([int(t), float(o), float(h), float(l), float(c)])

    df = pd.DataFrame(rows, columns=["t_ms", "open", "high", "low", "close"])
    df["dt_et"] = pd.to_datetime(df["t_ms"], unit="ms", utc=True).dt.tz_convert(ET)
    return df.sort_values("dt_et").set_index("dt_et")

# -------------------------
# Fundamentals (market cap cache)
# -------------------------
def fundamentals_get_market_cap(con, ticker: str):
    cur = con.execute("select market_cap, updated_ts_et from ticker_fundamentals where ticker=%s", (ticker,))
    row = cur.fetchone()
    if not row:
        return None
    mcap, updated = row
    return (float(mcap) if mcap is not None else None), updated

def fundamentals_upsert(con, ticker: str, market_cap):
    con.execute("""
      insert into ticker_fundamentals(ticker, market_cap, updated_ts_et)
      values (%s,%s, now())
      on conflict(ticker) do update set
        market_cap=excluded.market_cap,
        updated_ts_et=now()
    """, (ticker, float(market_cap) if market_cap is not None else None))

def polygon_fetch_market_cap(ticker: str) -> float | None:
    # Direct REST to avoid SDK mismatch across versions
    url = f"https://api.polygon.io/v3/reference/tickers/{ticker}"
    try:
        r = requests.get(url, params={"apiKey": POLYGON_API_KEY}, timeout=8)
        if r.status_code != 200:
            return None
        js = r.json()
        res = js.get("results") or {}
        # Polygon returns different keys depending on plan/data
        for key in ("market_cap", "marketCap", "market_capitalization"):
            if key in res and res[key] is not None:
                return float(res[key])
    except Exception:
        return None
    return None

def ensure_market_cap_cached(con, ticker: str):
    got = fundamentals_get_market_cap(con, ticker)
    if got:
        mcap, updated = got
        # refresh if stale
        if updated and (datetime.now(ET) - updated.astimezone(ET)) < timedelta(hours=FUNDAMENTALS_TTL_HOURS):
            return mcap
    mcap = polygon_fetch_market_cap(ticker)
    fundamentals_upsert(con, ticker, mcap)
    return mcap

# -------------------------
# Watchlist union (global + per-user)
# -------------------------
def get_all_watchlist_tickers(con, trade_date):
    cur = con.execute("""
      select distinct ticker from (
        select ticker from global_watchlist where trade_date=%s
        union
        select ticker from user_watchlist where trade_date=%s
      ) x
    """, (trade_date, trade_date))
    return [r[0] for r in cur.fetchall()]

# -------------------------
# Main loop
# -------------------------
def main_loop():
    print("[WORKER] starting…")
    while True:
        try:
            now_et = datetime.now(ET)
            trade_date = now_et.date()

            daily_reset_if_needed(now_et)

            # Pull snapshots ONCE per cycle (this is the whole point)
            snaps = client.get_snapshot_all("stocks")

            # ===== Page 1 scanner (8:15–9:45 ET) =====
            if is_between(now_et, WINDOW_START, WINDOW_END):
                with pg() as con:
                    for s in snaps:
                        ticker = getattr(s, "ticker", None)
                        if not ticker:
                            continue

                        fields = get_min_fields(s)
                        if not fields:
                            continue

                        price, minute_high, cum_vol, _ts_ms = fields
                        if price <= 0:
                            continue
                        if cum_vol < MIN_CUM_VOL:
                            continue

                        # load existing row if present
                        cur = con.execute("""
                          select baseline_8am, start_price, max_gain_window,
                                 hit_gain, qualified_locked, bursts_5m, bursts_10m, bursts_total,
                                 last_5m_counted_t, last_5m_vol, last_10m_vol
                          from scanner_tickers
                          where trade_date=%s and ticker=%s
                        """, (trade_date, ticker))
                        row = cur.fetchone()

                        info = {
                            "baseline_8am": None,
                            "start_price": price,
                            "current_price": price,
                            "cum_vol": cum_vol,
                            "max_gain_window": 0.0,
                            "hit_gain": False,
                            "qualified_locked": False,
                            "bursts_5m": 0,
                            "bursts_10m": 0,
                            "bursts_total": 0,
                            "last_5m_counted_t": None,
                            "last_5m_vol": 0.0,
                            "last_10m_vol": 0.0,
                        }

                        if row:
                            (baseline_8am, start_price, max_gain_window,
                             hit_gain, qualified_locked, bursts_5m, bursts_10m, bursts_total,
                             last_5m_counted_t, last_5m_vol, last_10m_vol) = row
                            info.update({
                                "baseline_8am": baseline_8am,
                                "start_price": start_price if start_price is not None else price,
                                "max_gain_window": float(max_gain_window or 0.0),
                                "hit_gain": bool(hit_gain),
                                "qualified_locked": bool(qualified_locked),
                                "bursts_5m": int(bursts_5m or 0),
                                "bursts_10m": int(bursts_10m or 0),
                                "bursts_total": int(bursts_total or 0),
                                "last_5m_counted_t": last_5m_counted_t,
                                "last_5m_vol": float(last_5m_vol or 0.0),
                                "last_10m_vol": float(last_10m_vol or 0.0),
                            })

                        info["current_price"] = price
                        info["cum_vol"] = cum_vol

                        # baseline once
                        if info["baseline_8am"] is None:
                            base = fetch_exact_8am_baseline(ticker, now_et)
                            info["baseline_8am"] = base if (base and base > 0) else price

                        base = info["baseline_8am"] or price
                        gain_now = (minute_high - base) / base if base > 0 else 0.0
                        info["max_gain_window"] = max(info["max_gain_window"], gain_now)

                        # bursts
                        try:
                            last_t, last_v, prev_t, prev_v = get_last_two_5m_bars(ticker, now_et)
                        except Exception:
                            last_t = None

                        if last_t is not None:
                            info["last_5m_vol"] = last_v
                            vol10 = last_v + (prev_v if prev_t is not None else 0.0)
                            info["last_10m_vol"] = vol10

                            if info["last_5m_counted_t"] != last_t:
                                info["last_5m_counted_t"] = last_t
                                did_5m = (last_v >= THRESH_VOL)
                                did_10m = (prev_t is not None) and (vol10 >= THRESH_VOL) and (not did_5m)

                                if did_5m:
                                    info["bursts_5m"] += 1
                                    info["bursts_total"] += 1
                                    insert_event(con, trade_date, now_et, ticker, "BURST_5M", f"5mVol={last_v:,.0f}")
                                if did_10m:
                                    info["bursts_10m"] += 1
                                    info["bursts_total"] += 1
                                    insert_event(con, trade_date, now_et, ticker, "BURST_10M", f"10mVol={vol10:,.0f}")

                        # qualify
                        if (not info["hit_gain"]) and (info["max_gain_window"] >= GAIN_THRESHOLD) and (info["bursts_total"] >= 3):
                            info["hit_gain"] = True
                            info["qualified_locked"] = True
                            insert_event(con, trade_date, now_et, ticker, "HIT_GAIN",
                                         f"Hit {GAIN_THRESHOLD*100:.1f}% vs base={base:.4f}; bursts={info['bursts_total']}")

                            # global add (shared for ALL users)
                            upsert_global_watchlist(con, trade_date, ticker, "PAGE1_QUALIFIED", now_et)

                        upsert_scanner_ticker(con, trade_date, ticker, info)

                        gain_pct = ((price - base) / base * 100.0) if base > 0 else 0.0
                        insert_series_point(con, trade_date, ticker, now_et, {
                            "gain_pct": gain_pct,
                            "cum_vol": cum_vol,
                            "last_5m_vol": info["last_5m_vol"],
                            "last_10m_vol": info["last_10m_vol"],
                            "bursts_total": info["bursts_total"],
                        })

            # ===== Page 3 RTH alerts (9:30–16:00 ET) =====
            if is_rth(now_et):
                with pg() as con:
                    for s in snaps:
                        ticker = getattr(s, "ticker", None)
                        if not ticker:
                            continue

                        # universe trim like your code
                        if get_day_volume(s) < 100_000:
                            continue

                        fields = get_min_fields(s)
                        if not fields:
                            continue

                        price, _high, av, ts_ms = fields
                        ts_et = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).astimezone(ET)

                        d10 = compute_delta(ticker, ts_et, av, 10)
                        d2 = compute_delta(ticker, ts_et, av, 2)

                        hit10 = (d10 is not None) and (d10 >= VOL_10M_THRESHOLD)
                        hit2 = (d2 is not None) and (d2 >= VOL_2M_THRESHOLD)

                        if not (hit10 or hit2):
                            continue

                        if hit10:
                            win = 10
                            delta = int(d10)
                            reason = f"ΔVol {win}m ≥ {VOL_10M_THRESHOLD:,}"
                            source = "PAGE3_VOL_10M"
                        else:
                            win = 2
                            delta = int(d2)
                            reason = f"ΔVol {win}m ≥ {VOL_2M_THRESHOLD:,}"
                            source = "PAGE3_VOL_2M"

                        # write once/day per ticker per window
                        upsert_rth_alert(con, trade_date, ticker, win, ts_et, reason, delta, price)

                        # global add (shared)
                        upsert_global_watchlist(con, trade_date, ticker, source, ts_et)

                        # market cap cache so Page2 can filter fast
                        ensure_market_cap_cached(con, ticker)

            # ===== Page 2 buy signals (once for union watchlist) =====
            # After 9:30 ET compute for ALL tickers: global + user watchlists
            if now_et >= now_et.replace(hour=9, minute=30, second=0, microsecond=0):
                with pg() as con:
                    tickers = get_all_watchlist_tickers(con, trade_date)

                for tkr in tickers:
                    df = fetch_1m_ohlc(tkr, now_et, lookback_minutes=LOOKBACK_MINUTES)
                    if df.empty or len(df) < max(30, BOLL_PERIOD + 5):
                        continue

                    df["rsi14"] = rsi_wilder(df["close"], 14)
                    df["boll_lower"] = boll_lower(df["close"], BOLL_PERIOD, BOLL_STD)

                    # closed candle = -2, prev = -3
                    closed = df.iloc[-2]
                    prev = df.iloc[-3]
                    closed_time = df.index[-2]

                    rsi_val = df["rsi14"].iloc[-2]
                    boll_l = df["boll_lower"].iloc[-2]
                    if pd.isna(rsi_val) or pd.isna(boll_l):
                        continue

                    boll_touch = (closed["low"] <= float(boll_l))
                    cond_rsi = (float(rsi_val) <= RSI_THRESHOLD)

                    if not (boll_touch and cond_rsi):
                        continue

                    big_green = is_big_green_vs_prev(prev["open"], prev["close"], closed["open"], closed["close"])
                    hammer = is_hammer_any_color(closed["open"], closed["high"], closed["low"], closed["close"])
                    piercing = is_piercing(prev["open"], prev["close"], closed["open"], closed["close"])

                    pattern = "BIG_GREEN" if big_green else ("HAMMER" if hammer else ("PIERCING" if piercing else ""))
                    if not pattern:
                        continue

                    details = (
                        f"RSI={float(rsi_val):.2f}<= {RSI_THRESHOLD}; "
                        f"BOLL low<=lower; pattern={pattern}; "
                        f"closed={closed_time.strftime('%Y-%m-%d %H:%M')}"
                    )

                    with pg() as con:
                        insert_buy_signal(
                            con, trade_date, closed_time, tkr,
                            float(closed["close"]),
                            float(rsi_val),
                            float(boll_l),
                            pattern,
                            details
                        )

            print(f"[WORKER] ok @ {now_et.isoformat()}")

        except Exception as e:
            print(f"[WORKER] ERROR: {repr(e)}")

        time.sleep(REFRESH_SECONDS)

if __name__ == "__main__":
    main_loop()
