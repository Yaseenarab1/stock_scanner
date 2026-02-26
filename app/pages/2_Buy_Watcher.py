import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from polygon import RESTClient
from streamlit_autorefresh import st_autorefresh

from auth import require_login, logout_button
from db import pg_conn

ET = ZoneInfo("America/New_York")

st.set_page_config(page_title="Buy Watcher", layout="wide")
require_login()

st.title("🎟️ Page 2 — Buy Watcher (DB-backed)")
logout_button()

now = datetime.now(ET)
trade_date = now.date()
user_id = st.session_state.auth_user["id"]

# Polygon client (safe to create once here)
client = RESTClient(st.secrets["POLYGON_API_KEY"])

# -----------------------------
# Sidebar controls
# -----------------------------
with st.sidebar:
    refresh = st.slider("Refresh (seconds)", 5, 60, 10)

    st.subheader("Global filters")
    show_only_10m = st.checkbox("Show only 10-minute alerts", value=True)
    mcap_filter = st.checkbox("Only market cap < 150M", value=False)

    st.subheader("Add ticker (manual)")
    new_ticker = st.text_input("Ticker", value="", placeholder="e.g. NVDA").strip().upper()
    if st.button("➕ Add"):
        if new_ticker:
            with pg_conn() as con:
                con.execute(
                    """
                    insert into user_watchlist(user_id, trade_date, ticker, source)
                    values (%s, %s, %s, 'manual')
                    on conflict (user_id, trade_date, ticker)
                    do update set source = excluded.source
                    """,
                    (user_id, trade_date, new_ticker),
                )
            st.success(f"Added {new_ticker}")
            st.rerun()

# Non-blocking autorefresh
st_autorefresh(interval=refresh * 1000, key="page2_refresh")

# -----------------------------
# Load GLOBAL tickers (Page 1 qualified + Page 3 alerts)
# -----------------------------
with pg_conn() as con:
    gw = pd.read_sql(
        """
        select ticker, source, first_seen_ts_et
        from global_watchlist
        where trade_date=%s
        order by first_seen_ts_et desc
        """,
        con,
        params=[trade_date],
    )

# Optional safety net: if worker hasn’t inserted page1 qualified into global_watchlist yet,
# uncomment this to also include them directly:
# with pg_conn() as con:
#     q = pd.read_sql("""
#         select ticker, 'PAGE1_QUALIFIED' as source, max(updated_at) as first_seen_ts_et
#         from scanner_tickers
#         where trade_date=%s and qualified_locked=true
#         group by ticker
#     """, con, params=[trade_date])
# if not q.empty:
#     gw = pd.concat([gw, q], ignore_index=True).drop_duplicates(subset=["ticker","source"])

# -----------------------------
# Load MY watchlist (per-user)
# -----------------------------
with pg_conn() as con:
    wl = pd.read_sql(
        """
        select ticker, source, added_at
        from user_watchlist
        where user_id=%s and trade_date=%s
        order by added_at desc
        """,
        con,
        params=[user_id, trade_date],
    )

# Effective tickers used on this page (global + user)
tickers_all = sorted(set(wl["ticker"].tolist()) | set(gw["ticker"].tolist()))

# -----------------------------
# Remove tickers UI (only affects user_watchlist)
# -----------------------------
st.subheader("My watchlist")
if wl.empty:
    st.info("You have no tickers in your personal watchlist yet.")
else:
    st.dataframe(wl, use_container_width=True, hide_index=True)

with st.expander("Remove tickers from MY watchlist"):
    if wl.empty:
        st.caption("No tickers to remove.")
    else:
        rm = st.multiselect("Select tickers", options=wl["ticker"].tolist())
        if st.button("Remove selected"):
            with pg_conn() as con:
                for t in rm:
                    con.execute(
                        """
                        delete from user_watchlist
                        where user_id=%s and trade_date=%s and ticker=%s
                        """,
                        (user_id, trade_date, t),
                    )
            st.rerun()

# -----------------------------
# GLOBAL 10m alerts table (with optional market cap filter)
# -----------------------------
with pg_conn() as con:
    # If show_only_10m is True, use only window_min=10, else show both 2 and 10.
    if show_only_10m:
        alerts_sql = """
          select a.ticker,
                 (a.first_seen_ts_et at time zone 'America/New_York') as when_ts,
                 a.delta_shares,
                 a.window_min,
                 a.price,
                 f.market_cap
          from rth_alerts a
          left join ticker_fundamentals f on f.ticker = a.ticker
          where a.trade_date = %s
            and a.window_min = 10
          order by when_ts desc
        """
        params = [trade_date]
    else:
        alerts_sql = """
          select a.ticker,
                 (a.first_seen_ts_et at time zone 'America/New_York') as when_ts,
                 a.delta_shares,
                 a.window_min,
                 a.price,
                 f.market_cap
          from rth_alerts a
          left join ticker_fundamentals f on f.ticker = a.ticker
          where a.trade_date = %s
          order by when_ts desc
        """
        params = [trade_date]

    tenm = pd.read_sql(alerts_sql, con, params=params)

