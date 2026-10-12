"""Thin wrapper around OANDA v20 for account, pricing, candles, and orders.

Every entry goes in as a MARKET order with a server-side stop-loss AND
take-profit attached (stopLossOnFill / takeProfitOnFill), so the brackets sit
at OANDA and trigger even if this machine is off.

Practice vs live is chosen entirely by config.OANDA_HOST. The same code path
serves both -- that is the whole point of validating on practice first.
"""
from __future__ import annotations

import requests

import config

_SESSION = requests.Session()
_TIMEOUT = 20


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.OANDA_API_TOKEN}",
        "Content-Type": "application/json",
    }


def _acct_url(path: str) -> str:
    return f"{config.OANDA_HOST}/v3/accounts/{config.OANDA_ACCOUNT_ID}{path}"


def _req(method: str, url: str, **kw):
    r = _SESSION.request(method, url, headers=_headers(), timeout=_TIMEOUT, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"OANDA {method} {url.split('/v3')[-1]} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.text else {}


# ------------------------------------------------------------- account ----
def get_account() -> dict:
    """Normalized account snapshot. `equity` is OANDA NAV (balance + unrealized)."""
    data = _req("GET", _acct_url("/summary"))["account"]
    return {
        "equity": float(data["NAV"]),
        "balance": float(data["balance"]),
        "cash": float(data.get("marginAvailable", data["balance"])),
        "unrealized_pl": float(data.get("unrealizedPL", 0.0)),
        "margin_used": float(data.get("marginUsed", 0.0)),
        "currency": data.get("currency", config.ACCOUNT_CCY),
        "open_trades": int(data.get("openTradeCount", 0)),
    }


def get_positions() -> list[dict]:
    """Open trades, normalized. Units are signed: + long, - short."""
    trades = _req("GET", _acct_url("/openTrades")).get("trades", [])
    out = []
    for t in trades:
        units = float(t["currentUnits"])
        out.append({
            "trade_id": t["id"],
            "instrument": t["instrument"],
            "units": units,
            "side": "long" if units > 0 else "short",
            "avg_entry": float(t["price"]),
            "unrealized_pl": float(t.get("unrealizedPL", 0.0)),
            "current_price": None,  # filled in by the caller from pricing()
        })
    return out


def get_recent_orders(limit: int = 20) -> list[dict]:
    """Recent trades (open + closed), newest first -- used as the order history."""
    trades = _req("GET", _acct_url(f"/trades?state=ALL&count={limit}")).get("trades", [])
    out = []
    for t in trades:
        units = float(t["initialUnits"])
        out.append({
            "instrument": t["instrument"],
            "side": "buy" if units > 0 else "sell",
            "units": abs(units),
            "fill": float(t["price"]),
            "state": t["state"],
            "time": t.get("openTime", "")[:19],
            "realized_pl": float(t.get("realizedPL", 0.0)),
        })
    return out


# --------------------------------------------------------------- market ---
def pricing(instruments: list[str]) -> dict:
    """Return {instrument: {bid, ask, mid, tradeable}} for the given instruments."""
    if not instruments:
        return {}
    ins = "%2C".join(instruments)  # url-encoded comma
    data = _req("GET", _acct_url(f"/pricing?instruments={ins}"))
    out = {}
    for p in data.get("prices", []):
        bid = float(p["bids"][0]["price"]) if p.get("bids") else None
        ask = float(p["asks"][0]["price"]) if p.get("asks") else None
        out[p["instrument"]] = {
            "bid": bid,
            "ask": ask,
            "mid": (bid + ask) / 2.0 if (bid is not None and ask is not None) else None,
            "tradeable": bool(p.get("tradeable", False)),
        }
    return out


def latest_price(instrument: str) -> float | None:
    return pricing([instrument]).get(instrument, {}).get("mid")


def conversion_rate(from_ccy: str, to_ccy: str) -> float | None:
    """Rate to convert 1 unit of `from_ccy` into `to_ccy`, via OANDA pricing.

    Tries the direct instrument (FROM_TO), then the inverse (TO_FROM). Each is
    priced alone so an invalid instrument can't 400 the other. Returns None if
    neither exists -- the caller then refuses to size that trade.
    """
    if from_ccy == to_ccy:
        return 1.0
    for inst, invert in ((f"{from_ccy}_{to_ccy}", False), (f"{to_ccy}_{from_ccy}", True)):
        try:
            mid = pricing([inst]).get(inst, {}).get("mid")
        except RuntimeError:
            mid = None
        if mid:
            return (1.0 / mid) if invert else mid
    return None


def market_open() -> bool:
    """FX is open if OANDA reports our first pair as tradeable right now."""
    probe = config.PAIRS[0]
    return pricing([probe]).get(probe, {}).get("tradeable", False)


def get_candles(instrument: str, granularity: str | None = None,
                count: int | None = None) -> list[dict]:
    """Completed OHLC(V) mid candles, oldest first."""
    granularity = granularity or config.CANDLE_GRANULARITY
    count = count or config.CANDLE_COUNT
    url = (f"{config.OANDA_HOST}/v3/instruments/{instrument}/candles"
           f"?granularity={granularity}&count={count}&price=M")
    r = _SESSION.get(url, headers=_headers(), timeout=_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"OANDA candles {instrument} -> {r.status_code}: {r.text[:200]}")
    out = []
    for c in r.json().get("candles", []):
        if not c.get("complete", False):
            continue
        m = c["mid"]
        out.append({
            "time": c["time"][:19],
            "open": float(m["o"]), "high": float(m["h"]),
            "low": float(m["l"]), "close": float(m["c"]),
            "volume": int(c.get("volume", 0)),
        })
    return out


# --------------------------------------------------------------- orders ---
def place_market(instrument: str, units: int,
                 stop_pips: float, target_pips: float, ref_price: float) -> dict:
    """MARKET order with attached stop-loss + take-profit.

    units: signed integer (+ long, - short). The stop-loss is a price DISTANCE
    from the actual fill (StopLossDetails supports `distance`, so risk stays
    exact even with slippage). The take-profit MUST be an absolute price --
    OANDA's TakeProfitDetails has no `distance` field -- so it is computed from
    `ref_price` (the quote used for sizing: ask for longs, bid for shorts).
    """
    pip = config.pip_size(instrument)
    dec = config.price_decimals(instrument)
    sl_dist = f"{stop_pips * pip:.{dec}f}"
    direction = 1 if units > 0 else -1
    tp_price = f"{ref_price + direction * target_pips * pip:.{dec}f}"
    body = {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(int(units)),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"distance": sl_dist, "timeInForce": "GTC"},
            "takeProfitOnFill": {"price": tp_price, "timeInForce": "GTC"},
        }
    }
    resp = _req("POST", _acct_url("/orders"), json=body)
    fill = resp.get("orderFillTransaction") or {}
    opened = fill.get("tradeOpened") or {}
    return {
        "instrument": instrument,
        "units": int(units),
        "fill_price": float(fill["price"]) if fill.get("price") else None,
        "trade_id": opened.get("tradeID"),
        "status": "filled" if fill else "no_fill",
        "sl_distance": sl_dist,
        "tp_price": tp_price,
        "raw": resp,
    }


def close_position(instrument: str) -> dict:
    """Fully close all open trades on one instrument, closing each by trade ID.

    Closing by trade ID (`/trades/{id}/close` with units=ALL) is unambiguous:
    it never asks OANDA to close a side with no exposure, which the position
    endpoint (longUnits=ALL + shortUnits=ALL) would 400-reject.
    """
    trades = _req("GET", _acct_url("/openTrades")).get("trades", [])
    ids = [t["id"] for t in trades if t["instrument"] == instrument]
    if not ids:
        return {"instrument": instrument, "closed_trades": 0, "note": "nothing open"}
    for tid in ids:
        _req("PUT", _acct_url(f"/trades/{tid}/close"), json={"units": "ALL"})
    return {"instrument": instrument, "closed_trades": len(ids)}


def close_all() -> list[str]:
    """Close every open position. Returns the instruments closed."""
    closed = []
    for inst in {p["instrument"] for p in get_positions()}:
        try:
            close_position(inst)
            closed.append(inst)
        except RuntimeError:
            pass
    return closed
