import streamlit as st
from auth import login_ui, require_login, logout_button

st.set_page_config(page_title="Stock Scanner", layout="wide")

# If not logged in, show login and stop.
if st.session_state.get("auth_user") is None:
    login_ui()
    st.stop()

require_login()

st.title("✅ Logged in")
st.write(f"User: {st.session_state.auth_user['email']}")

logout_button()
st.info("Use the left sidebar pages: Scanner, Buy Watcher, Market Alerts.")
