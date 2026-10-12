"""The risk engine. The analyst proposes; this module disposes.

Hard rules enforced here (see config.py for the numbers):
- Kill switch file halts everything.
- Daily loss limit: equity down > DAILY_LOSS_LIMIT_PCT from day start -> no new
  trades today.
- Max open positions cap.
- Position sizing by risk: each trade risks a fixed % of equity to its stop,
  and total notional is capped at MAX_LEVERAGE x equity.
- Minimum confidence on analyst picks.
- Entries only on pairs in today's analyzed universe; closes only of pairs
  actually held; never stack a second position on a pair already open.
"""
from __future__ import annotations

import json
from datetime import date

import config


def kill_switch_on() -> bool:
    return config.KILL_SWITCH_FILE.exists()


def set_kill_switch(on: bool) -> None:
    if on:
        config.KILL_SWITCH_FILE.write_text("halted")
        return
    if config.KILL_SWITCH_FILE.exists():
        config.KILL_SWITCH_FILE.unlink()


def _load_state() -> dict:
    if config.STATE_FILE.exists():
        try:
            return json.loads(config.STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_state(state: dict) -> None:
    config.STATE_FILE.write_text(json.dumps(state, indent=2))


def record_day_start(equity: float) -> float:
    """Anchor the daily-loss baseline. Resets once per calendar day."""
    state = _load_state()
    today = date.today().isoformat()
    if state.get("day") != today:
        state["day"] = today
        state["day_start_equity"] = equity
        _save_state(state)
    return state["day_start_equity"]


def mark_ran() -> None:
    from datetime import datetime, timezone
    state = _load_state()
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)


def last_run() -> str | None:
    return _load_state().get("last_run")


def daily_loss_breached(equity: float) -> bool:
    state = _load_state()
    start = state.get("day_start_equity")
    if not start or state.get("day") != date.today().isoformat():
        return False
    return equity <= start * (1 - config.DAILY_LOSS_LIMIT_PCT)


def filter_decisions(
    decisions: list[dict],
    positions: list[dict],
    equity: float,
    universe: set[str] | None = None,
) -> tuple[list[dict], list[dict], list[str]]:
    """Apply hard rules. Returns (approved_entries, approved_closes, rejections).

    `universe` = the pairs actually analyzed this cycle. Entries outside it are
    rejected so the analyst can't trade a pair it never saw data for.
    Entry actions are 'long'/'short'; 'close' flattens a held pair.
    """
    if universe is None:
        universe = set(config.PAIRS)
    held = {p["instrument"] for p in positions}
    closed_pairs: set[str] = set()
    entries, closes, rejected = [], [], []
    slots_left = config.MAX_OPEN_POSITIONS - len(held)

    for d in decisions:
        pair = d.get("pair", "").upper()
        action = (d.get("action") or "").lower()
        conf = float(d.get("confidence", 0))

        if action == "close":
            if pair in closed_pairs:
                rejected.append(f"{pair}: duplicate close ignored")
            elif pair in held:
                closes.append({**d, "pair": pair})
                closed_pairs.add(pair)
            else:
                rejected.append(f"{pair}: close rejected — not held")
            continue

        if action not in ("long", "short"):
            rejected.append(f"{pair}: unknown action {action!r}")
            continue

        if pair not in universe:
            rejected.append(f"{pair}: {action} rejected — not in today's analyzed universe")
            continue
        if pair in held:
            rejected.append(f"{pair}: {action} rejected — already have a position")
            continue
        if conf < config.MIN_CONFIDENCE:
            rejected.append(f"{pair}: {action} rejected — confidence {conf:.2f} < {config.MIN_CONFIDENCE}")
            continue
        if slots_left <= 0:
            rejected.append(f"{pair}: {action} rejected — max {config.MAX_OPEN_POSITIONS} positions")
            continue
        slots_left -= 1
        held.add(pair)  # a second decision naming this pair now hits the "already have a position" guard
        entries.append({**d, "pair": pair})

    return entries, closes, rejected


def position_size(equity: float, instrument: str, entry_price: float,
                  stop_pips: float, quote_to_acct: float = 1.0) -> int:
    """Unsigned unit count for one trade.

    Sized so the distance to the stop risks exactly RISK_PER_TRADE_PCT of equity,
    then capped so open notional never exceeds MAX_LEVERAGE x equity.

    `quote_to_acct` converts one unit of the pair's QUOTE currency into the
    account currency (broker.conversion_rate). This makes the math correct for
    ANY pair on ANY account currency -- e.g. EUR_USD on a CAD account uses the
    USD->CAD rate. Pass 1.0 only when the quote currency IS the account currency.
    A rate of 1.0 also recovers the simple USD-account case exactly.
    """
    if entry_price <= 0 or stop_pips <= 0 or quote_to_acct <= 0:
        return 0
    if "_" not in instrument:
        return 0

    pip = config.pip_size(instrument)
    stop_dist = stop_pips * pip                          # price move to stop, in QUOTE ccy
    loss_per_unit = stop_dist * quote_to_acct            # account ccy risked per unit
    notional_per_unit = entry_price * quote_to_acct      # account ccy notional per unit
    if loss_per_unit <= 0 or notional_per_unit <= 0:
        return 0

    risk_budget = equity * config.RISK_PER_TRADE_PCT
    units_by_risk = risk_budget / loss_per_unit
    units_by_leverage = (equity * config.MAX_LEVERAGE) / notional_per_unit
    units = int(min(units_by_risk, units_by_leverage))
    return units if units >= config.MIN_TRADE_UNITS else 0
