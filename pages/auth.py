# auth.py  (put in root / import from pages)
import streamlit as st

def require_login():
    # If you already have a login system, call it here.
    # This is a minimal placeholder to ensure st.session_state["user_email"] exists.
    # Replace with your real Google/OAuth auth routine.
    if "user_email" not in st.session_state or not st.session_state["user_email"]:
        st.warning("Please sign in.")
        # In production, redirect to real auth. Here we show a simple input for dev:
        if st.text_input("Enter email to simulate login", key="login_email"):
            st.session_state["user_email"] = st.session_state["login_email"].strip().lower()
            st.experimental_rerun()

def get_user_email():
    # return canonical user email used by pages
    return st.session_state.get("user_email") or ""
