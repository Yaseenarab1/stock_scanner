"""
Webull OpenAPI trading helpers (official SDK).
Docs: https://developer.webull.com/api-doc/
Requires: webull-python-sdk-core, webull-python-sdk-trade, webull-python-sdk-mdata
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional, Tuple


def sdk_available() -> bool:
    try:
        from webullsdkcore.client import ApiClient  # noqa: F401
        from webullsdktrade.trade.order_operation import OrderOperation  # noqa: F401
        from webullsdkmdata.quotes.instrument import Instrument  # noqa: F401

        return True
    except ImportError:
        return False


def make_client(app_key: str, app_secret: str):
    from webullsdkcore.client import ApiClient
    from webullsdkcore.common.region import Region

    return ApiClient(app_key, app_secret, Region.US.value)


def resolve_account_id(client, stored: Optional[str]) -> str:
    if stored and str(stored).strip():
        return str(stored).strip()
    from webullsdktrade.trade.account_info import Account

    res = Account(client).get_app_subscriptions()
    if res.status_code != 200:
        raise RuntimeError(f"Webull get_app_subscriptions failed: {res.status_code} {res.text}")
    data = res.json()
    if not data:
        raise RuntimeError("Webull subscriptions empty; link brokerage / approve API access.")
    # quick-start shape: list of dicts with account_id
    if isinstance(data, list) and data and isinstance(data[0], dict):
        aid = data[0].get("account_id")
        if aid:
            return str(aid)
    if isinstance(data, dict):
        aid = data.get("account_id") or (data.get("list") or [{}])[0].get("account_id")
        if aid:
            return str(aid)
    raise RuntimeError(f"Unexpected subscriptions payload: {json.dumps(data)[:500]}")


def get_instrument_id(client, symbol: str) -> str:
    from webullsdkmdata.quotes.instrument import Instrument

    sym = symbol.strip().upper()
    res = Instrument(client).get_instrument(sym, "US_STOCK")
    if res.status_code != 200:
        raise RuntimeError(f"Webull get_instrument failed: {res.status_code} {res.text}")
    data = res.json()
    # Typical: list of instruments
    if isinstance(data, list) and data:
        iid = data[0].get("instrument_id")
        if iid:
            return str(iid)
    if isinstance(data, dict):
        iid = data.get("instrument_id")
        if iid:
            return str(iid)
        lst = data.get("list") or data.get("data") or []
        if lst and isinstance(lst[0], dict) and lst[0].get("instrument_id"):
            return str(lst[0]["instrument_id"])
    raise RuntimeError(f"Could not parse instrument_id for {sym}: {json.dumps(data)[:500]}")


def place_market_equity(
    client,
    account_id: str,
    instrument_id: str,
    side: str,
    qty: int,
    client_order_id: Optional[str] = None,
) -> Tuple[int, Any]:
    """
    US stock market order. side: BUY or SELL per Webull dictionary.
    """
    from webullsdktrade.trade.order_operation import OrderOperation

    coid = (client_order_id or uuid.uuid4().hex)[:40]
    qty_s = str(int(qty))
    op = OrderOperation(client)
    resp = op.place_order(
        account_id,
        qty_s,
        instrument_id,
        side.upper(),
        coid,
        "MARKET",
        False,
        "DAY",
    )
    body = None
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    return resp.status_code, {"client_order_id": coid, "body": body, "raw_text": getattr(resp, "text", "")}
