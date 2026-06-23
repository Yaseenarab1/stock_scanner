import streamlit as st
from supabase import create_client
from streamlit_cookies_manager import EncryptedCookieManager

# Key under which we keep the Supabase refresh token inside the (encrypted) cookie.
_REFRESH_KEY = "sb_refresh_token"


def supabase_client():
    url = st.secrets.get("SUPABASE_URL", "")
    anon = st.secrets.get("SUPABASE_ANON_KEY", "")
    return create_client(url, anon)


def _new_cookies():
    """Create a FRESH cookie manager and stash it for the rest of this run.

    EncryptedCookieManager reads the browser cookie inside its constructor, so
    it must be re-created on every script run (a cached instance never becomes
    `ready()`). We stash this run's instance in session_state only so that other
    helpers called later in the SAME run reuse it instead of building a second
    component (which would raise DuplicateWidgetID).
    """
    cm = EncryptedCookieManager(
        prefix="stockscanner/",
        password=st.secrets.get("COOKIE_PASSWORD", ""),
    )
    st.session_state["_cookie_mgr"] = cm
    return cm


def _cookies():
    """Reuse the instance created earlier this run (or make one if needed)."""
    cm = st.session_state.get("_cookie_mgr")
    return cm if cm is not None else _new_cookies()


def _persist_session(user, session):
    """Save auth into session_state and stash the (rotating) refresh token in the cookie."""
    st.session_state.auth_user = {"id": user.id, "email": user.email}
    st.session_state.access_token = session.access_token if session else None
    st.session_state.refresh_token = session.refresh_token if session else None
    cookies = _cookies()
    if cookies.ready() and session and session.refresh_token:
        cookies[_REFRESH_KEY] = session.refresh_token
        cookies.save()


def _clear_session():
    st.session_state.auth_user = None
    st.session_state.access_token = None
    st.session_state.refresh_token = None
    cookies = _cookies()
    if cookies.ready() and _REFRESH_KEY in cookies:
        del cookies[_REFRESH_KEY]
        cookies.save()


def _restore_from_cookie():
    """Rebuild a logged-in session from the refresh token in the cookie, if present."""
    cookies = _cookies()
    token = cookies.get(_REFRESH_KEY) if cookies.ready() else None
    if not token:
        return False
    try:
        res = supabase_client().auth.refresh_session(token)
        if res and res.session and res.user:
            _persist_session(res.user, res.session)  # also re-saves the rotated token
            return True
    except Exception:
        # Expired / revoked / invalid — drop it so we stop retrying every load.
        if cookies.ready() and _REFRESH_KEY in cookies:
            del cookies[_REFRESH_KEY]
            cookies.save()
    return False


def require_login():
    # Re-create the cookie manager fresh for this run, then wait for it to load.
    cookies = _new_cookies()
    if not cookies.ready():
        st.stop()
    if st.session_state.get("auth_user") is None and not _restore_from_cookie():
        st.switch_page("streamlit_app.py")


def logout_button():
    if st.button("Logout"):
        _clear_session()
        st.rerun()


def login_ui():
    st.title("Login")

    # Fresh cookie manager for this run; wait until it has loaded.
    cookies = _new_cookies()
    if not cookies.ready():
        st.stop()

    # If a valid cookie already exists, restore and go straight in.
    if st.session_state.get("auth_user") is None and _restore_from_cookie():
        st.rerun()

    sb = supabase_client()

    email = st.text_input("Email")
    password = st.text_input("Password", type="password")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sign in"):
            try:
                res = sb.auth.sign_in_with_password({"email": email, "password": password})
                _persist_session(res.user, res.session)
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
