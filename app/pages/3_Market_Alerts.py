import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from auth import require_login, logout_button
from db import pg_conn

ET = ZoneInfo("America/New_York")

st.set_page_config(page_title="open market screanner - all of the stocks - all prices", layout="wide")
require_login()

st.title("🔔 Page 3 — RTH Alerts (DB-backed)")
logout_button()

now = datetime.now(ET)
trade_date = now.date()

# tiny wav "click" (works fine)
WAV_B64 = "UklGRiQAAABXQVZFZm10IBAAAAABAAEAESsAACJWAAACABAAZGF0YQAAAAA="

def play_beep_once(nonce: str, wav_b64: str):
    components.html(f"""
    <!-- nonce:{nonce} -->
    <audio autoplay>
      <source src="data:audio/wav;base64,{wav_b64}" type="audio/wav">
    </audio>
    """, height=0, width=0)

with st.sidebar:
    refresh = st.slider("Refresh (seconds)", 5, 60, 10)
    limit = st.slider("Rows", 50, 500, 250, 50)
    mcap_only = st.checkbox("Only market cap < 150M", value=False)

with pg_conn() as con:
    alerts = pd.read_sql("""
        set timezone = 'America/New_York';
        select
            a.first_seen_ts_et as "When",
            a.ticker,
            a.reason,
            a.delta_shares,
            a.window_min,
            a.price,
            f.market_cap
        from rth_alerts a
        left join ticker_fundamentals f
          on f.ticker = a.ticker
        where a.trade_date=%s
        order by a.first_seen_ts_et desc
        limit %s
    """, con, params=[trade_date, limit])

if mcap_only and not alerts.empty:
    alerts = alerts[(alerts["market_cap"].notna()) & (alerts["market_cap"] < 150_000_000)]

st.subheader("Alerts (today)")
if alerts.empty:
    st.info("No alerts yet today.")
else:
    st.dataframe(alerts, use_container_width=True, hide_index=True)

# Beep only on new alerts (per browser session)
if "seen_alert_keys" not in st.session_state:
    st.session_state.seen_alert_keys = set()

new_rows = []
for _, r in alerts.iterrows():
    when_iso = pd.to_datetime(r["When"]).isoformat()
    key = f"{when_iso}_{r['ticker']}_{int(r['window_min'])}"
    if key not in st.session_state.seen_alert_keys:
        new_rows.append(key)
        st.session_state.seen_alert_keys.add(key)

if new_rows:
    play_beep_once(str(datetime.now().timestamp()), WAV_B64)
    st.toast(f"🚨 {len(new_rows)} new alert(s)", icon="🚨")

st.caption("Auto-refreshing…")
time.sleep(int(refresh))
st.rerun()
