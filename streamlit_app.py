# streamlit_app.py  (Page 1 - Global Dashboard)
from datetime import datetime
from zoneinfo import ZoneInfo
import streamlit as st
import pandas as pd
from ready_to_ship.pages.auth import require_login

require_login()
ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

st.set_page_config(layout="wide", page_title="Page 1 — Pre-market Scanner (Global)")
st.title("⚡ Page 1 — Pre-market Scanner (Global view)")

# connect to Supabase via Streamlit's SQL connector (Streamlit Cloud) or via supabase-py
conn = st.connection("supabase", type="sql")

now = datetime.now(ET)
trade_date = now.date().isoformat()

# --- heartbeat
hb = conn.query("select last_seen from public.scanner_heartbeat where id=1", ttl="5s")
if len(hb):
    st.caption(f"Scanner heartbeat: {hb.loc[0,'last_seen']}")
else:
    st.warning("No heartbeat found (scanner may be down).")

m1, m2, m3 = st.columns(3)
m1.metric("Time (ET)", now.strftime("%H:%M:%S"))
m2.metric("Time (CT)", now.astimezone(CT).strftime("%H:%M:%S"))
m3.metric("Trade Date (ET)", trade_date)

st.divider()

# --- main table (global state)
df = conn.query(
    """
    select
      ticker,
      cum_vol,
      baseline_8am,
      current_price,
      max_gain_window,
      bursts_5m,
      bursts_10m,
      bursts_total,
      hit_gain,
      qualified_locked,
      updated_at
    from public.p1_tickers
    where trade_date = :d
    order by qualified_locked desc, bursts_total desc, max_gain_window desc, cum_vol desc
    """,
    params={"d": trade_date},
    ttl="5s"
)

st.subheader("📋 Main Table (global)")
if df.empty:
    st.info("No rows yet today (scanner may not have started or no matches).")
else:
    df2 = df.copy()
    df2["cum_vol"] = df2["cum_vol"].fillna(0).astype("int64")
    df2["max_gain_window"] = (df2["max_gain_window"].fillna(0) * 100).round(2)
    df2["hit_gain"] = df2["hit_gain"].astype(bool).map(lambda x: "✅" if x else "")
    df2["qualified_locked"] = df2["qualified_locked"].astype(bool).map(lambda x: "💎" if x else "")
    st.dataframe(df2, use_container_width=True, hide_index=True)

st.subheader("🏆 Locked (global)")
locked = df[df["qualified_locked"] == True] if not df.empty else pd.DataFrame()
if locked.empty:
    st.info("No locked tickers yet.")
else:
    locked2 = locked.copy()
    locked2["cum_vol"] = locked2["cum_vol"].fillna(0).astype("int64")
    locked2["max_gain_window"] = (locked2["max_gain_window"].fillna(0) * 100).round(2)
    st.dataframe(locked2, use_container_width=True, hide_index=True)

st.subheader("🧾 Recent Events (global)")
ev = conn.query(
    """
    select ts_et, ticker, event_type, details
    from public.p1_events
    where trade_date = :d
    order by ts_et desc
    limit 80
    """,
    params={"d": trade_date},
    ttl="5s"
)
if ev.empty:
    st.info("No events yet.")
else:
    st.dataframe(ev, use_container_width=True, hide_index=True)

with st.sidebar:
    refresh_s = st.slider("Refresh (seconds)", 3, 120, 15)

st.caption("Auto-refreshing…")
time.sleep = st.time_input if False else None  # no-op placeholder to avoid lint complaints
st.experimental_rerun()
