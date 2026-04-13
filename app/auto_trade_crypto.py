"""
Fernet encryption for Webull API credentials.
Set the same key in Streamlit secrets and the worker environment:
  AUTO_TRADE_FERNET_KEY=<urlsafe base64 32-byte key>
"""
from __future__ import annotations

import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


def _fernet_from_key(key: str) -> Fernet:
    return Fernet(key.strip().encode("ascii"))


def encrypt_str(plaintext: str, key: str) -> str:
    if not plaintext:
        return ""
    f = _fernet_from_key(key)
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_str(ciphertext: str, key: str) -> str:
    if not ciphertext:
        return ""
    f = _fernet_from_key(key)
    return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")

    
def decrypt_str_safe(ciphertext: str, key: str) -> Optional[str]:
    if not ciphertext:
        return None
    try:
        return decrypt_str(ciphertext, key)
    except Exception:
        return None

def get_fernet_key_from_streamlit_secrets() -> Optional[str]:
    try:
        import streamlit as st

        k = st.secrets.get("AUTO_TRADE_FERNET_KEY")
        return str(k).strip() if k else None
    except Exception:
        return None


def get_fernet_key_from_env() -> Optional[str]:
    k = os.environ.get("AUTO_TRADE_FERNET_KEY")
    return k.strip() if k else None
