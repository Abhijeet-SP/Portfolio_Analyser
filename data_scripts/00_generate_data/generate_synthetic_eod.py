"""Synthetic end-of-day feed generator for the portfolio risk platform.

Produces three PostgreSQL-loadable CSVs from the three fixed input universes:

    data/01_nifty_500_ticker_universe.csv  (498 EQUITY)
    data/02_etf_bonds_ticker_universe.csv  (24 ETF / COMMODITY_ETF / BOND_ETF)
    data/03_portfolio_universe.csv         (50 portfolios)
                    |
                    v
    data/prices.csv        market feed, 522 instruments x every NSE session
    data/transactions.csv  append-only ledger, BUY / SELL / DIVIDEND
    data/holdings.csv      EOD snapshot, derived by replaying the ledger

instrument_id assignment mirrors the load order used by
etl/01_dimension_load/*: file 01 rows become 1..498, file 02 rows become
499..522, because `instruments.instrument_id` is a SERIAL filled in that order.

Risk-free rate is NOT a column in any of these files. Sharpe / Sortino are
computed downstream at the analytics stage using RISK_FREE_ANNUAL below
(7.0% p.a., converted to a daily rate as (1 + r) ** (1 / 252) - 1).

Usage:
    python etl/00_generate_data/generate_synthetic_eod.py
"""

from __future__ import annotations

import math
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

EQUITY_CSV = DATA_DIR / "01_nifty_500_ticker_universe.csv"
ETF_BOND_CSV = DATA_DIR / "02_etf_bonds_ticker_universe.csv"
PORTFOLIO_CSV = DATA_DIR / "03_portfolio_universe.csv"

PRICES_CSV = DATA_DIR / "prices.csv"
TRANSACTIONS_CSV = DATA_DIR / "transactions.csv"
HOLDINGS_CSV = DATA_DIR / "holdings.csv"

SEED = 20260803

START_DATE = date(2025, 1, 1)
END_DATE = date(2026, 12, 31)

TRADING_DAYS_PER_YEAR = 252

# External assumption. Not stored in any CSV; recorded here so the number the
# analytics layer uses for Sharpe / Sortino is traceable to one place.
RISK_FREE_ANNUAL = 0.07
RISK_FREE_DAILY = (1.0 + RISK_FREE_ANNUAL) ** (1.0 / TRADING_DAYS_PER_YEAR) - 1.0

BENCHMARK_TICKER = "NIFTYBEES.NS"

# NSE trading holidays that fall on a weekday. 2025 is the published list;
# 2026 is projected from the festival calendar (NSE had not published it when
# this generator was written) and is documented as an assumption.
NSE_HOLIDAYS = [
    # 2025 - published
    "2025-02-26",  # Mahashivratri
    "2025-03-14",  # Holi
    "2025-03-31",  # Id-Ul-Fitr
    "2025-04-10",  # Mahavir Jayanti
    "2025-04-14",  # Ambedkar Jayanti
    "2025-04-18",  # Good Friday
    "2025-05-01",  # Maharashtra Day
    "2025-08-15",  # Independence Day
    "2025-08-27",  # Ganesh Chaturthi
    "2025-10-02",  # Gandhi Jayanti / Dussehra
    "2025-10-21",  # Diwali Laxmi Pujan (normal session closed)
    "2025-10-22",  # Diwali Balipratipada
    "2025-11-05",  # Guru Nanak Jayanti
    "2025-12-25",  # Christmas
    # 2026 - projected
    "2026-01-26",  # Republic Day
    "2026-03-04",  # Holi
    "2026-03-20",  # Id-Ul-Fitr
    "2026-03-26",  # Ram Navami
    "2026-03-31",  # Mahavir Jayanti
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-27",  # Bakri Id
    "2026-09-14",  # Ganesh Chaturthi
    "2026-10-02",  # Gandhi Jayanti
    "2026-10-20",  # Dussehra
    "2026-11-10",  # Diwali Balipratipada
    "2026-11-24",  # Guru Nanak Jayanti
    "2026-12-25",  # Christmas
]

TXN_BUY = "BUY"
TXN_SELL = "SELL"
TXN_DIVIDEND = "DIVIDEND"

UNCLASSIFIED = "UNCLASSIFIED"


# --------------------------------------------------------------------------- #
# Trading calendar
# --------------------------------------------------------------------------- #

def build_trading_calendar() -> pd.DatetimeIndex:
    # Mon-Fri minus the NSE holiday list. Saturdays are never trading days in
    # the 2025-2026 window (no special sessions fall inside it).
    weekdays = pd.bdate_range(START_DATE, END_DATE)
    holidays = pd.DatetimeIndex(pd.to_datetime(NSE_HOLIDAYS))
    return weekdays.difference(holidays)


# --------------------------------------------------------------------------- #
# Instrument universe
# --------------------------------------------------------------------------- #

def load_universe() -> pd.DataFrame:
    equities = pd.read_csv(EQUITY_CSV)
    etf_bonds = pd.read_csv(ETF_BOND_CSV)

    universe = pd.concat([equities, etf_bonds], ignore_index=True)

    # instrument_id must match the SERIAL the dimension loaders will hand out.
    universe.insert(0, "instrument_id", np.arange(1, len(universe) + 1))

    universe["sector"] = universe["sector"].fillna(UNCLASSIFIED)
    return universe


def assign_size_tiers(universe: pd.DataFrame, rng: np.random.Generator) -> pd.Series:
    # No market cap in the source files, so a Nifty-500-shaped tier split is
    # drawn once under the fixed seed: 100 large, 150 mid, rest small.
    tiers = pd.Series("NONE", index=universe.index, dtype=object)

    equity_pos = np.flatnonzero((universe["asset_type"] == "EQUITY").to_numpy())
    shuffled = rng.permutation(equity_pos)

    tiers.iloc[shuffled[:100]] = "LARGE"
    tiers.iloc[shuffled[100:250]] = "MID"
    tiers.iloc[shuffled[250:]] = "SMALL"
    return tiers


# --------------------------------------------------------------------------- #
# Price dynamics
# --------------------------------------------------------------------------- #

