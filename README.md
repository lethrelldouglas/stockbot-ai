# StockBot — AI-Assisted Forex Trading Bot

An automated forex/index trading bot that pairs an **LLM analyst** with a **deterministic, hard-coded risk engine**. The AI proposes trades; the risk engine disposes. No position is ever sized, opened, or left unprotected by anything other than rules written in plain Python — the model never has the final say over real money.

Built on the **OANDA v20** API (practice by default, real-money capable behind two explicit safety switches) with **Claude** as the analyst.

> ⚠️ **Educational project. Not financial advice.** Forex and CFDs are leveraged and can lose money fast. Run it on the practice (demo) environment. Nothing here removes the risk of real loss.

## Why it's built this way

The interesting problem isn't "can an LLM pick trades" — it's **"how do you let a probabilistic model touch money without trusting it?"** The answer here is a strict separation of concerns:

- **`analyst.py`** — sends market context (candles, spreads, open positions) to Claude and gets back *proposals*: direction, confidence, stop, target.
- **`risk.py`** — the gatekeeper. Enforces hard limits **regardless of what the AI proposes**: risk-per-trade %, max leverage, max open positions, min/max stop distance, a daily loss limit that halts trading, and a confidence floor. It computes exact position size from the stop distance so a tight stop can't quietly over-leverage the account.
- **`broker.py`** — the only code that talks to OANDA (prices, orders, brackets, positions).
- **`bot.py`** — orchestrates one analysis → risk-check → order cycle.

If the analyst and the rules disagree, **the rules win.**

## Features

- **Two strategies:** multi-pair forex analysis on 15-minute candles, and an **Opening Range Breakout (ORB)** on an index CFD (first clean break of the opening range, stop on the far side, 2R target).
- **Safety first:** runs in OANDA *practice* mode by default. Going live requires flipping **two** switches (`OANDA_ENV=live` **and** `I_UNDERSTAND_REAL_MONEY=YES`). A `KILL_SWITCH` file halts everything. ORB runs in shadow (dry-run) mode until explicitly enabled.
- **Hard risk controls:** 1% default risk per trade, 10x max leverage cap, 3% daily-loss halt, confidence threshold, stop-distance floor/ceiling.
- **Live dashboard** (`dashboard.py`) — a local web UI on port 5250 showing status, credentials check, and the latest analysis.
- **Trading journal** (`ORB_JOURNAL.md`) — scored in R, not dollars, because discipline is the real edge.

## Stack

Python · OANDA v20 REST API · Anthropic API (Claude) · a from-scratch risk & position-sizing engine

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in your OANDA + Anthropic keys
python dashboard.py           # opens the dashboard on http://localhost:5250
```

Credentials live only in `.env` (git-ignored — never committed). See `.env.example` for the template. The bot stays in practice mode until you deliberately opt into live trading.

## Safety switches (read before going live)

| Switch | Default | To go live |
|---|---|---|
| `OANDA_ENV` | `practice` | `live` |
| `I_UNDERSTAND_REAL_MONEY` | *(blank)* | `YES` |
| `ORB_DRY_RUN` | `true` | `false` |

All three must be changed for real-money ORB trades. The code refuses to run live with only one flipped.

---

Built by **Lethrell Douglas** — [lethrelldouglas.github.io](https://lethrelldouglas.github.io)
