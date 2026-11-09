"""All tunable settings. Values come from environment variables / .env, with defaults below."""
import os
from pathlib import Path


def _load_dotenv(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0].split("\t#", 1)[0]       # drop inline comments
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(os.environ.get("BOT_ENV_FILE", Path(__file__).with_name(".env")))


def _get(name, default, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    if cast is bool:
        return v.lower() in ("1", "true", "yes", "on")
    return cast(v)


class Config:
    # --- account / mode -----------------------------------------------------
    API_KEY = _get("DELTA_API_KEY", "")
    API_SECRET = _get("DELTA_API_SECRET", "")
    DELTA_ENV = _get("DELTA_ENV", "testnet")          # testnet | prod
    MODE = _get("MODE", "paper")                       # paper | live
    UNDERLYING = _get("UNDERLYING", "BTC")
    PERP_SYMBOL = _get("PERP_SYMBOL", "BTCUSD")
    ORDER_SIZE = _get("ORDER_SIZE", 1, int)            # contracts per trade

    # --- direction score D ---------------------------------------------------
    D_THRESHOLD = _get("D_THRESHOLD", 0.30, float)
    W_TREND = _get("W_TREND", 0.5, float)
    W_MOMENTUM = _get("W_MOMENTUM", 0.3, float)
    W_STRETCH = _get("W_STRETCH", 0.2, float)
    RSI_LOW = _get("RSI_LOW", 40, float)               # sell put when 5m RSI <= this
    RSI_HIGH = _get("RSI_HIGH", 60, float)             # sell call when 5m RSI >= this

    # --- sideways days: short strangle / iron condor -------------------------
    STRANGLE = _get("STRANGLE", True, bool)            # trade both sides when D is sideways
    STRANGLE_DELTA = _get("STRANGLE_DELTA", 0.20, float)
    STRANGLE_MIN_DELTA = _get("STRANGLE_MIN_DELTA", 0.12, float)
    STRANGLE_MAX_DELTA = _get("STRANGLE_MAX_DELTA", 0.28, float)
    STRANGLE_RSI_LOW = _get("STRANGLE_RSI_LOW", 40, float)   # enter only while 5m RSI is calm
    STRANGLE_RSI_HIGH = _get("STRANGLE_RSI_HIGH", 60, float)
    STRANGLE_EXIT_D = _get("STRANGLE_EXIT_D", 0.45, float)   # a trend has started: get out
    STRANGLE_BACKSTOP_PCT = _get("STRANGLE_BACKSTOP_PCT", 1.0, float)  # naked: per-leg stop if a leg doubles

    # --- option selection ----------------------------------------------------
    TARGET_DELTA = _get("TARGET_DELTA", 0.30, float)
    MIN_DELTA = _get("MIN_DELTA", 0.25, float)
    MAX_DELTA = _get("MAX_DELTA", 0.35, float)
    ENTRY_WORK_SECONDS = _get("ENTRY_WORK_SECONDS", 30, int)  # entry: rest a limit order up to this long per leg
    ENTRY_STEP_SECONDS = _get("ENTRY_STEP_SECONDS", 5, int)   # ... re-pricing it this often
    ENTRY_SLIPPAGE_PCT = _get("ENTRY_SLIPPAGE_PCT", 0.03, float)  # entry: up to 3% worse over 3 tries
    MAX_SPREAD_PCT = _get("MAX_SPREAD_PCT", 0.06, float)
    MIN_PREMIUM = _get("MIN_PREMIUM", 50.0, float)     # USD per 1 BTC
    MIN_HOURS_TO_EXPIRY = _get("MIN_HOURS_TO_EXPIRY", 5.0, float)  # nearest expiry with >= this many hours left
    EXPIRY_HOUR_IST = 17
    EXPIRY_MIN_IST = 30

    # --- fees (Delta India options; verify against your fills) ---------------
    FEE_RATE_NOTIONAL = _get("FEE_RATE_NOTIONAL", 0.0001, float)  # 0.03% of notional
    FEE_CAP_PREMIUM = _get("FEE_CAP_PREMIUM", 0.035, float)       # capped at 3.5% of premium
    GST = _get("GST", 0.18, float)
    MAX_TP_GROSS_PCT = _get("MAX_TP_GROSS_PCT", 0.25, float)  # skip if fees push needed decay above this

    # --- hedge (credit spread) -----------------------------------------------
    HEDGE = _get("HEDGE", "off")                       # off | on | compare (compare = paper only)
    HEDGE_DELTA = _get("HEDGE_DELTA", 0.08, float)     # |delta| of the bought wing
    HEDGE_MAX_DELTA = _get("HEDGE_MAX_DELTA", 0.25, float)
    HEDGE_MAX_WIDTH = _get("HEDGE_MAX_WIDTH", 1500, float)     # protective strike at most this far away
    MAX_LOSS_PER_TRADE_USD = _get("MAX_LOSS_PER_TRADE_USD", 40.0, float)  # lots sized so a gap loses <= this
    HEDGE_MIN_CREDIT_SHARE = _get("HEDGE_MIN_CREDIT_SHARE", 0.5, float)  # credit >= 50% of sold premium
    MAX_OPEN_PER_BOOK = _get("MAX_OPEN_PER_BOOK", 6, int)

    # --- exits ---------------------------------------------------------------
    TP_NET_PCT = _get("TP_NET_PCT", 0.03, float)       # 3% of premium AFTER fees
    SL_MULT = _get("SL_MULT", 1.5, float)               # stop when closing cost >= 1.5x the credit
    STOP_POTM = _get("STOP_POTM", 0.55, float)         # or when BTC nears a sold strike: P(worthless) < 55%
    MAX_ABORTS_PER_DAY = _get("MAX_ABORTS_PER_DAY", 2, int)  # pause after entries that half-filled
    BACKSTOP_PCT = _get("BACKSTOP_PCT", 0.25, float)   # exchange-side stop if bot dies
    MAX_HOLD_MIN = _get("MAX_HOLD_MIN", 240, int)
    THETA_SHARE = _get("THETA_SHARE", 0.5, float)      # decay must deliver this share of the TP in MAX_HOLD
    EXIT_BEFORE_EXPIRY_MIN = _get("EXIT_BEFORE_EXPIRY_MIN", 60, int)
    EXIT_ON_D_FLIP = _get("EXIT_ON_D_FLIP", True, bool)

    # --- runner: hold a strong winner to expiry instead of taking 3% ----------
    RUNNER = _get("RUNNER", True, bool)
    RUNNER_MIN_D = _get("RUNNER_MIN_D", 0.40, float)          # D must still agree this strongly
    RUNNER_MIN_POTM = _get("RUNNER_MIN_POTM", 0.75, float)    # P(option expires worthless)
    RUNNER_MAX_DELTA = _get("RUNNER_MAX_DELTA", 0.25, float)  # sold option's delta has shrunk
    RUNNER_TRAIL = _get("RUNNER_TRAIL", 1.0, float)           # exit if premium rebounds 100% off its low
    RUNNER_EXIT_Z = _get("RUNNER_EXIT_Z", 0.3, float)         # exit if P(worthless) falls below ~62%
    RUNNER_FLIP_D = _get("RUNNER_FLIP_D", 0.10, float)        # exit if D crosses this the wrong way
    RUNNER_SETTLE_POTM = _get("RUNNER_SETTLE_POTM", 0.97, float)  # needed to hold through settlement

    # --- daily limits --------------------------------------------------------
    MAX_TRADES_PER_DAY = _get("MAX_TRADES_PER_DAY", 1000, int)   # effectively no count limit
    MAX_LOSSES_PER_DAY = _get("MAX_LOSSES_PER_DAY", 1000, int)
    MAX_OPEN_RISK_USD = _get("MAX_OPEN_RISK_USD", 120.0, float)   # worst case of ALL open trades combined
    MAX_MARGIN_USE_PCT = _get("MAX_MARGIN_USE_PCT", 0.50, float)  # no new trades above this margin use
    MAX_DAILY_LOSS_USD = _get("MAX_DAILY_LOSS_USD", 20.0, float)
    COOLDOWN_MIN = _get("COOLDOWN_MIN", 5, int)
    TRADE_HOURS_IST = _get("TRADE_HOURS_IST", "00:00-23:59")

    # --- loop / files --------------------------------------------------------
    SCAN_SECONDS = _get("SCAN_SECONDS", 60, int)
    MANAGE_SECONDS = _get("MANAGE_SECONDS", 15, int)
    DATA_DIR = _get("DATA_DIR", str(Path(__file__).with_name("data")))

    # --- optional Telegram alerts --------------------------------------------
    TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = _get("TELEGRAM_CHAT_ID", "")

    @classmethod
    def variants(cls):
        return {"off": ["naked"], "on": ["spread"], "compare": ["naked", "spread"]}[cls.HEDGE]

    @classmethod
    def validate(cls):
        errs = []
        if cls.MODE not in ("paper", "live"):
            errs.append("MODE must be paper or live")
        if cls.DELTA_ENV not in ("testnet", "prod"):
            errs.append("DELTA_ENV must be testnet or prod")
        if cls.MODE == "live" and not (cls.API_KEY and cls.API_SECRET):
            errs.append("live mode needs DELTA_API_KEY and DELTA_API_SECRET in .env")
        if cls.HEDGE not in ("off", "on", "compare"):
            errs.append("HEDGE must be off, on or compare")
        if cls.HEDGE == "compare" and cls.MODE != "paper":
            errs.append("HEDGE=compare only works with MODE=paper")
        if not cls.STRANGLE_MIN_DELTA <= cls.STRANGLE_DELTA <= cls.STRANGLE_MAX_DELTA:
            errs.append("STRANGLE_DELTA must sit between STRANGLE_MIN_DELTA and STRANGLE_MAX_DELTA")
        if not cls.MIN_DELTA <= cls.TARGET_DELTA <= cls.MAX_DELTA:
            errs.append("TARGET_DELTA must sit between MIN_DELTA and MAX_DELTA")
        if errs:
            raise SystemExit("Config error: " + "; ".join(errs))
