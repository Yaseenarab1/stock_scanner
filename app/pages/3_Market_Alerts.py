import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from auth import require_login, logout_button
from db import pg_conn

try:
    from ui import apply_theme, page_header, section, kpi_row, clock_caption, auto_column_config
except ModuleNotFoundError:
    from app.ui import apply_theme, page_header, section, kpi_row, clock_caption, auto_column_config

ET = ZoneInfo("America/New_York")

apply_theme(page_title="Market Alerts", icon="🔔")
require_login()

page_header(
    "Market Alerts",
    subtitle="Real-time RTH volume alerts across all stocks and prices, with an "
             "audible ping whenever a fresh alert lands.",
    eyebrow="PAGE 3 · RTH ALERTS",
)

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
    st.divider()
    logout_button()

with pg_conn() as con:
    alerts = pd.read_sql("""
        select
            (a.first_seen_ts_et at time zone 'America/New_York') as "When",
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

if alerts.empty:
    last_px, busiest = "—", "—"
else:
    last_px = f"${float(alerts.iloc[0]['price']):,.2f}" if pd.notna(alerts.iloc[0]["price"]) else "—"
    busiest = str(alerts.iloc[0]["ticker"])

kpi_row([
    {"label": "Alerts Today", "value": f"{len(alerts):,}"},
    {"label": "Most Recent", "value": busiest},
    {"label": "Last Price", "value": last_px},
    {"label": "Cap Filter", "value": "< 150M" if mcap_only else "off"},
])

section("Alerts", hint="today · newest first")
if alerts.empty:
    st.info("No alerts yet today.")
else:
    st.dataframe(alerts, use_container_width=True, hide_index=True,
                 column_config=auto_column_config(alerts))

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

st.divider()
clock_caption(now)
time.sleep(int(refresh))
st.rerun()