# ticker -> (factor key, beta, idiosyncratic daily sigma)
# 'MARKET' trackers load on the broad factor only; a sector key loads on the
# broad factor plus that sector's factor.
ETF_FACTOR_MAP = {
    "NIFTYBEES.NS": ("MARKET", 1.00, 0.0020),
    "JUNIORBEES.NS": ("MARKET", 1.12, 0.0034),
    "MID150BEES.NS": ("MARKET", 1.18, 0.0038),
    "BANKBEES.NS": ("Financial Services", 1.05, 0.0028),
    "ITBEES.NS": ("Technology", 1.05, 0.0030),
    "PHARMABEES.NS": ("Healthcare", 1.00, 0.0028),
    "AUTOBEES.NS": ("Consumer Cyclical", 1.02, 0.0030),
    "INFRABEES.NS": ("Industrials", 1.00, 0.0028),
    "CONSUMBEES.NS": ("Consumer Defensive", 0.92, 0.0026),
    "PSUBNKBEES.NS": ("Financial Services", 1.35, 0.0052),
    "CPSEETF.NS": ("Energy", 1.10, 0.0044),
    "MON100.NS": ("USTECH", 1.00, 0.0034),
    "MAFANG.NS": ("USTECH", 1.25, 0.0048),
}

# ticker -> (gold loading, silver loading, tracking-error sigma)
COMMODITY_FACTOR_MAP = {
    "GOLDBEES.NS": (1.00, 0.00, 0.0010),
    "SETFGOLD.NS": (1.00, 0.00, 0.0011),
    "GOLDSHARE.NS": (0.99, 0.00, 0.0013),
    "SILVERBEES.NS": (0.00, 1.00, 0.0016),
}

# ticker -> (annual accrual drift, daily sigma) - vol scales with duration.
BOND_FACTOR_MAP = {
    "LIQUIDBEES.NS": (0.0665, 0.00008),
    "SDL26BEES.NS": (0.0705, 0.00060),
    "EBBETF0430.NS": (0.0715, 0.00150),
    "EBBETF0431.NS": (0.0720, 0.00180),
    "EBBETF0433.NS": (0.0725, 0.00220),
    "GILT5YBEES.NS": (0.0700, 0.00180),
    "LTGILTBEES.NS": (0.0710, 0.00300),
}

# Plausible NAVs at the start of 2025.
SEED_PRICES = {
    "NIFTYBEES.NS": 258.0,
    "JUNIORBEES.NS": 712.0,
    "MID150BEES.NS": 205.0,
    "BANKBEES.NS": 512.0,
    "ITBEES.NS": 43.0,
    "PHARMABEES.NS": 22.0,
    "AUTOBEES.NS": 246.0,
    "INFRABEES.NS": 92.0,
    "CONSUMBEES.NS": 113.0,
    "PSUBNKBEES.NS": 74.0,
    "CPSEETF.NS": 88.0,
    "MON100.NS": 178.0,
    "MAFANG.NS": 96.0,
    "GOLDBEES.NS": 64.0,
    "SILVERBEES.NS": 92.0,
    "SETFGOLD.NS": 68.0,
    "GOLDSHARE.NS": 66.0,
    "LIQUIDBEES.NS": 1000.0,
    "EBBETF0430.NS": 24.0,
    "EBBETF0431.NS": 11.5,
    "EBBETF0433.NS": 12.2,
    "LTGILTBEES.NS": 25.5,
    "GILT5YBEES.NS": 26.5,
    "SDL26BEES.NS": 12.4,
}


def ar1_series(
    n: int,
    phi: float,
    sigma: float,
    rng: np.random.Generator,
    demean: bool = False,
) -> np.ndarray:
    """Stationary AR(1); `sigma` is the long-run sd.

    `demean` strips the realised sample mean. Over ~493 draws the sample mean
    of a persistent series is itself large enough to swamp a deliberate drift,
    so any factor whose cumulative drift is a modelling choice (bond accrual,
    the gold trend, sector factors) is de-meaned and given its drift explicitly.
    """
    shocks = rng.normal(0.0, sigma * math.sqrt(1.0 - phi * phi), n)
    out = np.empty(n)
    out[0] = rng.normal(0.0, sigma)
    for t in range(1, n):
        out[t] = phi * out[t - 1] + shocks[t]
    if demean:
        out -= out.mean()
    return out


