import streamlit as st
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

from app.auth import require_login, logout_button
from app.db import pg_conn





ET = ZoneInfo("America/New_York")

st.set_page_config(page_title="Scanner", layout="wide")
require_login()

st.title("⚡ Page 1 — Scanner (DB-backed)")
logout_button()

now = datetime.now(ET)
trade_date = now.date()

with st.sidebar:
    st.write(f"Trade date: **{trade_date}**")
    refresh = st.slider("Refresh (seconds)", 5, 60, 10)

user_id = st.session_state.auth_user["id"]

# Load scanner tickers (global results)
with pg_conn() as con:
    df = pd.read_sql("""
        select trade_date, ticker, cum_vol, baseline_8am, start_price, current_price,
               (max_gain_window*100.0) as max_gain_window_pct,
               bursts_5m, bursts_10m, bursts_total,
               hit_gain, qualified_locked
        from scanner_tickers
        where trade_date = %s
        order by qualified_locked desc, bursts_total desc, cum_vol desc
        limit 500
    """, con, params=[trade_date])

st.subheader("Scanner results (global)")
if df.empty:
    st.info("No data yet today. Worker will populate automatically.")
else:
    st.dataframe(df, use_container_width=True, hide_index=True)

st.divider()

st.subheader("➕ Add ticker to MY watchlist (Page 2)")
ticker = st.text_input("Ticker to add", value="", placeholder="e.g. NVDA").strip().upper()

if st.button("Add to my watchlist"):
    if not ticker:
        st.warning("Enter a ticker.")
    else:
        with pg_conn() as con:
            con.execute("""
                insert into user_watchlist(user_id, trade_date, ticker, source)
                values (%s, %s, %s, 'scanner')
                on conflict (user_id, trade_date, ticker) do update set source = excluded.source
            """, (user_id, trade_date, ticker))
        st.success(f"Added {ticker} to your watchlist.")
        st.rerun()

st.caption("Auto-refreshing…")
import time
time.sleep(int(refresh))  # or refresh_rate / refresh_s depending on page
st.rerun()

