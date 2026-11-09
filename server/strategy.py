"""Signal maths: direction score D, entry trigger, option/spread selection, runner test.

D = 0.5*tanh(T) + 0.3*M - 0.2*tanh(V)      (15-minute BTC perpetual candles)
  T = (EMA20 - EMA50) / ATR14               trend
  M = (RSI14 - 50) / 50                     momentum
  V = (Price - VWAP) / ATR14                overstretch (faded)

D > +threshold -> sell a put  (enter when 5-min RSI <= 40)
D < -threshold -> sell a call (enter when 5-min RSI >= 60)

Sideways (|D| <= threshold): sell BOTH a put and a call (~0.20 delta) when the 5-min RSI
is calm (40-60): a short strangle, or an iron condor when HEDGE adds wings.

Runner test (checked when a trade reaches its 3% target):
  z     = ln(S/K) / (sigma * sqrt(t))   for a short put   (ln(K/S)/... for a short call)
  P_otm = N(z)                          chance the sold option expires worthless
  Upgrade to "hold to expiry" when D still agrees strongly, P_otm is high and
  the option's delta has shrunk.
"""
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- indicators
def to_frame(candles):
    df = pd.DataFrame(candles)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df["time"] = df["time"].astype(int)
    return df.sort_values("time").reset_index(drop=True)


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    out = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    out = out.where(dn > 0, 100.0)                      # no losses -> 100
    return out.where((up > 0) | (dn > 0), 50.0)         # flat -> 50


def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
                   axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def session_vwap(df):
    """VWAP reset at 00:00 UTC (05:30 IST) each day."""
    day = df["time"] // 86400
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].where(df["volume"] > 0, 1e-9)
    return (tp * vol).groupby(day).cumsum() / vol.groupby(day).cumsum()


def realized_vol(candles_15m, bars=96):
    """Annualised volatility from the last `bars` 15-minute log returns."""
    c = to_frame(candles_15m)["close"].tail(bars + 1)
    r = np.log(c / c.shift()).dropna()
    return float(r.std() * math.sqrt(4 * 24 * 365)) if len(r) > 5 else 0.0


@dataclass
class Direction:
    D: float
    T: float
    M: float
    V: float
    price: float
    rvol: float = 0.0


def direction_score(candles_15m, w=(0.5, 0.3, 0.2)):
    df = to_frame(candles_15m)
    if len(df) < 60:
        raise ValueError(f"need >=60 candles for D, got {len(df)}")
    close = df["close"]
    a = atr(df).iloc[-1]
    if not a or math.isnan(a):
        raise ValueError("ATR unavailable")
    T = (ema(close, 20).iloc[-1] - ema(close, 50).iloc[-1]) / a
    M = (rsi(close).iloc[-1] - 50) / 50
    V = (close.iloc[-1] - session_vwap(df).iloc[-1]) / a
    D = w[0] * math.tanh(T) + w[1] * M - w[2] * math.tanh(V)
    return Direction(D=D, T=T, M=M, V=V, price=float(close.iloc[-1]),
                     rvol=realized_vol(candles_15m))


def last_rsi(candles, n=14):
    return float(rsi(to_frame(candles)["close"], n).iloc[-1])


def bias_from_D(D, threshold):
    if D > threshold:
        return "bull"   # sell put
    if D < -threshold:
        return "bear"   # sell call
    return None


def entry_triggered(bias, rsi5, rsi_low=40, rsi_high=60):
    """bull: buy-the-dip (RSI <= low). bear: sell-the-rip (RSI >= high).
    neutral: calm market, RSI inside [low, high]."""
    if bias == "neutral":
        return rsi_low <= rsi5 <= rsi_high
    return (bias == "bull" and rsi5 <= rsi_low) or (bias == "bear" and rsi5 >= rsi_high)


# --------------------------------------------------------------- probability
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def prob_otm(kind, spot, strike, sigma, hours_left):
    """(z, P) that a SOLD put/call finishes out of the money (driftless lognormal)."""
    t = max(hours_left, 1e-6) / (24 * 365)
    sd = max(sigma, 1e-6) * math.sqrt(t)
    z = math.log(spot / strike) / sd if kind == "put" else math.log(strike / spot) / sd
    return z, norm_cdf(z)


def prob_range(spot, put_strike, call_strike, sigma, hours_left):
    """(min z, P) that BTC settles between the two sold strikes (both legs expire worthless)."""
    zp, pp = prob_otm("put", spot, put_strike, sigma, hours_left)
    zc, pc = prob_otm("call", spot, call_strike, sigma, hours_left)
    return min(zp, zc), max(pp + pc - 1, 0.0)


# --------------------------------------------------------- option selection
def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