def build_returns(
    universe: pd.DataFrame,
    tiers: pd.Series,
    n_days: int,
    rng: np.random.Generator,
) -> np.ndarray:
    n_inst = len(universe)
    returns = np.zeros((n_inst, n_days))

    asset_type = universe["asset_type"].to_numpy()
    sectors = universe["sector"].to_numpy()
    tickers = universe["ticker"].to_numpy()

    # --- systematic factors -------------------------------------------------
    # Broad market: steady positive drift with two higher-vol regimes, one of
    # them a genuine drawdown stretch, so the series is not one stationary blob.
    market_noise = rng.normal(0.0, 0.0082, n_days)
    market_noise -= market_noise.mean()
    regime = np.ones(n_days)
    drawdown = slice(int(n_days * 0.28), int(n_days * 0.40))
    regime[drawdown] = 1.9
    regime[int(n_days * 0.62):int(n_days * 0.72)] = 1.5
    market = 0.00060 + market_noise * regime
    market[drawdown] -= 0.00130

    equity_sectors = sorted(
        {s for s, a in zip(sectors, asset_type) if a == "EQUITY"}
    )
    # Sector factors are pure dispersion: zero cumulative drift of their own,
    # so a sector's two-year path is driven by the market plus its own betas.
    sector_factor = {
        s: ar1_series(n_days, 0.06, rng.uniform(0.0040, 0.0070), rng, demean=True)
        for s in equity_sectors
    }

    us_tech_noise = rng.normal(0.0, 0.0105, n_days)
    us_tech = 0.00055 + (us_tech_noise - us_tech_noise.mean())

    # Gold: independent macro factor, trending (positive AR(1)) rather than
    # mean-reverting, so cumulative paths drift instead of oscillating. Tail
    # moves live in the factor, not in the individual trackers, so two gold
    # ETFs stay ~99% correlated the way real ones do.
    gold_core = ar1_series(n_days, 0.14, 0.0072, rng, demean=True)
    gold_shock = (rng.random(n_days) < 0.010) * rng.uniform(0.025, 0.045, n_days) * rng.choice(
        [-1.0, 1.0], n_days, p=[0.45, 0.55]
    )
    gold_core = gold_core + gold_shock - gold_shock.mean()
    gold = 0.00035 + gold_core

    silver_noise = rng.normal(0.0, 0.0090, n_days)
    silver = 0.00030 + 1.55 * gold_core + (silver_noise - silver_noise.mean())

    factor_lookup = {"MARKET": np.zeros(n_days), "USTECH": us_tech}
    factor_lookup.update(sector_factor)

    # --- per-instrument returns --------------------------------------------
    for i in range(n_inst):
        atype = asset_type[i]
        ticker = tickers[i]

        if atype == "EQUITY":
            tier = tiers.iloc[i]
            if tier == "LARGE":
                beta = float(np.clip(rng.normal(0.95, 0.22), 0.35, 1.75))
                idio = rng.uniform(0.0068, 0.0112)
                tail_p, tail_lo, tail_hi = 0.018, 0.040, 0.062
            elif tier == "MID":
                beta = float(np.clip(rng.normal(1.05, 0.30), 0.35, 2.00))
                idio = rng.uniform(0.0095, 0.0150)
                tail_p, tail_lo, tail_hi = 0.023, 0.045, 0.070
            else:
                beta = float(np.clip(rng.normal(1.10, 0.38), 0.30, 2.10))
                idio = rng.uniform(0.0118, 0.0182)
                tail_p, tail_lo, tail_hi = 0.027, 0.050, 0.078

            systematic = market + sector_factor[sectors[i]]
            path = beta * systematic + rng.normal(0.0, idio, n_days)

        elif atype == "ETF":
            key, beta, idio = ETF_FACTOR_MAP[ticker]
            systematic = market if key == "MARKET" else (
                us_tech if key == "USTECH" else market + factor_lookup[key]
            )
            path = beta * systematic + rng.normal(0.0, idio, n_days)
            tail_p, tail_lo, tail_hi = 0.004, 0.025, 0.045

        elif atype == "COMMODITY_ETF":
            w_gold, w_silver, te = COMMODITY_FACTOR_MAP[ticker]
            # Tracking error only; the macro shocks are already in the factor.
            path = w_gold * gold + w_silver * silver + rng.normal(0.0, te, n_days)
            tail_p, tail_lo, tail_hi = 0.0, 0.0, 0.0

        else:  # BOND_ETF
            # Accrual is the whole story here, so the noise is de-meaned and
            # the yield drift is applied exactly.
            drift_annual, sigma = BOND_FACTOR_MAP[ticker]
            drift = (1.0 + drift_annual) ** (1.0 / TRADING_DAYS_PER_YEAR) - 1.0
            path = drift + ar1_series(n_days, 0.10, sigma, rng, demean=True)
            tail_p, tail_lo, tail_hi = 0.0, 0.0, 0.0

        if tail_p > 0.0:
            hits = rng.random(n_days) < tail_p
            magnitude = rng.uniform(tail_lo, tail_hi, n_days)
            sign = rng.choice([-1.0, 1.0], n_days, p=[0.48, 0.52])
            path = path + hits * magnitude * sign

        returns[i] = path

    # NSE-style circuit bound, and a hard floor so prices can never hit zero.
    np.clip(returns, -0.19, 0.19, out=returns)
    returns[:, 0] = 0.0
    return returns


def seed_prices(universe: pd.DataFrame, tiers: pd.Series, rng: np.random.Generator) -> np.ndarray:
    prices = np.empty(len(universe))
    for i, (ticker, atype) in enumerate(
        zip(universe["ticker"], universe["asset_type"])
    ):
        if atype == "EQUITY":
            tier = tiers.iloc[i]
            centre = {"LARGE": 1400.0, "MID": 620.0, "SMALL": 260.0}[tier]
            prices[i] = float(
                np.clip(rng.lognormal(math.log(centre), 0.80), 22.0, 9500.0)
            )
        else:
            prices[i] = SEED_PRICES[ticker]
    return np.round(prices, 2)


