"""Opening Range Breakout (ORB) on an index CFD, wired to the OANDA bot.

The mechanism (see config.ORB_* for the numbers):
  1. At the US cash open (9:30 ET) the index has a full overnight session of
     positioning built up. Mark the HIGH and LOW of the first ORB_RANGE_MINUTES.
  2. Take the first completed candle that CLOSES beyond that range -- long above
     the high, short below the low. A volume filter kills limp false breakouts.
  3. The protective stop rides the OPPOSITE side of the range; the target is
     ORB_TARGET_R x that risk (2:1 by default).
  4. Sizing (risk.position_size) and the bracketed MARKET order (broker.place_market)
     are the SAME battle-tested code the forex side uses -- the discipline the
     "$1k in 30 min" reels never show. Brackets sit server-side at OANDA.

Safety: honors the kill switch, the daily-loss halt, max-positions, and the two
-switch live-money gate (config.assert_safe). Practice by default. Run it as a
standalone loop (`python orb.py`) or call run_once() from the scheduler.
"""
from __future__ import annotations

import csv
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import broker
import risk

ET = ZoneInfo("America/New_York")
log = logging.getLogger("orb")


# --------------------------------------------------------------- time -----
def _now_et() -> datetime:
    return datetime.now(ET)


def _et_at(hh: int, mm: int, ref: datetime | None = None) -> datetime:
    ref = ref or _now_et()
    return ref.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _candle_et(c: dict) -> datetime:
    """Parse an OANDA candle's (UTC) start time into Eastern."""
    return datetime.fromisoformat(c["time"]).replace(tzinfo=timezone.utc).astimezone(ET)


# --------------------------------------------------------------- state ----
# State is per-instrument so an index loop and an FX loop can run side by side
# without seeing each other's range or entry count.
def _state_file(instrument: str):
    return config.BASE_DIR / f"orb_state_{instrument}.json"


def _load_state(instrument: str) -> dict:
    path = _state_file(instrument)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_state(instrument: str, state: dict) -> None:
    _state_file(instrument).write_text(json.dumps(state, indent=2))


def _today_state(instrument: str) -> dict:
    """State scoped to today (ET). Resets automatically on a new day."""
    state = _load_state(instrument)
    today = _now_et().date().isoformat()
    if state.get("day") != today:
        state = {"day": today, "entries": 0, "range": None, "trades": []}
        _save_state(instrument, state)
    return state


# --------------------------------------------------------- opening range --
def opening_range(instrument: str | None = None) -> dict | None:
    """High/low/avg-volume of the first ORB_RANGE_MINUTES after the open.

    Returns None until the range window has fully elapsed (so the range is
    final, never a moving target). Caches the frozen range in ORB state.
    """
    instrument = instrument or config.ORB_INSTRUMENT
    now = _now_et()
    open_t = _et_at(*config.ORB_OPEN_ET)
    range_end = open_t + timedelta(minutes=config.ORB_RANGE_MINUTES)
    if now < range_end:
        return None  # range not complete yet

    state = _today_state(instrument)
    if state.get("range"):
        return state["range"]

    candles = broker.get_candles(instrument, config.ORB_GRANULARITY,
                                 config.CANDLE_COUNT)
    window = [c for c in candles
              if open_t <= _candle_et(c) < range_end
              and _candle_et(c).date() == now.date()]
    if not window:
        return None

    highs = [c["high"] for c in window]
    lows = [c["low"] for c in window]
    vols = [c["volume"] for c in window]
    rng = {
        "high": max(highs),
        "low": min(lows),
        "avg_volume": sum(vols) / len(vols) if vols else 0,
        "candles": len(window),
    }
    state["range"] = rng
    _save_state(instrument, state)
    dec = config.price_decimals(instrument)
    log.info("ORB range %s: high=%.*f low=%.*f (%d candles)",
             instrument, dec, rng["high"], dec, rng["low"], rng["candles"])
    return rng