@dataclass
class Candidate:
    symbol: str
    product_id: int
    kind: str          # "put" / "call"
    strike: float
    delta: float
    theta_day: float
    bid: float
    ask: float
    mark: float
    iv: float = 0.0

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self):
        return (self.ask - self.bid) / self.mid if self.mid > 0 else 1.0

    @property
    def theta_hr_pct(self):
        return abs(self.theta_day) / 24 / self.mid if self.mid > 0 else 0.0


def parse_iv(q):
    vals = [_f(q.get("bid_iv")), _f(q.get("ask_iv"))]
    vals = [v for v in vals if 0 < v < 10]
    return sum(vals) / len(vals) if vals else 0.0


def parse_chain(tickers, expiry_suffix):
    """Keep only options whose symbol ends with the target expiry (ddmmyy)."""
    out = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith(expiry_suffix):
            continue
        ct = t.get("contract_type")
        kind = "put" if ct == "put_options" else "call" if ct == "call_options" else None
        if not kind:
            continue
        g = t.get("greeks") or {}
        q = t.get("quotes") or {}
        out.append(Candidate(
            symbol=sym, product_id=int(t["product_id"]), kind=kind,
            strike=_f(t.get("strike_price")), delta=_f(g.get("delta")),
            theta_day=_f(g.get("theta")), bid=_f(q.get("best_bid")),
            ask=_f(q.get("best_ask")), mark=_f(t.get("mark_price")), iv=parse_iv(q),
        ))
    return out


def select_short(cands, kind, cfg, target, lo, hi):
    """The option to sell: delta band + liquidity. Returns (candidate, reason)."""
    pool = [c for c in cands if c.kind == kind]
    if not pool:
        return None, f"no {kind}s in chain for target expiry"
    pool = [c for c in pool if lo <= abs(c.delta) <= hi]
    if not pool:
        return None, f"no {kind} with |delta| in [{lo}, {hi}]"
    pool = [c for c in pool if c.bid > 0 and c.ask > 0 and c.bid >= cfg.MIN_PREMIUM]
    if not pool:
        return None, f"no two-sided {kind} quotes above minimum premium"
    pool = [c for c in pool if c.spread_pct <= cfg.MAX_SPREAD_PCT]
    if not pool:
        return None, f"{kind} spreads wider than {cfg.MAX_SPREAD_PCT:.0%}"
    return min(pool, key=lambda c: abs(abs(c.delta) - target)), "ok"


def select_main(cands, bias, cfg):
    """Directional trade: one put (bull) or one call (bear)."""
    kind = "put" if bias == "bull" else "call"
    return select_short(cands, kind, cfg, cfg.TARGET_DELTA, cfg.MIN_DELTA, cfg.MAX_DELTA)


def select_strangle(cands, cfg):
    """Sideways trade: one put AND one call. Returns ([put, call], reason)."""
    out = []
    for kind in ("put", "call"):
        c, why = select_short(cands, kind, cfg, cfg.STRANGLE_DELTA,
                              cfg.STRANGLE_MIN_DELTA, cfg.STRANGLE_MAX_DELTA)
        if not c:
            return None, why
        out.append(c)
    return out, "ok"


def select_wing(cands, main, cfg):
    """The protective option to buy: same type, further out of the money, |delta| ~ HEDGE_DELTA."""
    further = (lambda c: c.strike < main.strike) if main.kind == "put" else \
              (lambda c: c.strike > main.strike)
    pool = [c for c in cands if c.kind == main.kind and further(c) and c.ask > 0
            and abs(c.delta) <= cfg.HEDGE_MAX_DELTA
            and abs(c.strike - main.strike) <= cfg.HEDGE_MAX_WIDTH]
    if not pool:
        return None, f"no wing within {cfg.HEDGE_MAX_WIDTH:.0f} points further out of the money"
    return min(pool, key=lambda c: abs(abs(c.delta) - cfg.HEDGE_DELTA)), "ok"


# ------------------------------------------------------------------- fees
def fee_pct_side(premium, spot, cfg):
    """Fee for one fill as a fraction of that option's price: min(rate*notional, cap*premium) + GST."""
    if premium <= 0:
        return 0.0
    return min(cfg.FEE_RATE_NOTIONAL * spot / premium, cfg.FEE_CAP_PREMIUM) * (1 + cfg.GST)


def fee_abs(price, spot, cfg):
    """Fee for one fill in price units (USD per 1 BTC of contract)."""
    return price * fee_pct_side(price, spot, cfg)


