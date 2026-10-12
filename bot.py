"""One trading cycle: gather FX data -> Claude analyzes -> risk engine filters ->
place market orders with server-side brackets. Run directly (`python bot.py`)
or via the dashboard.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone

import config
import broker
import risk

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("forexbot")


def _log_trade(row: dict) -> None:
    new_file = not config.TRADE_LOG.exists()
    with open(config.TRADE_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp", "instrument", "action", "units", "price",
                "stop_pips", "target_pips", "confidence", "rationale",
            ],
        )
        if new_file:
            w.writeheader()
        w.writerow(row)


def run_cycle(force: bool = False) -> dict:
    summary = {
        "started": datetime.now(timezone.utc).isoformat(),
        "orders": [],
        "closes": [],
        "rejected": [],
        "skipped": None,
    }

    config.assert_safe()

    creds = config.ready()
    if not creds["anthropic"] or not creds["oanda"]:
        summary["skipped"] = "missing API keys — see dashboard setup"
        return summary

    if risk.kill_switch_on():
        summary["skipped"] = "kill switch is ON"
        log.warning("Cycle skipped: kill switch on")
        return summary

    if not force and not broker.market_open():
        summary["skipped"] = "FX market is closed"
        log.info("Cycle skipped: market closed")
        return summary

    account = broker.get_account()
    if account["currency"] != config.ACCOUNT_CCY:
        summary["skipped"] = (
            f"account currency {account['currency']} != config.ACCOUNT_CCY "
            f"{config.ACCOUNT_CCY} — fix config before trading (sizing would be wrong)"
        )
        log.error(summary["skipped"])
        return summary
    positions = broker.get_positions()
    day_start = risk.record_day_start(account["equity"])

    if risk.daily_loss_breached(account["equity"]):
        summary["skipped"] = (
            f"daily loss limit hit (equity {account['equity']:.2f} vs day start "
            f"{day_start:.2f}) — no new trades today"
        )
        log.warning(summary["skipped"])
        return summary

    # Universe = configured pairs + anything currently held.
    universe = list(config.PAIRS)
    universe += [p["instrument"] for p in positions if p["instrument"] not in universe]

    log.info("Fetching intraday data for %d pairs...", len(universe))
    prices = broker.pricing(universe)
    candles = {}
    for pair in universe:
        try:
            candles[pair] = broker.get_candles(pair)
        except RuntimeError as e:
            log.warning("Candles unavailable for %s (%s)", pair, e)
    # enrich held positions with a current price for display/logging
    for p in positions:
        p["current_price"] = prices.get(p["instrument"], {}).get("mid")

    universe_set = {p for p, c in candles.items() if c}

    log.info("Asking %s for decisions across %d pairs...",
             config.CLAUDE_MODEL, len(universe_set))

    import analyst
    analysis = analyst.analyze(account, positions, candles, prices)
    analysis["timestamp"] = datetime.now(timezone.utc).isoformat()
    config.ANALYSIS_FILE.write_text(json.dumps(analysis, indent=2), encoding="utf-8")
    log.info("Outlook: %s", analysis.get("market_outlook", ""))

    entries, closes, rejected = risk.filter_decisions(
        analysis.get("decisions", []),
        positions,
        account["equity"],
        universe=universe_set,
    )
    summary["rejected"] = rejected
    for r in rejected:
        log.info("Risk engine: %s", r)

    # ---- closes first (frees exposure before opening new risk) ----
    for d in closes:
        try:
            broker.close_position(d["pair"])
            log.info("CLOSED %s — %s", d["pair"], d.get("rationale", ""))
            summary["closes"].append(d["pair"])
            _log_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "instrument": d["pair"], "action": "close", "units": "",
                "price": "", "stop_pips": "", "target_pips": "",
                "confidence": d.get("confidence", ""), "rationale": d.get("rationale", ""),
            })
        except Exception as e:
            log.error("Failed to close %s: %s", d["pair"], e)

    # ---- entries ----
    for d in entries:
        pair = d["pair"]
        try:
            px = prices.get(pair, {})
            side = d["action"]
            entry_price = px.get("ask") if side == "long" else px.get("bid")
            if not entry_price or not px.get("tradeable", False):
                summary["rejected"].append(f"{pair}: {side} rejected — not tradeable / no price")
                continue
            stop_pips = float(d.get("stop_pips") or config.STOP_LOSS_PIPS)
            target_pips = float(d.get("target_pips") or config.TAKE_PROFIT_PIPS)
            # Clamp the model's stop into a sane band before sizing: a stop tighter
            # than the spread would size a max-leverage position that stops out on
            # the first tick; an absurd stop over the ceiling is a bad signal.
            stop_pips = max(config.MIN_STOP_PIPS, min(stop_pips, config.MAX_STOP_PIPS))
            # Keep reward:risk at least 1:1 — never target less than the stop.
            target_pips = max(target_pips, stop_pips)
            # Convert the pair's quote-currency P&L into the account currency so
            # sizing is exact even when neither leg is the account ccy.
            quote_ccy = pair.split("_")[1]
            quote_to_acct = broker.conversion_rate(quote_ccy, config.ACCOUNT_CCY)
            if not quote_to_acct:
                summary["rejected"].append(
                    f"{pair}: {side} rejected — no {quote_ccy}->{config.ACCOUNT_CCY} conversion rate")
                continue
            units = risk.position_size(account["equity"], pair, entry_price, stop_pips, quote_to_acct)
            if units <= 0:
                summary["rejected"].append(f"{pair}: {side} rejected — computed size is zero")
                continue
            signed = units if side == "long" else -units
            order = broker.place_market(pair, signed, stop_pips, target_pips, entry_price)
            if order["status"] != "filled":
                summary["rejected"].append(f"{pair}: {side} not filled (FOK)")
                continue
            log.info("%s %s x%d @ ~%.5f (stop %.0f / target %.0f pips) — %s",
                     side.upper(), pair, units, order["fill_price"] or entry_price,
                     stop_pips, target_pips, d.get("rationale", ""))
            summary["orders"].append(order)
            _log_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "instrument": pair, "action": side, "units": signed,
                "price": round(order["fill_price"] or entry_price, 5),
                "stop_pips": stop_pips, "target_pips": target_pips,
                "confidence": d.get("confidence", ""), "rationale": d.get("rationale", ""),
            })
        except Exception as e:
            log.error("Failed to open %s %s: %s", d.get("action"), pair, e)

    risk.mark_ran()
    summary["outlook"] = analysis.get("market_outlook", "")
    log.info("Cycle done: %d entries, %d closes, %d rejected.",
             len(summary["orders"]), len(summary["closes"]), len(rejected))
    return summary


if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    result = run_cycle(force=force)
    print(json.dumps({k: v for k, v in result.items() if k != "raw"}, indent=2, default=str))
