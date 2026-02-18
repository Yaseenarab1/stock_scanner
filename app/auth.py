import streamlit as st
from supabase import create_client
import streamlit as st
from supabase import create_client

def supabase_client():
    url = st.secrets.get("SUPABASE_URL", "")
    anon = st.secrets.get("SUPABASE_ANON_KEY", "")
    return create_client(url, anon)

def require_login():
    if st.session_state.get("auth_user") is None:
        st.switch_page("streamlit_app.py")

def logout_button():
    if st.button("Logout"):
        st.session_state.auth_user = None
        st.session_state.access_token = None
        st.session_state.refresh_token = None
        st.rerun()

def login_ui():
    st.title("Login")

    sb = supabase_client()

    email = st.text_input("Email")
    password = st.text_input("Password", type="password")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sign in"):
            try:
                res = sb.auth.sign_in_with_password({"email": email, "password": password})
                user = res.user
                session = res.session
                st.session_state.auth_user = {"id": user.id, "email": user.email}
                st.session_state.access_token = session.access_token if session else None
                st.session_state.refresh_token = session.refresh_token if session else None
                st.success("Logged in.")
                st.rerun()
            except Exception as e:
                st.error(f"Login failed: {e}")

    with col2:
        if st.button("Create account"):
            try:
                sb.auth.sign_up({"email": email, "password": password})
                st.success("Signup submitted. Check your email if confirmation is enabled.")
            except Exception as e:
                st.error(f"Signup failed: {e}")