# ------------------------------------------------------------- breakout ---
def breakout_signal(instrument: str, rng: dict) -> dict | None:
    """First completed post-range candle that closes beyond the range.

    Returns {'action','breakout_candle','ref_side'} or None. Applies the volume
    filter: the breaking candle must trade more than the range's average volume.
    """
    now = _now_et()
    range_end = _et_at(*config.ORB_OPEN_ET) + timedelta(minutes=config.ORB_RANGE_MINUTES)
    cutoff = _et_at(*config.ORB_ENTRY_CUTOFF_ET)

    candles = broker.get_candles(instrument, config.ORB_GRANULARITY,
                                 config.CANDLE_COUNT)
    post = [c for c in candles if _candle_et(c) >= range_end
            and _candle_et(c).date() == now.date()]
    if not post:
        return None

    last = post[-1]
    if _candle_et(last) > cutoff:
        return None  # too late in the morning to open a fresh breakout

    if config.ORB_VOL_FILTER and last["volume"] <= rng["avg_volume"]:
        return None  # limp break -- no conviction behind it

    if last["close"] > rng["high"]:
        return {"action": "long", "breakout_candle": last}
    if last["close"] < rng["low"]:
        return {"action": "short", "breakout_candle": last}
    return None


# --------------------------------------------------------------- log ------
def _log_trade(row: dict) -> None:
    fields = ["timestamp", "strategy", "instrument", "action", "units",
              "entry", "stop_pts", "target_pts", "trade_id"]
    new = not config.TRADE_LOG.exists()
    with config.TRADE_LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


