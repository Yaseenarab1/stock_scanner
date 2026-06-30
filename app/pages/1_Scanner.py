import streamlit as st
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

from app.auth import require_login, logout_button
from app.db import pg_conn

try:
    from ui import apply_theme, page_header, section, kpi_row, clock_caption
except ModuleNotFoundError:
    from app.ui import apply_theme, page_header, section, kpi_row, clock_caption



ET = ZoneInfo("America/New_York")

apply_theme(page_title="Scanner", icon="⚡")
require_login()

page_header(
    "Pre-Market Scanner",
    subtitle="Live, database-backed scan of qualifying tickers — volume bursts, "
             "intraday gains and locked-in movers, refreshed automatically.",
    eyebrow="PAGE 1 · SCANNER",
)

now = datetime.now(ET)
trade_date = now.date()

with st.sidebar:
    st.markdown(f"**Trade date**\n\n`{trade_date}`")
    st.divider()
    if st.button("👤 Profile / Notifications", use_container_width=True):
        st.switch_page("pages/9_Profile.py")
    if st.button("🤖 Auto-trade (Webull)", use_container_width=True):
        st.switch_page("pages/10_Auto_Trade.py")
    st.divider()
    refresh = st.slider("Refresh (seconds)", 5, 60, 10)
    logout_button()

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

# ── Live KPIs ────────────────────────────────────────────────────────────────
if df.empty:
    locked_n = 0
    top_txt, top_trend = "—", "flat"
    bursts_n = 0
else:
    locked_n = int(df["qualified_locked"].fillna(False).astype(bool).sum())
    bursts_n = int(df["bursts_total"].fillna(0).sum())
    _top = df.sort_values("max_gain_window_pct", ascending=False).iloc[0]
    _g = float(_top["max_gain_window_pct"] or 0)
    top_txt = f"{_top['ticker']} +{_g:.1f}%"
    top_trend = "up" if _g >= 0 else "down"

kpi_row([
    {"label": "Tickers Scanned", "value": f"{len(df):,}"},
    {"label": "Locked Movers", "value": f"{locked_n:,}", "delta": "💎 qualified", "trend": "up"},
    {"label": "Total Bursts", "value": f"{bursts_n:,}"},
    {"label": "Top Gainer", "value": top_txt, "trend": top_trend},
])

section("Scanner Results", hint="global · sorted by lock → bursts → volume")
if df.empty:
    st.info("No data yet today. The worker will populate this automatically.")
else:
    st.dataframe(df, use_container_width=True, hide_index=True)

st.divider()

section("Add Ticker to My Watchlist", hint="feeds Page 2 · Buy Watcher")
ticker = st.text_input("Ticker to add", value="", placeholder="e.g. NVDA").strip().upper()

if st.button("➕ Add to my watchlist", type="primary"):
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

st.divider()
clock_caption(now)
import time
time.sleep(int(refresh))  # or refresh_rate / refresh_s depending on page
st.rerun()

