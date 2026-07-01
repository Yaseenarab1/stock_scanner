import streamlit as st
from app.auth import require_login, logout_button
from app.db import pg_conn

try:
    from ui import apply_theme, page_header, section, pill, auto_column_config
except ModuleNotFoundError:
    from app.ui import apply_theme, page_header, section, pill, auto_column_config


apply_theme(page_title="Profile", icon="👤")
require_login()

page_header(
    "Profile & Notifications",
    subtitle="Control which scanners are allowed to email you, and jump to your "
             "auto-trade settings.",
    eyebrow="PAGE 9 · ACCOUNT",
    status=None,
)

user = st.session_state.auth_user
user_id = user["id"]
email = user.get("email") or ""

st.html(f'<div>Signed in as &nbsp; {pill(email, "live")}</div>')
with st.sidebar:
    logout_button()

with pg_conn() as con:
    # ensure prefs row exists
    con.execute(
        """
        insert into public.user_notification_prefs(user_id)
        values (%s)
        on conflict do nothing
        """,
        (user_id,),
    )

    # Try per-page prefs; fall back to global-only if columns don't exist yet.
    try:
        row = con.execute(
            """
            select alerts_enabled, page2_enabled, page3_enabled, page4_enabled
            from public.user_notification_prefs
            where user_id=%s
            """,
            (user_id,),
        ).fetchone()
        has_per_page = True
    except Exception:
        row = con.execute(
            """
            select alerts_enabled
            from public.user_notification_prefs
            where user_id=%s
            """,
            (user_id,),
        ).fetchone()
        has_per_page = False

alerts_enabled = bool(row[0]) if row else True

section("Email Notifications", hint="master + per-page control")
new_alerts_enabled = st.toggle("Master switch", value=alerts_enabled)

page2_enabled = page3_enabled = page4_enabled = True
if has_per_page and row:
    page2_enabled = bool(row[1])
    page3_enabled = bool(row[2])
    page4_enabled = bool(row[3])

if has_per_page:
    st.caption("Choose which pages can email you (master switch must be ON).")
    new_page2 = st.toggle("Page 2 — Buy signals", value=page2_enabled, disabled=not new_alerts_enabled)
    new_page3 = st.toggle("Page 3 — RTH volume alerts", value=page3_enabled, disabled=not new_alerts_enabled)
    new_page4 = st.toggle("Page 4 — Low-price $10M/10m alerts", value=page4_enabled, disabled=not new_alerts_enabled)
else:
    st.caption("Per-page toggles not enabled in DB yet. Run the migration SQL to enable them.")
    new_page2 = new_page3 = new_page4 = None

if new_alerts_enabled != alerts_enabled or (
    has_per_page and row and (
        new_page2 != page2_enabled or new_page3 != page3_enabled or new_page4 != page4_enabled
    )
):
    with pg_conn() as con2:
        if has_per_page:
            con2.execute(
                """
                update public.user_notification_prefs
                set alerts_enabled=%s,
                    page2_enabled=%s,
                    page3_enabled=%s,
                    page4_enabled=%s,
                    updated_at=now()
                where user_id=%s
                """,
                (new_alerts_enabled, new_page2, new_page3, new_page4, user_id),
            )
        else:
            con2.execute(
                """
                update public.user_notification_prefs
                set alerts_enabled=%s, updated_at=now()
                where user_id=%s
                """,
                (new_alerts_enabled, user_id),
            )
    st.success("Saved.")

st.divider()
section("Auto-Trade", hint="Webull integration")
if st.button("🤖 Open Auto-trade (Webull) settings", type="primary"):
    st.switch_page("pages/10_Auto_Trade.py")

