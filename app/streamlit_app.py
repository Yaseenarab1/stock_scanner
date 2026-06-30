import streamlit as st
from auth import login_ui, require_login, logout_button

try:
    from ui import apply_theme, page_header, section, pill
except ModuleNotFoundError:
    from app.ui import apply_theme, page_header, section, pill

apply_theme(page_title="Stock Scanner", icon="📈")

# If not logged in, show the (now glossy) login screen and stop.
if st.session_state.get("auth_user") is None:
    login_ui()
    st.stop()

require_login()

email = st.session_state.auth_user["email"]

page_header(
    "Command Center",
    subtitle="Your real-time trading cockpit — scanners, alerts, watchlists, "
             "model analytics and automated execution, all in one place.",
    eyebrow="DASHBOARD",
)

st.markdown(f'Signed in as &nbsp; {pill(email, "live")}', unsafe_allow_html=True)

with st.sidebar:
    st.markdown("### 📈 Stock Scanner")
    st.caption("Navigate using the pages above.")
    st.divider()
    logout_button()

# ── Quick navigation cards ────────────────────────────────────────────────────
section("Workspaces", hint="jump straight in")

# switch_page paths are resolved relative to this entrypoint's folder (app/).
NAV = [
    ("⚡", "Scanner", "Pre-market qualified movers", "pages/1_Scanner.py"),
    ("🎯", "Buy Watcher", "Watchlist · signals · charts", "pages/2_Buy_Watcher.py"),
    ("🔔", "Market Alerts", "Live RTH volume alerts", "pages/3_Market_Alerts.py"),
    ("🪙", "Low-Price", "Sub-$5 microcap screener", "pages/4_small_stocks_screanner.py"),
    ("🤖", "Auto-Trade", "Webull automation", "pages/10_Auto_Trade.py"),
    ("👤", "Profile", "Notifications & account", "pages/9_Profile.py"),
]

cols = st.columns(3)
for i, (icon, title, desc, target) in enumerate(NAV):
    with cols[i % 3]:
        st.markdown(
            f"""
            <div class="kpi" style="min-height:104px;">
              <div style="font-size:24px;">{icon}</div>
              <div class="value" style="font-size:18px;font-family:'Inter';font-weight:700;margin-top:4px;">{title}</div>
              <div class="label" style="text-transform:none;letter-spacing:0;margin-top:2px;">{desc}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button(f"Open {title}", key=f"nav_{i}", use_container_width=True):
            try:
                st.switch_page(target)
            except Exception:
                st.error(f"Couldn't open {title}. Use the sidebar page list instead.")

st.divider()
st.caption("Tip: use the left sidebar to switch pages at any time.")
