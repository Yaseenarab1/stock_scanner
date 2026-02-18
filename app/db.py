import os
import psycopg
import streamlit as st

def get_database_url() -> str:
    # Streamlit secrets first, then environment
    if "DATABASE_URL" in st.secrets:
        return st.secrets["DATABASE_URL"]
    return os.environ["DATABASE_URL"]

def pg_conn():
    return psycopg.connect(get_database_url(), autocommit=True)
