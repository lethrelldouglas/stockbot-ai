"""The AI brain: Claude reads intraday FX data each cycle and proposes trades.

Claude only PROPOSES. Every proposal then passes through risk.py, which
enforces position sizing, stop-losses, position caps, and the daily loss
limit no matter what the model says.
"""
from __future__ import annotations

import json

import anthropic

import config

# ------------------------------------------------------------- Schema -----
# Claude must return exactly this shape. market_outlook is a short read on FX
# conditions right now; decisions is the (possibly empty) list of proposals.
# Every proposal is re-validated by the risk engine before anything executes.
PICKS_SCHEMA = {
    "type": "object",
    "properties": {
        "market_outlook": {
            "type": "string",
            "description": "2-3 sentence read on FX conditions right now.",
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair": {"type": "string", "description": "OANDA instrument, e.g. EUR_USD"},
                    "action": {"type": "string", "enum": ["long", "short", "close"]},
                    "confidence": {"type": "number"},
                    "stop_pips": {"type": "number", "description": "protective stop distance in pips"},
                    "target_pips": {"type": "number", "description": "take-profit distance in pips"},
                    "rationale": {"type": "string"},
                },
                "required": ["pair", "action", "confidence", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["market_outlook", "decisions"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are a disciplined intraday FX day-trader for a small, real-money account. "
    "You analyze 15-minute OHLCV candles and technical indicators for a set of major "
    "currency pairs plus the account's current open positions.\n\n"
    "Rules you operate under:\n"
    "- Actions: 'long' opens a long, 'short' opens a short, 'close' flattens a pair "
    "you currently hold (thesis broken, target/again reached, or better to be flat). "
    "'close' may ONLY name a pair currently held.\n"
    "- Propose at most 3 new entries per cycle, and only when the setup is genuinely "
    "clean. Most cycles the right answer is 0 or 1 new entries. An empty decisions "
    "list is a perfectly good output.\n"
    "- Every entry automatically gets a server-side stop-loss and take-profit. Give "
    "stop_pips and target_pips that fit the pair's current volatility (use the ATR "
    "provided) and keep reward:risk at roughly 2:1. If you omit them, defaults apply.\n"
    "- confidence is 0.0-1.0. Proposals under "
    f"{config.MIN_CONFIDENCE} are discarded by the risk engine, so don't pad the list "
    "with low-conviction ideas.\n"
    "- Mind the spread shown for each pair: skip setups where the spread is wide "
    "relative to your intended stop.\n"
    "- Never average down. Never propose a new position on a pair you already hold.\n"
    "- Capital preservation beats catching every move. When conditions are choppy or "
    "unclear, say so in market_outlook and propose nothing."
)


def _indicators(candles: list[dict]) -> dict:
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    if len(closes) < 21:
        return {}
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None

    gains, losses = [], []
    for i in range(-14, 0):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain, avg_loss = sum(gains) / 14, sum(losses) / 14
    rsi = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    # ATR(14) in pips: average true range over the last 14 candles.
    trs = []
    for i in range(-14, 0):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    atr = sum(trs) / 14

    last = closes[-1]
    dec = 3 if sma50 is None else 5  # display precision handled by pair below
    return {
        "last": round(last, 5),
        "chg_5c_pips": round((last - closes[-6]), 5),
        "chg_20c_pips": round((last - closes[-21]), 5),
        "sma20": round(sma20, 5),
        "above_sma20": last > sma20,
        "sma50": round(sma50, 5) if sma50 else None,
        "above_sma50": (last > sma50) if sma50 else None,
        "rsi14": round(rsi, 1),
        "atr14": round(atr, 5),
    }


def _to_pips(instrument: str, price_delta) -> float | None:
    if price_delta is None:
        return None
    return round(price_delta / config.pip_size(instrument), 1)


def build_briefing(account: dict, positions: list[dict],
                   candles_by_pair: dict, prices: dict) -> str:
    held = {p["instrument"]: p for p in positions}
    pairs = {}
    for pair, candles in candles_by_pair.items():
        if not candles:
            continue
        ind = _indicators(candles)
        px = prices.get(pair, {})
        spread_pips = None
        if px.get("ask") is not None and px.get("bid") is not None:
            spread_pips = _to_pips(pair, px["ask"] - px["bid"])
        pos = held.get(pair)
        pairs[pair] = {
            "bid": px.get("bid"),
            "ask": px.get("ask"),
            "spread_pips": spread_pips,
            "atr14_pips": _to_pips(pair, ind.get("atr14")),
            "rsi14": ind.get("rsi14"),
            "above_sma20": ind.get("above_sma20"),
            "above_sma50": ind.get("above_sma50"),
            "move_20c_pips": _to_pips(pair, ind.get("chg_20c_pips")),
            "held": (
                {"side": pos["side"], "units": pos["units"],
                 "unrealized_pl": round(pos["unrealized_pl"], 2)}
                if pos else None
            ),
        }
    # Held pairs whose candles failed to load are still surfaced (with null
    # indicators) so the model can always choose to close a position it holds.
    for inst, pos in held.items():
        if inst in pairs:
            continue
        px = prices.get(inst, {})
        spread_pips = None
        if px.get("ask") is not None and px.get("bid") is not None:
            spread_pips = _to_pips(inst, px["ask"] - px["bid"])
        pairs[inst] = {
            "bid": px.get("bid"),
            "ask": px.get("ask"),
            "spread_pips": spread_pips,
            "atr14_pips": None,
            "rsi14": None,
            "above_sma20": None,
            "above_sma50": None,
            "move_20c_pips": None,
            "indicators_unavailable": True,
            "held": {"side": pos["side"], "units": pos["units"],
                     "unrealized_pl": round(pos["unrealized_pl"], 2)},
        }

    brief = {
        "account": {
            "equity": round(account["equity"], 2),
            "currency": account["currency"],
            "open_positions": len(positions),
            "max_positions": config.MAX_OPEN_POSITIONS,
            "risk_per_trade_pct": config.RISK_PER_TRADE_PCT * 100,
        },
        "timeframe": config.CANDLE_GRANULARITY,
        "pairs": pairs,
    }
    return json.dumps(brief, indent=1)


def analyze(account: dict, positions: list[dict],
            candles_by_pair: dict, prices: dict) -> dict:
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    briefing = build_briefing(account, positions, candles_by_pair, prices)

    response = client.beta.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-06-01"],
        fallbacks=[{"model": config.FALLBACK_MODEL}],
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": PICKS_SCHEMA},
        },
        system=SYSTEM,
        messages=[
            {
                "role": "user",
                "content": "Here is the current account state and per-pair intraday data. "
                "Give me this cycle's decisions.\n\n" + briefing,
            }
        ],
    )

    if response.stop_reason == "refusal":
        return {
            "market_outlook": "Analysis unavailable: the model declined the request (safety classifier). No trades this cycle.",
            "decisions": [],
            "model": response.model,
        }
    if response.stop_reason == "max_tokens":
        return {
            "market_outlook": "Analysis unavailable: output truncated at the token limit. No trades this cycle.",
            "decisions": [],
            "model": response.model,
        }

    text = next(b.text for b in response.content if b.type == "text")
    result = json.loads(text)
    result["model"] = response.model
    return result
