import time
import pandas as pd
import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo

from app.auth import require_login, logout_button
from app.db import pg_conn

ET = ZoneInfo("America/New_York")

st.set_page_config(page_title="page 4 - 0.2 - 5$ scanner ", layout="wide")
require_login()

st.title("Page 4 — Low Price ($0.10–$5.00) $10M/10m Target Alerts")
logout_button()

now = datetime.now(ET)
trade_date = now.date()

with st.sidebar:
    refresh = st.slider("Refresh (seconds)", 5, 60, 10)
    limit = st.slider("Rows", 50, 500, 250, 50)
    mcap_only = st.checkbox("Only market cap < 100M (Yahoo)", value=False)

with pg_conn() as con:
    df = pd.read_sql("""
      select a.first_seen_ts_et,
             a.ticker,
             a.price,
             a.delta10_shares,
             a.target_shares,
             a.reason,
             f.market_cap_yf
      from low_price_alerts a
      left join ticker_fundamentals f on f.ticker = a.ticker
      where a.trade_date=%s
      order by a.first_seen_ts_et desc
      limit %s
    """, con, params=[trade_date, limit])

if mcap_only and not df.empty:
    df = df[(df["market_cap_yf"].notna()) & (df["market_cap_yf"] < 150_000_000)]

st.subheader("Low Price Alerts (today)")
if df.empty:
    st.info("No low-price target alerts yet today.")
else:
    st.dataframe(df, use_container_width=True, hide_index=True)

st.caption("Auto-refreshing…")
time.sleep(int(refresh))
st.rerun()
