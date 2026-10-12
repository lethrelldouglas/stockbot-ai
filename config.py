"""Central config for the ResolvOps ForexBot (OANDA v20, real-money capable).

Everything safety-related lives here. The bot refuses to trade LIVE unless you
explicitly flip TWO switches (see LIVE TRADING below). Until then it runs
against OANDA's practice (demo) environment: real market feed, fake money.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------- .env ----
def _load_env():
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

_load_env()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OANDA_API_TOKEN   = os.environ.get("OANDA_API_TOKEN", "")
OANDA_ACCOUNT_ID  = os.environ.get("OANDA_ACCOUNT_ID", "")

# ------------------------------------------------------- LIVE TRADING -----
# PRACTICE = OANDA demo money on the real market feed. This is the default and
# the only mode that runs until you deliberately flip BOTH switches in .env:
#   OANDA_ENV=live   AND   I_UNDERSTAND_REAL_MONEY=YES
# Forex is leveraged. Nothing here removes the risk of real losses.
LIVE = os.environ.get("OANDA_ENV", "practice").lower() == "live"
LIVE_CONFIRMED = os.environ.get("I_UNDERSTAND_REAL_MONEY", "") == "YES"

# OANDA v20 REST hosts (chosen automatically from the mode above).
OANDA_HOST = "https://api-fxtrade.oanda.com" if LIVE else "https://api-fxpractice.oanda.com"

# ------------------------------------------------------------ AI brain ----
# Fable 5 = Anthropic's deepest-reasoning model. If it ever declines, analyst.py
# falls back to FALLBACK_MODEL so a cycle never dies mid-run.
CLAUDE_MODEL   = "claude-fable-5"
FALLBACK_MODEL = "claude-opus-4-8"

# ------------------------------------------------------------ Universe ----
# The currency pairs the bot may analyze and trade. Majors only by default:
# deepest liquidity, tightest spreads, and every pair here contains USD so the
# risk sizing math (below) stays exact for a USD account. Add crosses at your
# own risk -- wider spreads, and sizing needs a conversion rate.
PAIRS = [
    "EUR_USD", "USD_JPY", "GBP_USD", "USD_CHF",
    "AUD_USD", "USD_CAD", "NZD_USD",
]
ACCOUNT_CCY = "CAD"           # your OANDA account's home currency (must match OANDA)
CANDLE_GRANULARITY = "M15"    # intraday timeframe fed to the analyst (15-min)
CANDLE_COUNT = 80             # how many candles of history per pair

# ---------------------------------------------------------- Risk rules ----
# HARD limits enforced in code (risk.py), regardless of what the AI proposes.
# The analyst proposes; the risk engine disposes.
# Risk per trade (entry -> stop) as a fraction of equity. Overridable via .env
# (RISK_PER_TRADE_PCT=0.02 etc.). 1-2% is professional range; treat 3% as a
# ceiling earned with a clean journal, never a starting point.
RISK_PER_TRADE_PCT = float(os.environ.get("RISK_PER_TRADE_PCT", "0.01"))
MAX_OPEN_POSITIONS   = 3      # never hold more than this many pairs at once
MAX_LEVERAGE         = 10     # cap total open notional at 10x equity
STOP_LOSS_PIPS       = 20     # default protective stop if analyst gives none
TAKE_PROFIT_PIPS     = 40     # default target (2:1 reward:risk)
MIN_STOP_PIPS        = 6      # floor: never size off a stop tighter than this
                              # (a sub-spread stop => max-leverage instant stopout)
MAX_STOP_PIPS        = 150    # ceiling: reject absurd stops before sizing
DAILY_LOSS_LIMIT_PCT = 0.03   # equity down 3% from day start -> halt for the day
MIN_CONFIDENCE       = 0.6    # ignore analyst picks below this confidence
MIN_TRADE_UNITS      = 1      # OANDA minimum order size

# ------------------------------------------------------------- Schedule ---
# Forex trades ~24h from Sunday 5pm ET to Friday 5pm ET. The dashboard's
# scheduler runs one analysis cycle every POLL_MINUTES while the FX market is
# open (OANDA reports tradability per instrument).
POLL_MINUTES = 30

# ------------------------------------------------- ORB (opening range) ----
# Opening Range Breakout on an index CFD. Mark the first ORB_RANGE_MINUTES after
# the US cash open (9:30 ET), then take the first candle that CLOSES beyond the
# range -- long above the high, short below the low. Stop rides the opposite
# side of the range; target is ORB_TARGET_R x that risk. Sizing + brackets go
# through the same risk engine and OANDA order path as the forex side.
# Any OANDA instrument works: an index CFD (NAS100_USD) or a forex pair
# (EUR_USD). FX units are tiny, so ORB on a major pair sizes correctly on
# accounts as small as a few hundred dollars. Override per-run via .env.
ORB_INSTRUMENT = os.environ.get("ORB_INSTRUMENT", "NAS100_USD")
ORB_GRANULARITY     = "M5"           # candle size for both the range and breakouts
ORB_RANGE_MINUTES   = 15             # opening range = first N minutes after the open
ORB_OPEN_ET         = (9, 30)        # US equity cash open (fuel for the range)
ORB_ENTRY_CUTOFF_ET = (11, 0)        # no NEW breakout entries after this time
ORB_FLATTEN_ET      = (15, 55)       # flatten any ORB trade before the 16:00 close
ORB_MIN_STOP_PTS    = 8              # floor: never size off a tinier range (over-lever)
ORB_MAX_STOP_PTS    = 120            # ceiling: clamp an absurdly wide range
ORB_TARGET_R        = 2.0            # take-profit at 2x the stop distance
ORB_VOL_FILTER      = True           # require breakout candle volume > range average
ORB_MAX_ENTRIES     = 1              # trades to take per day (1 = first clean break only)
ORB_CONFIDENCE      = 0.7            # fixed confidence handed to the risk engine
ORB_POLL_SECONDS    = 20             # how often the standalone loop checks for a break

# Shadow (dry-run) mode: compute the real range/breakout/size/stop/target against
# the live feed and LOG the trade it would take -- but never send an order.
# Default ON. Set ORB_DRY_RUN=false in .env to go live (the two live-money
# switches above still apply on top).
ORB_DRY_RUN = os.environ.get("ORB_DRY_RUN", "true").lower() != "false"
ORB_SHADOW_EQUITY = float(os.environ.get("ORB_SHADOW_EQUITY", "5000"))  # sizing basis when account is empty

# ----------------------------------------------------------- Files/state --
KILL_SWITCH_FILE = BASE_DIR / "KILL_SWITCH"      # exists -> bot does nothing
STATE_FILE = BASE_DIR / "state.json"             # day-start equity, last run
ORB_STATE_FILE = BASE_DIR / "orb_state.json"     # per-day ORB range + entries taken
ANALYSIS_FILE = BASE_DIR / "last_analysis.json"  # latest Claude analysis
TRADE_LOG = BASE_DIR / "trades_log.csv"
LOG_FILE = BASE_DIR / "bot.log"

DASHBOARD_PORT = 5250


# Index CFDs quote in points, not forex pips. For these, "one pip" = one index
# point (1.0), and OANDA prices them to a single decimal. Keeping them here lets
# the same pip_size()/price_decimals()/risk-sizing/order code serve both worlds.
# One unit of an *_USD index CFD makes 1 USD per 1.0 point, so treating a point
# as the pip makes risk.position_size() exact (loss_per_unit = points x rate).
INDEX_INSTRUMENTS = {"NAS100_USD", "US30_USD", "SPX500_USD"}


def pip_size(instrument: str) -> float:
    """Price value of one pip. Index CFDs = 1 point; JPY pairs 0.01; else 0.0001."""
    if instrument in INDEX_INSTRUMENTS:
        return 1.0
    return 0.01 if instrument.endswith("_JPY") else 0.0001


def price_decimals(instrument: str) -> int:
    """Decimal places OANDA expects for a price on this instrument."""
    if instrument in INDEX_INSTRUMENTS:
        return 1
    return 3 if instrument.endswith("_JPY") else 5


def ready() -> dict:
    """Return which credentials are present (used by the dashboard setup screen)."""
    return {
        "anthropic": bool(ANTHROPIC_API_KEY),
        "oanda": bool(OANDA_API_TOKEN and OANDA_ACCOUNT_ID),
        "live": LIVE,
    }


def assert_safe():
    if LIVE and not LIVE_CONFIRMED:
        raise SystemExit(
            "REFUSING TO RUN LIVE: OANDA_ENV=live but I_UNDERSTAND_REAL_MONEY "
            "is not set to YES. Real money would be at risk. Prove the bot on "
            "the practice environment first, then flip both switches."
        )
