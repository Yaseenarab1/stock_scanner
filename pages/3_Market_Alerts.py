# pages/3_Market_Alerts.py
from datetime import datetime
from zoneinfo import ZoneInfo
import streamlit as st
from auth import require_login, get_user_email

require_login()
ET = ZoneInfo("America/New_York")

st.set_page_config(layout="wide", page_title="Page 3 — Market Alerts (Global)")
st.title("🔔 Page 3 — Market Alerts (Global)")

conn = st.connection("supabase", type="sql")

now = datetime.now(ET)
trade_date = now.date().isoformat()

hb = conn.query("select last_seen from public.scanner_heartbeat where id=1", ttl="5s")
if len(hb):
    st.caption(f"Scanner heartbeat: {hb.loc[0,'last_seen']}")
else:
    st.warning("No heartbeat found (scanner may be down).")

with st.sidebar:
    refresh_s = st.slider("Refresh (seconds)", 3, 120, 10)
    min_delta = st.number_input("Min ΔVol", min_value=0, value=0, step=500_000)
    window_min = st.selectbox("Window (minutes)", options=["All", 2, 10], index=0)
    use_price_filter = st.checkbox("Filter by price range", value=False)
    min_price = st.number_input("Min price", min_value=0.0, value=0.5, step=0.1, disabled=not use_price_filter)
    max_price = st.number_input("Max price", min_value=0.0, value=20.0, step=0.5, disabled=not use_price_filter)

st.divider()
alerts = conn.query(
    """
    select ts_et, ticker, reason, vol_delta, window_min, price
    from public.p3_alerts
    where trade_date = :d
    order by ts_et desc
    limit 500
    """,
    params={"d": trade_date},
    ttl="5s"
)

if alerts.empty:
    st.info("No alerts saved yet today.")
else:
    df = alerts.copy()
    df = df[df["vol_delta"].fillna(0) >= int(min_delta)]
    if window_min != "All":
        df = df[df["window_min"] == int(window_min)]
    if use_price_filter:
        df = df[df["price"].fillna(0).between(float(min_price), float(max_price))]

    c1, c2, c3 = st.columns(3)
    c1.metric("Alerts shown", len(df))
    c2.metric("Total saved today", len(alerts))
    c3.metric("Trade date", trade_date)

    st.dataframe(df, use_container_width=True, hide_index=True)

st.caption("Auto-refreshing…")
st.experimental_rerun()
