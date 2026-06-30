import time
import pandas as pd
import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo

from auth import require_login, logout_button
from db import pg_conn

try:
    from ui import apply_theme, page_header, section, kpi_row, clock_caption, auto_column_config
except ModuleNotFoundError:
    from app.ui import apply_theme, page_header, section, kpi_row, clock_caption, auto_column_config

ET = ZoneInfo("America/New_York")

apply_theme(page_title="Low-Price Screener", icon="🪙")
require_login()

page_header(
    "Low-Price Screener",
    subtitle="Sub-$5 names ($0.10–$5.00) hitting the $10M-per-10-minute volume "
             "target — the fast-money microcap watch.",
    eyebrow="PAGE 4 · LOW-PRICE",
)

now = datetime.now(ET)
trade_date = now.date()

with st.sidebar:
    refresh = st.slider("Refresh (seconds)", 5, 60, 10)
    limit = st.slider("Rows", 50, 500, 250, 50)
    mcap_only = st.checkbox("Only market cap < 150M (Yahoo)", value=False)
    st.divider()
    logout_button()

with pg_conn() as con:
    df = pd.read_sql("""
      select (a.first_seen_ts_et at time zone 'America/New_York'),
             a.ticker,
             a.price,
             a.delta10_shares,
             a.target_shares,
             a.reason,
             f.market_cap
      from low_price_alerts a
      left join ticker_fundamentals f on f.ticker = a.ticker
      where a.trade_date=%s
      order by a.first_seen_ts_et desc
      limit %s
    """, con, params=[trade_date, limit])

if mcap_only and not df.empty:
    df = df[(df["market_cap"].notna()) & (df["market_cap"] < 150_000_000)]

uniq = int(df["ticker"].nunique()) if not df.empty else 0
kpi_row([
    {"label": "Alerts Today", "value": f"{len(df):,}"},
    {"label": "Unique Tickers", "value": f"{uniq:,}"},
    {"label": "Price Band", "value": "$0.10–$5.00"},
    {"label": "Target", "value": "$10M / 10m"},
])

section("Low-Price Alerts", hint="today · newest first")
if df.empty:
    st.info("No low-price target alerts yet today.")
else:
    st.dataframe(df, use_container_width=True, hide_index=True,
                 column_config=auto_column_config(df))

st.divider()
clock_caption(now)
time.sleep(int(refresh))
st.rerun()