# ------------------------------------------------------------- execute ----
def run_once() -> dict:
    """One idempotent ORB pass: compute range, check for a break, trade it once.

    Safe to call every poll -- state caps entries at ORB_MAX_ENTRIES/day and the
    risk engine blocks a second position on the same instrument regardless.
    """
    if not config.ORB_DRY_RUN:
        config.assert_safe()  # refuse live unless both switches are set
    inst = config.ORB_INSTRUMENT
    now = _now_et()

    if risk.kill_switch_on():
        return {"status": "halted", "reason": "kill switch on"}

    state = _today_state(inst)

    # End-of-day: flatten any ORB trade before the cash close.
    if now >= _et_at(*config.ORB_FLATTEN_ET):
        held = [p for p in broker.get_positions() if p["instrument"] == inst]
        if held and not config.ORB_DRY_RUN:
            broker.close_position(inst)
            log.info("ORB end-of-day flatten: closed %s", inst)
            return {"status": "flattened", "instrument": inst}
        return {"status": "done", "reason": "after flatten time"}

    if state["entries"] >= config.ORB_MAX_ENTRIES:
        return {"status": "done", "reason": f"{state['entries']} entr(ies) taken today"}

    rng = opening_range(inst)
    if rng is None:
        return {"status": "waiting", "reason": "opening range not complete yet"}

    px = broker.pricing([inst]).get(inst, {})
    if not px.get("tradeable"):
        return {"status": "waiting", "reason": f"{inst} not tradeable right now"}

    sig = breakout_signal(inst, rng)
    if sig is None:
        return {"status": "watching", "range": rng,
                "reason": "no qualifying breakout yet"}

    # ---- size the trade off the opposite side of the range ----
    # raw_stop is a PRICE distance; divide by pip_size so stop_pts is in the
    # instrument's native unit (index points, or forex pips). Clamp bounds come
    # from the matching config block so FX pairs and indices each stay sane.
    action = sig["action"]
    if action == "long":
        ref = px["ask"]
        raw_stop = ref - rng["low"]
    else:
        ref = px["bid"]
        raw_stop = rng["high"] - ref
    raw_pips = raw_stop / config.pip_size(inst)
    if inst in config.INDEX_INSTRUMENTS:
        lo, hi = config.ORB_MIN_STOP_PTS, config.ORB_MAX_STOP_PTS
    else:
        lo, hi = config.MIN_STOP_PIPS, config.MAX_STOP_PIPS
    stop_pts = max(lo, min(raw_pips, hi))
    target_pts = stop_pts * config.ORB_TARGET_R

    acct = broker.get_account()
    equity = acct["equity"]
    if config.ORB_DRY_RUN and equity <= 0:
        equity = config.ORB_SHADOW_EQUITY  # size shadow trades off a synthetic stake
    risk.record_day_start(equity)
    if risk.daily_loss_breached(equity):
        return {"status": "halted", "reason": "daily loss limit hit"}

    # Run the same hard gate the forex side uses (max positions, dup, confidence).
    positions = broker.get_positions()
    entries, _closes, rejected = risk.filter_decisions(
        [{"pair": inst, "action": action, "confidence": config.ORB_CONFIDENCE}],
        positions, equity, universe={inst})
    if not entries:
        return {"status": "rejected", "reason": rejected}

    quote_ccy = inst.split("_")[1]
    rate = broker.conversion_rate(quote_ccy, config.ACCOUNT_CCY) or 1.0
    units = risk.position_size(equity, inst, ref, stop_pts, quote_to_acct=rate)
    if units <= 0:
        return {"status": "skipped", "reason": "size rounded to 0 units"}

    signed = units if action == "long" else -units
    if config.ORB_DRY_RUN:
        # Shadow: everything computed for real, nothing sent to OANDA.
        order = {"instrument": inst, "units": signed, "fill_price": ref,
                 "trade_id": f"SHADOW-{now:%H%M%S}", "status": "shadow"}
    else:
        order = broker.place_market(inst, signed, stop_pips=stop_pts,
                                    target_pips=target_pts, ref_price=ref)

    state["entries"] += 1
    state["trades"].append(order.get("trade_id"))
    _save_state(inst, state)

    row = {
        "timestamp": now.isoformat(timespec="seconds"),
        "strategy": "ORB-SHADOW" if config.ORB_DRY_RUN else "ORB",
        "instrument": inst, "action": action,
        "units": signed, "entry": order.get("fill_price"),
        "stop_pts": round(stop_pts, 1), "target_pts": round(target_pts, 1),
        "trade_id": order.get("trade_id"),
    }
    _log_trade(row)
    log.info("%s %s %s %d units @ %s stop=%.1fpts target=%.1fpts",
             row["strategy"], action.upper(), inst, signed,
             order.get("fill_price"), stop_pts, target_pts)
    return {"status": "shadow_entered" if config.ORB_DRY_RUN else "entered",
            "order": order, "range": rng,
            "stop_pts": round(stop_pts, 1), "target_pts": round(target_pts, 1)}


# --------------------------------------------------------------- loop -----
def loop() -> None:
    """Poll until the session ends, acting on the first clean breakout."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
                  logging.StreamHandler()],
    )
    creds = config.ready()
    if config.ORB_DRY_RUN:
        mode = "SHADOW (no orders)"
    else:
        mode = "LIVE" if creds["live"] else "PRACTICE"
    print(f"ORB loop -> {config.ORB_INSTRUMENT}  [{mode}]  "
          f"range={config.ORB_RANGE_MINUTES}m target={config.ORB_TARGET_R}R")
    if not creds["oanda"]:
        print("!! OANDA credentials missing -- set them in .env")
        return
    while True:
        try:
            res = run_once()
            status = res.get("status")
            print(f"{_now_et():%H:%M:%S} ET  {status}: {res.get('reason', '')}")
            if status in ("entered", "flattened"):
                # keep looping to allow EOD flatten, but slow down after a fill
                pass
            if status == "done" and _now_et() >= _et_at(*config.ORB_FLATTEN_ET):
                print("Session complete.")
                return
        except Exception as e:  # noqa: BLE001 -- never let one bad poll kill the loop
            log.exception("ORB poll error: %s", e)
            print(f"{_now_et():%H:%M:%S} ET  error: {e}")
        time.sleep(config.ORB_POLL_SECONDS)


if __name__ == "__main__":
    loop()