if mcap_filter and not tenm.empty:
    tenm = tenm[(tenm["market_cap"].notna()) & (tenm["market_cap"] < 150_000_000)]

st.subheader("🌍 Global Alerts (today)")
if tenm.empty:
    st.info("No alerts yet today.")
else:
    st.dataframe(tenm, use_container_width=True, hide_index=True)

# -----------------------------
# BUY signals (DB) for all tickers shown on this page
# -----------------------------
st.subheader("🧾 Buy signals (today) — based on tickers visible here (global + your list)")
if not tickers_all:
    st.info("No tickers available yet (global list empty and you have no watchlist tickers).")
else:
    with pg_conn() as con:
        buys = pd.read_sql(
            """
            select
              (ts_et at time zone 'America/New_York') as when_ts,
              ticker,
              price,
              rsi,
              boll_lower,
              pattern,
              details
            from buy_signals
            where trade_date=%s and ticker = any(%s)
            order by when_ts desc
            limit 300
            """,
            con,
            params=[trade_date, tickers_all],
        )

    if buys.empty:
        st.info("No buy signals yet today for these tickers.")
    else:
        st.dataframe(buys, use_container_width=True, hide_index=True)

# -----------------------------
# Chart helpers
# -----------------------------
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

@st.cache_data(ttl=20, show_spinner=False)
def fetch_1m_ohlc(ticker: str, lookback_minutes: int) -> pd.DataFrame:
    now_et_local = datetime.now(ET)
    from_dt = now_et_local - timedelta(minutes=lookback_minutes)
    to_dt = now_et_local
    aggs = client.get_aggs(ticker=ticker, multiplier=1, timespan="minute", from_=from_dt, to=to_dt, limit=5000)
    if not aggs:
        return pd.DataFrame()

    rows = []
    for a in aggs:
        t = getattr(a, "timestamp", None) or getattr(a, "t", None)
        o = getattr(a, "open", None) or getattr(a, "o", None)
        h = getattr(a, "high", None) or getattr(a, "h", None)
        l = getattr(a, "low", None) or getattr(a, "l", None)
        c = getattr(a, "close", None) or getattr(a, "c", None)
        v = getattr(a, "volume", None) or getattr(a, "v", None) or 0
        if t is None or o is None or h is None or l is None or c is None:
            continue
        rows.append([int(t), float(o), float(h), float(l), float(c), float(v)])

    df = pd.DataFrame(rows, columns=["t_ms", "open", "high", "low", "close", "volume"])
    df["dt_et"] = pd.to_datetime(df["t_ms"], unit="ms", utc=True).dt.tz_convert(ET)
    df = df.sort_values("dt_et").set_index("dt_et")
    return df

def plot_candles_rsi_boll(df: pd.DataFrame, ticker: str) -> go.Figure:
    df = df.copy()
    df["rsi14"] = rsi_wilder(df["close"], 14)
    df["boll_lower"] = boll_lower(df["close"], 20, 2.0)

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.7, 0.3],
        subplot_titles=(f"{ticker} — 1m Candles + BOLL Lower", "RSI(14)")
    )

    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            name="Price"
        ),
        row=1, col=1
    )

    fig.add_trace(
        go.Scatter(
            x=df.index, y=df["boll_lower"],
            mode="lines",
            name="BOLL Lower"
        ),
        row=1, col=1
    )

    fig.add_trace(
        go.Scatter(
            x=df.index, y=df["rsi14"],
            mode="lines",
            name="RSI(14)"
        ),
        row=2, col=1
    )

    # Optional RSI guide lines
    fig.add_hline(y=30, row=2, col=1)
    fig.add_hline(y=70, row=2, col=1)

    fig.update_layout(
        xaxis_rangeslider_visible=False,
        height=650,
        margin=dict(l=10, r=10, t=50, b=10)
    )
    return fig

# -----------------------------
# Chart UI
# -----------------------------
st.subheader("📈 Chart")
if not tickers_all:
    st.info("No tickers to chart yet.")
else:
    chart_ticker = st.selectbox("Select ticker", options=tickers_all)
    lookback_mins = st.slider("Lookback minutes", 60, 600, 240, 30)

    df = fetch_1m_ohlc(chart_ticker, int(lookback_mins))
    if df.empty or len(df) < 30:
        st.warning("Not enough 1m data yet for chart/RSI/BOLL.")
    else:
        fig = plot_candles_rsi_boll(df.tail(300), chart_ticker)
        st.plotly_chart(fig, use_container_width=True)
