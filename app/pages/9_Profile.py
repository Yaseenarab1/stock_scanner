import streamlit as st
from app.auth import require_login, logout_button
from app.db import pg_conn


st.set_page_config(page_title="Profile", layout="wide")
require_login()

st.title("👤 Profile / Notifications")
logout_button()

user = st.session_state.auth_user
user_id = user["id"]
email = user.get("email") or ""

st.write(f"Signed in as: **{email}**")

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

    row = con.execute(
        """
        select alerts_enabled
        from public.user_notification_prefs
        where user_id=%s
        """,
        (user_id,),
    ).fetchone()

enabled = bool(row[0]) if row else True

new_enabled = st.toggle("Enable email notifications", value=enabled)
if new_enabled != enabled:
    with pg_conn() as con:
        con.execute(
            """
            update public.user_notification_prefs
            set alerts_enabled=%s, updated_at=now()
            where user_id=%s
            """,
            (new_enabled, user_id),
        )
    st.success("Saved.")

st.caption("This controls email alerts for Pages 3, 4, and Page 2 buy signals.")