# ------------------------------------------------------------------ plans
@dataclass
class Plan:
    """A trade to open: one or two sold options, optionally one bought wing per sold option."""
    variant: str                 # "naked" / "spread"
    structure: str               # "vertical" (one side) / "strangle" (both sides)
    shorts: list
    wings: list = field(default_factory=list)
    spot: float = 0.0
    credit: float = 0.0          # premium received per 1 BTC
    fees_in: float = 0.0         # entry fees per 1 BTC
    decay_hr: float = 0.0        # net premium decay per hour per 1 BTC
    tp_gross: float = 0.0        # fraction of credit that must decay to bank TP_NET after fees
    max_loss: float = float("inf")

    @property
    def main(self):
        return self.shorts[0]

    @property
    def wing(self):
        return self.wings[0] if self.wings else None

    @property
    def name(self):
        if self.structure == "strangle":
            return "iron condor" if self.wings else "short strangle"
        return "credit spread" if self.wings else "naked " + self.shorts[0].kind

    def describe(self):
        txt = " + ".join(f"sell {c.symbol}" for c in self.shorts)
        if self.wings:
            txt += " / " + " + ".join(f"buy {c.symbol}" for c in self.wings)
        return txt


def build_legs_plan(variant, structure, shorts, wings, spot, cfg):
    p = Plan(variant=variant, structure=structure, shorts=list(shorts), wings=list(wings or []),
             spot=spot)
    p.credit = sum(c.bid for c in p.shorts) - sum(c.ask for c in p.wings)
    p.fees_in = sum(fee_abs(c.bid, spot, cfg) for c in p.shorts) + \
        sum(fee_abs(c.ask, spot, cfg) for c in p.wings)
    p.decay_hr = (sum(abs(c.theta_day) for c in p.shorts) -
                  sum(abs(c.theta_day) for c in p.wings)) / 24
    if p.wings:
        gaps = [abs(s.strike - w.strike) for s, w in zip(p.shorts, p.wings)]
        p.max_loss = max(gaps) - p.credit     # only one side of a condor can finish in the money
    if p.credit > 0:
        # exit fees assumed equal to entry fees for a scalp
        p.tp_gross = cfg.TP_NET_PCT + 2 * p.fees_in / p.credit
    return p


def build_plan(variant, main, wing, spot, cfg):
    """Directional (one-sided) plan: kept for the original call style."""
    return build_legs_plan(variant, "vertical", [main], [wing] if wing else [], spot, cfg)


def gate_plan(p, cfg, hold_hours):
    """Return (ok, reason) for opening this plan as a scalp."""
    if p.credit <= 0:
        return False, "no net credit"
    sold = sum(c.bid for c in p.shorts)
    if p.wings and p.credit < cfg.HEDGE_MIN_CREDIT_SHARE * sold:
        return False, f"wings cost too much (credit only {p.credit / sold:.0%} of premium sold)"
    if p.tp_gross > cfg.MAX_TP_GROSS_PCT:
        return False, (f"fees too heavy: needs {p.tp_gross:.1%} decay for "
                       f"{cfg.TP_NET_PCT:.0%} net (max {cfg.MAX_TP_GROSS_PCT:.0%})")
    need = cfg.THETA_SHARE * p.tp_gross / max(hold_hours, 1e-6)
    have = p.decay_hr / p.credit
    if have < need:
        return False, f"decay gate: {have:.2%}/hr of credit, needs {need:.2%}/hr"
    return True, "ok"


# ----------------------------------------------------------------- runner
def runner_check(bias, D, kind, spot, strike, sigma, hours_left, delta_now, cfg):
    """Should a winning scalp be held to expiry? Returns (ok, z, p_otm, reason)."""
    z, p = prob_otm(kind, spot, strike, sigma, hours_left)
    agrees = (bias == "bull" and D >= cfg.RUNNER_MIN_D) or (bias == "bear" and D <= -cfg.RUNNER_MIN_D)
    if not agrees:
        return False, z, p, f"D={D:+.2f} not strong enough (need {cfg.RUNNER_MIN_D})"
    if p < cfg.RUNNER_MIN_POTM:
        return False, z, p, f"P(expires worthless)={p:.0%} < {cfg.RUNNER_MIN_POTM:.0%}"
    if abs(delta_now) > cfg.RUNNER_MAX_DELTA:
        return False, z, p, f"|delta|={abs(delta_now):.2f} > {cfg.RUNNER_MAX_DELTA}"
    return True, z, p, "ok"


def runner_check_strangle(D, spot, put_strike, call_strike, sigma, hours_left, max_abs_delta, cfg):
    """Hold a winning strangle/condor to expiry? Returns (ok, z, p_range, reason)."""
    z, p = prob_range(spot, put_strike, call_strike, sigma, hours_left)
    if abs(D) >= cfg.D_THRESHOLD:
        return False, z, p, f"D={D:+.2f} is trending; a strangle wants a quiet market"
    if p < cfg.RUNNER_MIN_POTM:
        return False, z, p, f"P(both expire worthless)={p:.0%} < {cfg.RUNNER_MIN_POTM:.0%}"
    if max_abs_delta > cfg.RUNNER_MAX_DELTA:
        return False, z, p, f"a sold leg has |delta|={max_abs_delta:.2f} > {cfg.RUNNER_MAX_DELTA}"
    return True, z, p, "ok"
