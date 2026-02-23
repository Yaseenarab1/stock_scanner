import streamlit as st
from supabase import create_client

def supabase_client():
    url = st.secrets.get("SUPABASE_URL", "")
    anon = st.secrets.get("SUPABASE_ANON_KEY", "")
    return create_client(url, anon)

st.title("Unsubscribe")

token = st.query_params.get("token", None)
if not token:
    st.error("Missing token.")
    st.stop()

sb = supabase_client()

try:
    # Call the SQL function
    sb.rpc("unsubscribe_by_token", {"p_token": token}).execute()
    st.success("You are unsubscribed. You will no longer receive alerts.")
except Exception as e:
    st.error(f"Unsubscribe failed: {e}")