def build_volumes(
    universe: pd.DataFrame,
    tiers: pd.Series,
    returns: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    n_inst, n_days = returns.shape

    baseline = np.empty(n_inst)
    for i, atype in enumerate(universe["asset_type"]):
        if atype == "EQUITY":
            tier = tiers.iloc[i]
            lo, hi = {
                "LARGE": (6.0e5, 1.2e7),
                "MID": (1.2e5, 2.2e6),
                "SMALL": (1.5e4, 4.5e5),
            }[tier]
        elif atype == "ETF":
            lo, hi = 2.0e5, 8.0e6
        elif atype == "COMMODITY_ETF":
            lo, hi = 3.0e5, 1.2e7
        else:
            lo, hi = 2.0e4, 9.0e5
        baseline[i] = rng.uniform(lo, hi)

    log_base = np.log(baseline)

    # Autocorrelated turnover around the instrument baseline.
    log_vol = np.empty((n_inst, n_days))
    log_vol[:, 0] = log_base + rng.normal(0.0, 0.30, n_inst)
    shocks = rng.normal(0.0, 0.26, (n_inst, n_days))
    for t in range(1, n_days):
        log_vol[:, t] = log_base + 0.80 * (log_vol[:, t - 1] - log_base) + shocks[:, t]

    volume = np.exp(log_vol)

    # Turnover spikes on tail-move days.
    spike = 1.0 + 1.5 * (np.abs(returns) > 0.045) + 0.6 * (np.abs(returns) > 0.030)
    volume = volume * spike

    return np.maximum(np.rint(volume), 1.0).astype(np.int64)


# --------------------------------------------------------------------------- #
# Portfolio mandates
# --------------------------------------------------------------------------- #

class Archetype:
    def __init__(
        self,
        name,
        n_positions,
        trades_per_week,
        build_rate,
        buy_bias,
        exit_prob,
    ):
        self.name = name
        self.n_positions = n_positions          # (min, max) target holdings
        self.trades_per_week = trades_per_week  # (min, max), inside 0-5
        self.build_rate = build_rate            # trades/week while ramping up
        self.buy_bias = buy_bias                # P(trade is a BUY) once built
        self.exit_prob = exit_prob              # P(a SELL closes the position)


ARCHETYPES = {
    "PASSIVE_ETF": Archetype("PASSIVE_ETF", (8, 15), (0, 1), 3, 0.72, 0.05),
    "DEBT": Archetype("DEBT", (7, 12), (0, 1), 3, 0.70, 0.06),
    "CONSERVATIVE_INCOME": Archetype("CONSERVATIVE_INCOME", (14, 24), (0, 2), 4, 0.66, 0.07),
    "DIVIDEND_EQUITY": Archetype("DIVIDEND_EQUITY", (18, 30), (0, 2), 4, 0.64, 0.08),
    "VALUE_EQUITY": Archetype("VALUE_EQUITY", (18, 30), (0, 2), 4, 0.64, 0.08),
    "GROWTH_MOMENTUM": Archetype("GROWTH_MOMENTUM", (12, 22), (2, 5), 4, 0.55, 0.22),
    "BALANCED_MULTI_ASSET": Archetype("BALANCED_MULTI_ASSET", (24, 35), (1, 3), 5, 0.62, 0.09),
    "LARGE_CAP_CORE": Archetype("LARGE_CAP_CORE", (20, 32), (0, 2), 5, 0.65, 0.07),
}

# Ordered: first matching keyword wins.
KEYWORD_RULES = [
    ("PASSIVE_ETF", ["etf", "index", "passive", "smart beta"]),
    ("DEBT", ["debt", "duration", "accrual", "fixed income", "capital preservation"]),
    ("CONSERVATIVE_INCOME", ["income", "retirement", "pension", "sunset", "golden years"]),
    ("DIVIDEND_EQUITY", ["dividend", "payout", "yield"]),
    ("VALUE_EQUITY", ["value", "contra", "margin of safety", "special situations"]),
    ("GROWTH_MOMENTUM", [
        "momentum", "tactical", "trend", "breakout", "rotation",
        "aggressive", "conviction", "emerging", "small cap",
    ]),
    ("BALANCED_MULTI_ASSET", [
        "balanced", "multi asset", "all weather", "strategic",
        "diversified", "wealth", "global",
    ]),
]


def classify_portfolio(name: str) -> str:
    lowered = re.sub(r"\s+", " ", name.lower())
    for archetype, keywords in KEYWORD_RULES:
        for keyword in keywords:
            if keyword in lowered:
                return archetype
    return "LARGE_CAP_CORE"


VALUE_SECTORS = {"Financial Services", "Basic Materials", "Energy"}


def build_candidate_pools(universe: pd.DataFrame, tiers: pd.Series, div_yield: np.ndarray):
    ids = universe["instrument_id"].to_numpy()
    atype = universe["asset_type"].to_numpy()
    sectors = universe["sector"].to_numpy()
    tier_arr = tiers.to_numpy()

    is_equity = atype == "EQUITY"
    equity_ids = ids[is_equity]

    large = ids[is_equity & (tier_arr == "LARGE")]
    mid = ids[is_equity & (tier_arr == "MID")]
    small = ids[is_equity & (tier_arr == "SMALL")]

    # Top-third dividend payers among large/mid caps.
    eq_yield = div_yield[is_equity]
    yield_cut = np.quantile(eq_yield[eq_yield > 0], 0.66)
    high_yield = equity_ids[
        (eq_yield >= yield_cut) & np.isin(equity_ids, np.concatenate([large, mid]))
    ]

    value_tilt = ids[is_equity & np.isin(sectors, list(VALUE_SECTORS))]

    return {
        "equity": equity_ids,
        "large": large,
        "mid": mid,
        "small": small,
        "high_yield": high_yield,
        "value": value_tilt,
        "etf": ids[atype == "ETF"],
        "commodity": ids[atype == "COMMODITY_ETF"],
        "bond": ids[atype == "BOND_ETF"],
    }


def pick(rng: np.random.Generator, pool: np.ndarray, k: int, taken: set) -> list:
    available = [int(x) for x in pool if int(x) not in taken]
    k = min(k, len(available))
    if k <= 0:
        return []
    chosen = rng.choice(available, size=k, replace=False)
    result = [int(x) for x in np.atleast_1d(chosen)]
    taken.update(result)
    return result


def select_positions(archetype: str, pools: dict, rng: np.random.Generator):
    """Return [(instrument_id, target_weight)] for one portfolio.

    Weights are drawn per sleeve then scaled to that sleeve's mandate budget,
    so an archetype's asset-class mix holds regardless of how many names it
    happens to draw.
    """
    spec = ARCHETYPES[archetype]
    n_target = int(rng.integers(spec.n_positions[0], spec.n_positions[1] + 1))
    taken: set = set()
    sleeves = []  # (ids, budget)

    if archetype == "PASSIVE_ETF":
        n_core = max(3, int(round(n_target * 0.75)))
        sleeves.append((pick(rng, pools["etf"], n_core, taken), 0.82))
        rest = n_target - n_core
        sleeves.append((pick(rng, pools["commodity"], max(0, rest // 2), taken), 0.08))
        sleeves.append((pick(rng, pools["bond"], max(0, rest - rest // 2), taken), 0.10))

    elif archetype == "DEBT":
        n_bond = max(4, int(round(n_target * 0.6)))
        bond_budget = float(rng.uniform(0.70, 0.90))
        sleeves.append((pick(rng, pools["bond"], n_bond, taken), bond_budget))
        sleeves.append(
            (pick(rng, pools["large"], n_target - n_bond, taken), 1.0 - bond_budget)
        )

    elif archetype == "CONSERVATIVE_INCOME":
        n_bond = max(3, int(round(n_target * 0.30)))
        bond_budget = float(rng.uniform(0.35, 0.50))
        sleeves.append((pick(rng, pools["bond"], n_bond, taken), bond_budget))
        sleeves.append(
            (pick(rng, pools["high_yield"], n_target - n_bond, taken), 1.0 - bond_budget)
        )

    elif archetype == "DIVIDEND_EQUITY":
        n_hy = int(round(n_target * 0.8))
        sleeves.append((pick(rng, pools["high_yield"], n_hy, taken), 0.80))
        sleeves.append((pick(rng, pools["mid"], n_target - n_hy, taken), 0.20))

    elif archetype == "VALUE_EQUITY":
        n_val = int(round(n_target * 0.65))
        sleeves.append((pick(rng, pools["value"], n_val, taken), 0.68))
        sleeves.append((pick(rng, pools["equity"], n_target - n_val, taken), 0.32))

    elif archetype == "GROWTH_MOMENTUM":
        n_small = int(round(n_target * 0.45))
        n_mid = int(round(n_target * 0.40))
        sleeves.append((pick(rng, pools["small"], n_small, taken), 0.44))
        sleeves.append((pick(rng, pools["mid"], n_mid, taken), 0.40))
        sleeves.append((pick(rng, pools["large"], n_target - n_small - n_mid, taken), 0.16))

    elif archetype == "BALANCED_MULTI_ASSET":
        n_bond = max(2, int(round(n_target * 0.18)))
        n_comm = max(1, int(round(n_target * 0.08)))
        n_etf = max(1, int(round(n_target * 0.10)))
        n_eq = n_target - n_bond - n_comm - n_etf
        eq_budget = float(rng.uniform(0.55, 0.66))
        bond_budget = float(rng.uniform(0.16, 0.24))
        comm_budget = float(rng.uniform(0.05, 0.11))
        etf_budget = max(0.03, 1.0 - eq_budget - bond_budget - comm_budget)
        n_large = int(round(n_eq * 0.6))
        sleeves.append((pick(rng, pools["large"], n_large, taken), eq_budget * 0.62))
        sleeves.append((pick(rng, pools["mid"], n_eq - n_large, taken), eq_budget * 0.38))
        sleeves.append((pick(rng, pools["bond"], n_bond, taken), bond_budget))
        sleeves.append((pick(rng, pools["commodity"], n_comm, taken), comm_budget))
        sleeves.append((pick(rng, pools["etf"], n_etf, taken), etf_budget))

    else:  # LARGE_CAP_CORE
        sleeves.append((pick(rng, pools["large"], n_target, taken), 1.0))

    positions = []
    live = [(ids, budget) for ids, budget in sleeves if ids]
    total_budget = sum(budget for _, budget in live)
    for ids, budget in live:
        raw = rng.lognormal(0.0, 0.45, len(ids))
        raw = raw / raw.sum()
        share = budget / total_budget
        for instrument_id, w in zip(ids, raw):
            positions.append((instrument_id, float(w * share)))

    rng.shuffle(positions)
    return positions


# --------------------------------------------------------------------------- #
# Transaction ledger
# --------------------------------------------------------------------------- #

def dividend_schedule(
    universe: pd.DataFrame,
    n_days: int,
    trading_days: pd.DatetimeIndex,
    rng: np.random.Generator,
):
    """Per-EQUITY annual yield plus the day indices it pays on.

    India-listed ETFs and bond funds here are accumulation-style, so only
    EQUITY instruments appear.
    """
    years = trading_days.year.to_numpy()
    year_slots = {y: np.flatnonzero(years == y) for y in np.unique(years)}

    annual_yield = np.zeros(len(universe))
    pay_days: dict[int, list[int]] = {}

    for i, (instrument_id, atype) in enumerate(
        zip(universe["instrument_id"], universe["asset_type"])
    ):
        if atype != "EQUITY":
            continue
        annual_yield[i] = float(rng.uniform(0.005, 0.030))
        n_pay = 1 if rng.random() < 0.60 else 2
        days = []
        for slots in year_slots.values():
            if n_pay == 1:
                # Final dividend: typically Jun-Aug after the AGM.
                lo, hi = int(len(slots) * 0.42), int(len(slots) * 0.66)
            else:
                lo, hi = int(len(slots) * 0.10), int(len(slots) * 0.95)
            window = slots[lo:hi] if hi > lo else slots
            days.extend(int(d) for d in rng.choice(window, size=n_pay, replace=False))
        pay_days[int(instrument_id)] = sorted(days)

    return annual_yield, pay_days


def generate_ledger(
    portfolios: pd.DataFrame,
    universe: pd.DataFrame,
    pools: dict,
    prices: np.ndarray,
    trading_days: pd.DatetimeIndex,
    annual_yield: np.ndarray,
    pay_days: dict,
    rng: np.random.Generator,
):
    n_days = len(trading_days)
    id_to_row = {
        int(i): r for r, i in enumerate(universe["instrument_id"].to_numpy())
    }
    asset_type_by_id = dict(
        zip(universe["instrument_id"].astype(int), universe["asset_type"])
    )
    yield_by_id = {
        int(i): float(y) for i, y in zip(universe["instrument_id"], annual_yield)
    }
    n_pay_by_id = {k: len(v) // 2 for k, v in pay_days.items()}  # payments per year

    # Trading-day indices grouped into ISO weeks, so the "0-5 trades per week"
    # cap is enforced on real calendar weeks.
    week_groups: dict = {}
    for idx, day in enumerate(trading_days):
        iso = day.isocalendar()
        week_groups.setdefault((iso[0], iso[1]), []).append(idx)
    ordered_weeks = list(week_groups.values())

    pay_day_sets = {k: set(v) for k, v in pay_days.items()}

    rows = []
    meta = {}

    for _, portfolio in portfolios.iterrows():
        portfolio_id = int(portfolio["portfolio_id"])
        archetype = classify_portfolio(portfolio["portfolio_name"])
        spec = ARCHETYPES[archetype]

        inception = pd.Timestamp(portfolio["inception_date"])
        start_idx = int(np.searchsorted(trading_days.values, inception.to_datetime64()))
        if start_idx >= n_days:
            continue

        targets = select_positions(archetype, pools, rng)
        capital = float(rng.uniform(5.0e7, 4.5e9))  # Rs 5 cr - Rs 450 cr

        pending = list(targets)
        target_weight = dict(targets)
        max_positions = len(targets)

        qty: dict[int, int] = {}
        fee_rate = float(rng.uniform(0.0005, 0.0050))

        def record(instrument_id, day_idx, txn_type, quantity, unit_price, fees, amount):
            rows.append(
                (
                    portfolio_id,
                    int(instrument_id),
                    trading_days[day_idx].date(),
                    txn_type,
                    int(quantity),
                    round(float(unit_price), 2),
                    round(float(fees), 2),
                    round(float(amount), 2),
                )
            )

        def execute_buy(instrument_id, day_idx, notional):
            # Executed inside a +/-1% band around that day's close. The band is
            # 0.95% so 2dp rounding on a low-priced instrument cannot push the
            # realised slippage past 1%.
            px_close = prices[id_to_row[instrument_id], day_idx]
            px = round(float(px_close * (1.0 + rng.uniform(-0.0095, 0.0095))), 2)
            quantity = int(notional // px)
            if quantity < 1:
                return
            gross = quantity * px
            fees = round(gross * fee_rate, 2)
            record(instrument_id, day_idx, TXN_BUY, quantity, px, fees, gross + fees)
            qty[instrument_id] = qty.get(instrument_id, 0) + quantity

        def execute_sell(instrument_id, day_idx, quantity):
            held = qty.get(instrument_id, 0)
            quantity = int(min(quantity, held))
            if quantity < 1:
                return
            px_close = prices[id_to_row[instrument_id], day_idx]
            px = round(float(px_close * (1.0 + rng.uniform(-0.0095, 0.0095))), 2)
            gross = quantity * px
            fees = round(gross * fee_rate, 2)
            record(instrument_id, day_idx, TXN_SELL, quantity, px, fees, gross - fees)
            qty[instrument_id] = held - quantity
            if qty[instrument_id] == 0:
                del qty[instrument_id]

        for week in ordered_weeks:
            active = [d for d in week if d >= start_idx]
            if not active:
                continue

            if pending:
                n_trades = min(spec.build_rate, len(active), 5)
            else:
                n_trades = int(
                    rng.integers(spec.trades_per_week[0], spec.trades_per_week[1] + 1)
                )
                n_trades = min(n_trades, len(active), 5)

            if n_trades > 0:
                days = sorted(
                    int(d) for d in rng.choice(active, size=n_trades, replace=False)
                )
            else:
                days = []

            for day_idx in days:
                if pending:
                    instrument_id, weight = pending.pop(0)
                    execute_buy(instrument_id, day_idx, capital * weight)
                    continue

                if not qty:
                    continue

                if rng.random() < spec.buy_bias:
                    if len(qty) < max_positions and rng.random() < 0.35:
                        candidates = [i for i, _ in targets if i not in qty]
                        if candidates:
                            instrument_id = int(rng.choice(candidates))
                            execute_buy(
                                instrument_id,
                                day_idx,
                                capital * target_weight[instrument_id],
                            )
                            continue
                    instrument_id = int(rng.choice(list(qty.keys())))
                    top_up = capital * target_weight.get(instrument_id, 0.02)
                    execute_buy(instrument_id, day_idx, top_up * rng.uniform(0.10, 0.35))
                else:
                    instrument_id = int(rng.choice(list(qty.keys())))
                    if rng.random() < spec.exit_prob:
                        execute_sell(instrument_id, day_idx, qty[instrument_id])
                    else:
                        trim = math.floor(qty[instrument_id] * rng.uniform(0.15, 0.55))
                        execute_sell(instrument_id, day_idx, trim)

            # Dividends settle on the record date, independent of the trade cap.
            for day_idx in active:
                for instrument_id, held in list(qty.items()):
                    if asset_type_by_id[instrument_id] != "EQUITY":
                        continue
                    if day_idx not in pay_day_sets.get(instrument_id, ()):
                        continue
                    n_pay = max(1, n_pay_by_id.get(instrument_id, 1))
                    px_close = float(prices[id_to_row[instrument_id], day_idx])
                    dps = yield_by_id[instrument_id] * px_close / n_pay
                    amount = round(dps * held, 2)
                    if amount <= 0.0:
                        continue
                    record(instrument_id, day_idx, TXN_DIVIDEND, 0, 0.0, 0.0, amount)

        meta[portfolio_id] = {
            "archetype": archetype,
            "start_idx": start_idx,
            "capital": capital,
        }

    ledger = pd.DataFrame(
        rows,
        columns=[
            "portfolio_id",
            "instrument_id",
            "txn_date",
            "txn_type",
            "quantity",
            "price",
            "fees",
            "amount",
        ],
    )
    ledger = ledger.sort_values(
        ["txn_date", "portfolio_id", "instrument_id", "txn_type"], kind="mergesort"
    ).reset_index(drop=True)
    ledger.insert(0, "transaction_id", np.arange(1, len(ledger) + 1))
    return ledger, meta


# --------------------------------------------------------------------------- #
# Holdings snapshots
# --------------------------------------------------------------------------- #

def generate_holdings(
    ledger: pd.DataFrame,
    meta: dict,
    universe: pd.DataFrame,
    prices: np.ndarray,
    trading_days: pd.DatetimeIndex,
) -> pd.DataFrame:
    n_days = len(trading_days)
    id_to_row = {
        int(i): r for r, i in enumerate(universe["instrument_id"].to_numpy())
    }
    day_index = {d.date(): i for i, d in enumerate(trading_days)}

    trades = ledger[ledger["txn_type"] != TXN_DIVIDEND].copy()
    trades["day_idx"] = trades["txn_date"].map(day_index)

    frames = []

    for portfolio_id, portfolio_trades in trades.groupby("portfolio_id", sort=True):
        start_idx = meta[portfolio_id]["start_idx"]
        span = n_days - start_idx
        if span <= 0:
            continue

        instruments = sorted(portfolio_trades["instrument_id"].unique())
        pos_of = {inst: k for k, inst in enumerate(instruments)}

        quantity = np.zeros((len(instruments), span))
        avg_cost = np.zeros((len(instruments), span))

        for instrument_id, inst_trades in portfolio_trades.groupby(
            "instrument_id", sort=False
        ):
            k = pos_of[instrument_id]
            inst_trades = inst_trades.sort_values(["day_idx", "transaction_id"])

            running_qty = 0
            running_cost = 0.0
            deltas = np.zeros(span)
            cost_marks = np.full(span, np.nan)

            for _, txn in inst_trades.iterrows():
                offset = int(txn["day_idx"]) - start_idx
                if txn["txn_type"] == TXN_BUY:
                    running_cost = (
                        running_cost * running_qty + float(txn["price"]) * int(txn["quantity"])
                    ) / (running_qty + int(txn["quantity"]))
                    running_qty += int(txn["quantity"])
                    deltas[offset] += int(txn["quantity"])
                else:
                    running_qty -= int(txn["quantity"])
                    deltas[offset] -= int(txn["quantity"])
                    if running_qty == 0:
                        running_cost = 0.0
                cost_marks[offset] = running_cost

            quantity[k] = np.cumsum(deltas)

            # Carry the average cost forward until the next BUY re-marks it.
            marks = pd.Series(cost_marks).ffill().fillna(0.0).to_numpy()
            avg_cost[k] = marks

        rows_idx = [id_to_row[i] for i in instruments]
        price_block = prices[rows_idx, start_idx:]

        market_value = np.round(quantity * price_block, 2)
        live = quantity > 0
        if not live.any():
            continue

        totals = np.where(live, market_value, 0.0).sum(axis=0)
        totals[totals == 0.0] = np.nan
        weight = np.round(np.where(live, market_value, 0.0) / totals, 4)

        k_idx, t_idx = np.nonzero(live)
        frames.append(
            pd.DataFrame(
                {
                    "portfolio_id": portfolio_id,
                    "instrument_id": np.asarray(instruments)[k_idx],
                    "as_of_date": trading_days[start_idx:][t_idx].date,
                    "quantity": quantity[k_idx, t_idx].astype(np.int64),
                    "avg_cost": np.round(avg_cost[k_idx, t_idx], 2),
                    "market_value": market_value[k_idx, t_idx],
                    "weight": weight[k_idx, t_idx],
                }
            )
        )

    holdings = pd.concat(frames, ignore_index=True)
    holdings = holdings.sort_values(
        ["portfolio_id", "as_of_date", "instrument_id"], kind="mergesort"
    ).reset_index(drop=True)
    holdings.insert(0, "holding_id", np.arange(1, len(holdings) + 1))
    return holdings


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def validate(prices_df, ledger, holdings, portfolios, universe, trading_days):
    problems = []

    def check(condition, message):
        if not condition:
            problems.append(message)

    valid_instruments = set(universe["instrument_id"].astype(int))
    valid_portfolios = set(portfolios["portfolio_id"].astype(int))
    session_dates = {d.date() for d in trading_days}

    # ---- prices ----
    check(
        len(prices_df) == len(universe) * len(trading_days),
        f"prices row count {len(prices_df)} != {len(universe) * len(trading_days)}",
    )
    check(not prices_df.duplicated(["instrument_id", "price_date"]).any(),
          "duplicate (instrument_id, price_date) in prices")
    check((prices_df["adj_close"] > 0).all(), "non-positive adj_close in prices")
    check((prices_df["volume"] > 0).all(), "non-positive volume in prices")
    check(prices_df["price_id"].is_unique, "duplicate price_id")

    # ---- transactions ----
    check(ledger["transaction_id"].is_unique, "duplicate transaction_id")
    check(set(ledger["instrument_id"]) <= valid_instruments, "transactions FK break: instrument")
    check(set(ledger["portfolio_id"]) <= valid_portfolios, "transactions FK break: portfolio")
    check(set(ledger["txn_date"]) <= session_dates, "transaction on a non-trading day")
    check((ledger["quantity"] >= 0).all(), "negative transaction quantity")
    check((ledger["price"] >= 0).all(), "negative transaction price")
    check((ledger["fees"] >= 0).all(), "negative transaction fees")

    inception = dict(
        zip(
            portfolios["portfolio_id"].astype(int),
            pd.to_datetime(portfolios["inception_date"]).dt.date,
        )
    )
    early = ledger[
        ledger.apply(lambda r: r["txn_date"] < inception[r["portfolio_id"]], axis=1)
    ]
    check(early.empty, f"{len(early)} transactions before portfolio inception")

    dividends = ledger[ledger["txn_type"] == TXN_DIVIDEND]
    check((dividends["quantity"] == 0).all(), "DIVIDEND row with non-zero quantity")
    check((dividends["price"] == 0).all(), "DIVIDEND row with non-zero price")
    check((dividends["amount"] > 0).all(), "DIVIDEND row with non-positive amount")
    equity_ids = set(
        universe.loc[universe["asset_type"] == "EQUITY", "instrument_id"].astype(int)
    )
    check(set(dividends["instrument_id"]) <= equity_ids, "DIVIDEND on a non-EQUITY instrument")

    # Every trade must resolve to a price row on that date.
    price_keys = set(zip(prices_df["instrument_id"], prices_df["price_date"]))
    trade_keys = set(zip(ledger["instrument_id"], ledger["txn_date"]))
    check(trade_keys <= price_keys, "transaction without a matching prices.csv row")

    # ---- holdings ----
    check(holdings["holding_id"].is_unique, "duplicate holding_id")
    check(not holdings.duplicated(["portfolio_id", "instrument_id", "as_of_date"]).any(),
          "duplicate (portfolio_id, instrument_id, as_of_date) in holdings")
    check((holdings["quantity"] > 0).all(), "non-positive quantity in holdings")
    check(set(holdings["instrument_id"]) <= valid_instruments, "holdings FK break: instrument")
    check(set(holdings["portfolio_id"]) <= valid_portfolios, "holdings FK break: portfolio")
    check(set(holdings["as_of_date"]) <= session_dates, "holding on a non-trading day")
    early_h = holdings[
        holdings.apply(lambda r: r["as_of_date"] < inception[r["portfolio_id"]], axis=1)
    ]
    check(early_h.empty, f"{len(early_h)} holdings before portfolio inception")

    # market_value must equal quantity x that day's adj_close, to the cent.
    merged = holdings.merge(
        prices_df[["instrument_id", "price_date", "adj_close"]],
        left_on=["instrument_id", "as_of_date"],
        right_on=["instrument_id", "price_date"],
        how="left",
    )
    check(merged["adj_close"].notna().all(), "holding with no price row for that date")
    expected = np.round(merged["quantity"] * merged["adj_close"], 2)
    drift = (merged["market_value"] - expected).abs()
    check(drift.max() < 0.005, f"market_value mismatch, max drift {drift.max()}")

    # Weights sum to 1 per portfolio-day (4dp rounding leaves a small residue).
    weight_sums = holdings.groupby(["portfolio_id", "as_of_date"])["weight"].sum()
    check(
        (weight_sums - 1.0).abs().max() < 0.01,
        f"weight sum off by {(weight_sums - 1.0).abs().max():.6f}",
    )

    # Holdings must reconcile with a cumulative replay of the ledger.
    trades = ledger[ledger["txn_type"] != TXN_DIVIDEND].copy()
    trades["signed"] = np.where(
        trades["txn_type"] == TXN_BUY, trades["quantity"], -trades["quantity"]
    )
    last_day = holdings["as_of_date"].max()
    replay = (
        trades[trades["txn_date"] <= last_day]
        .groupby(["portfolio_id", "instrument_id"])["signed"]
        .sum()
    )
    check((replay >= 0).all(), "cumulative replay produced a negative position")

    final = holdings[holdings["as_of_date"] == last_day].set_index(
        ["portfolio_id", "instrument_id"]
    )["quantity"]
    # Compare only positions that were still open on the final session.
    for key, quantity in final.items():
        if int(replay.get(key, 0)) != int(quantity):
            problems.append(f"replay mismatch for {key}")
            break

    return problems


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    rng = np.random.default_rng(SEED)

    trading_days = build_trading_calendar()
    n_days = len(trading_days)
    sessions_2025 = int((trading_days.year == 2025).sum())
    sessions_2026 = int((trading_days.year == 2026).sum())

    print("=" * 62)
    print("Synthetic EOD generation")
    print("=" * 62)
    print(f"Seed                : {SEED}")
    print(f"Risk-free (annual)  : {RISK_FREE_ANNUAL:.4f}  -> daily {RISK_FREE_DAILY:.8f}")
    print(f"Benchmark           : {BENCHMARK_TICKER}")
    print(f"Sessions            : {n_days}  (2025: {sessions_2025}, 2026: {sessions_2026})")

    universe = load_universe()
    tiers = assign_size_tiers(universe, rng)
    print(f"Instruments         : {len(universe)}")

    # ---- prices ----
    returns = build_returns(universe, tiers, n_days, rng)
    start = seed_prices(universe, tiers, rng)
    price_matrix = np.round(start[:, None] * np.cumprod(1.0 + returns, axis=1), 2)
    price_matrix = np.maximum(price_matrix, 0.01)
    volume_matrix = build_volumes(universe, tiers, returns, rng)

    n_inst = len(universe)
    prices_df = pd.DataFrame(
        {
            "instrument_id": np.repeat(universe["instrument_id"].to_numpy(), n_days),
            "price_date": np.tile(trading_days.date, n_inst),
            "adj_close": price_matrix.reshape(-1),
            "volume": volume_matrix.reshape(-1),
        }
    )
    prices_df.insert(0, "price_id", np.arange(1, len(prices_df) + 1))
    print(f"prices rows         : {len(prices_df):,}")

    # ---- transactions ----
    portfolios = pd.read_csv(PORTFOLIO_CSV)
    annual_yield, pay_days = dividend_schedule(universe, n_days, trading_days, rng)
    pools = build_candidate_pools(universe, tiers, annual_yield)

    ledger, meta = generate_ledger(
        portfolios, universe, pools, price_matrix, trading_days,
        annual_yield, pay_days, rng,
    )
    counts = ledger["txn_type"].value_counts()
    print(
        f"transactions rows   : {len(ledger):,}  "
        f"(BUY {counts.get(TXN_BUY, 0):,} / SELL {counts.get(TXN_SELL, 0):,} / "
        f"DIVIDEND {counts.get(TXN_DIVIDEND, 0):,})"
    )

    # ---- holdings ----
    holdings = generate_holdings(ledger, meta, universe, price_matrix, trading_days)
    print(f"holdings rows       : {len(holdings):,}")

    archetype_counts = pd.Series(
        [m["archetype"] for m in meta.values()]
    ).value_counts()
    print("-" * 62)
    print("Portfolios by archetype")
    for name, count in archetype_counts.items():
        print(f"  {name:<22} {count}")

    # ---- validate then write ----
    print("-" * 62)
    problems = validate(prices_df, ledger, holdings, portfolios, universe, trading_days)
    if problems:
        for problem in problems:
            print(f"  FAIL  {problem}")
        raise SystemExit("validation failed, nothing written")
    print("Validation          : all checks passed")

    prices_df.to_csv(PRICES_CSV, index=False, float_format="%.2f")
    ledger.to_csv(TRANSACTIONS_CSV, index=False, float_format="%.2f")

    # weight carries 4dp, the money columns 2dp, so it is serialised
    # separately instead of sharing one float_format.
    holdings_out = holdings.copy()
    holdings_out["weight"] = holdings_out["weight"].map("{:.4f}".format)
    holdings_out.to_csv(HOLDINGS_CSV, index=False, float_format="%.2f")

    print("-" * 62)
    for path in (PRICES_CSV, TRANSACTIONS_CSV, HOLDINGS_CSV):
        print(f"  wrote {path.relative_to(PROJECT_ROOT)}  "
              f"({path.stat().st_size / 1e6:.1f} MB)")
    print("=" * 62)


if __name__ == "__main__":
    main()
