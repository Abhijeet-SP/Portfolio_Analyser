"""Derive end-of-day holdings snapshots from the transaction ledger.

A portfolio management system never stores arbitrary positions: holdings are a
projection of transaction history. This script is the only sanctioned producer of
``data/holdings.csv``.

    data/portfolio.csv  +  data/transactions.csv
                        |
                        v   replay chronologically
                 data/holdings.csv

Position accounting rules implemented here
------------------------------------------
BUY       increases quantity and re-weights average cost
SELL      decreases quantity, average cost is left untouched
DIVIDEND  income only, quantity and average cost unchanged

Valuation
---------
Historical market prices are not available to this stage of the pipeline, so each
position is valued at the latest observed traded price for that instrument on or
before the snapshot date. Prices are taken market-wide (across every portfolio),
because an execution price is a market fact rather than a portfolio fact, and a
single instrument must value identically in every portfolio on a given date.

Snapshots
---------
One snapshot per portfolio per date on which that portfolio transacted, taken
after every transaction for that date has been applied. Positions that reach zero
quantity are dropped from that snapshot onward.

Every derived figure is validated before anything is written. On any breach the
script raises and writes nothing.

Usage:
    python etl/03_gen_holding/generate_holdings.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

PORTFOLIO_CSV = DATA_DIR / "portfolio.csv"
TRANSACTIONS_CSV = DATA_DIR / "transactions.csv"
HOLDINGS_CSV = DATA_DIR / "holdings.csv"

TXN_BUY = "BUY"
TXN_SELL = "SELL"
TXN_DIVIDEND = "DIVIDEND"
VALID_TXN_TYPES = frozenset({TXN_BUY, TXN_SELL, TXN_DIVIDEND})

# Instrument dimension already loaded in PostgreSQL: instrument_id 1..100.
MIN_INSTRUMENT_ID = 1
MAX_INSTRUMENT_ID = 100

PORTFOLIO_COLUMNS = ["portfolio_id", "portfolio_name", "base_currency", "inception_date"]
TRANSACTION_COLUMNS = [
    "transaction_id",
    "portfolio_id",
    "instrument_id",
    "txn_date",
    "txn_type",
    "quantity",
    "price",
    "fees",
    "amount",
]
HOLDING_COLUMNS = [
    "holding_id",
    "portfolio_id",
    "instrument_id",
    "as_of_date",
    "quantity",
    "avg_cost",
    "market_value",
    "weight",
]

# Rounding aligned with the NUMERIC precision declared in sql/04_history.sql.
QUANTITY_DECIMALS = 6
AVG_COST_DECIMALS = 6
MARKET_VALUE_DECIMALS = 4
WEIGHT_DECIMALS = 6

# Quantities below this are treated as a fully closed position, absorbing binary
# floating-point residue from repeated add/subtract cycles.
ZERO_QUANTITY_TOLERANCE = 1e-9
# Accumulated rounding of up to a few dozen weights, each rounded to 6 decimals.
WEIGHT_SUM_TOLERANCE = 1e-4
# Tolerance for market_value == quantity * price, in rupees.
MARKET_VALUE_TOLERANCE = 1e-3
# Tolerance when reconciling replayed quantities against an independent recompute.
RECONCILIATION_TOLERANCE = 1e-6
# Tolerance on the weighted-average-cost bound check, in rupees.
AVG_COST_TOLERANCE = 1e-4

LOGGER = logging.getLogger("generate_holdings")


class HoldingsValidationError(Exception):
    """Raised when input transactions or derived holdings violate a business rule."""


# --------------------------------------------------------------------------- #
# Position state
# --------------------------------------------------------------------------- #

@dataclass
class Position:
    """Running state of a single instrument inside a single portfolio.

    Attributes:
        quantity: Units currently held; never negative.
        avg_cost: Weighted average acquisition cost per unit.
    """

    quantity: float = 0.0
    avg_cost: float = 0.0

    def apply_buy(self, quantity: float, price: float) -> None:
        """Add units and re-weight the average cost.

        new_avg_cost = (old_qty * old_avg_cost + buy_qty * buy_price)
                       / (old_qty + buy_qty)
        """
        total_quantity = self.quantity + quantity
        if total_quantity <= 0.0:
            raise HoldingsValidationError(
                f"BUY of {quantity} produced a non-positive total quantity {total_quantity}."
            )
        self.avg_cost = (self.quantity * self.avg_cost + quantity * price) / total_quantity
        self.quantity = total_quantity

    def apply_sell(self, quantity: float) -> None:
        """Remove units. Average cost is deliberately left unchanged."""
        remaining = self.quantity - quantity
        if remaining < -ZERO_QUANTITY_TOLERANCE:
            raise HoldingsValidationError(
                f"SELL of {quantity} exceeds held quantity {self.quantity}."
            )
        self.quantity = 0.0 if abs(remaining) <= ZERO_QUANTITY_TOLERANCE else remaining
        if self.quantity == 0.0:
            # Position closed: cost basis is retired with it.
            self.avg_cost = 0.0

    @property
    def is_open(self) -> bool:
        """True when the position still carries units."""
        return self.quantity > ZERO_QUANTITY_TOLERANCE


@dataclass
class PriceBook:
    """Last-known traded price per instrument, as of a walked-forward date.

    The replay visits dates in ascending order, so a simple mutable map of the most
    recent traded price is sufficient and avoids repeated as-of lookups.

    Attributes:
        last_price: instrument_id -> most recently observed traded price.
    """

    last_price: dict[int, float] = field(default_factory=dict)

    def observe(self, instrument_id: int, price: float) -> None:
        """Record a traded price. Zero-price rows (dividends) carry no information."""
        if price > 0.0:
            self.last_price[instrument_id] = price

    def value_of(self, instrument_id: int, quantity: float) -> float:
        """Return ``quantity * latest_price`` for an instrument.

        Raises:
            HoldingsValidationError: If no traded price has ever been observed,
                which would mean a position exists without an acquiring trade.
        """
        price = self.last_price.get(instrument_id)
        if price is None:
            raise HoldingsValidationError(
                f"No traded price observed for instrument_id={instrument_id}; "
                "a position cannot exist without a prior BUY."
            )
        return quantity * price


# --------------------------------------------------------------------------- #
# Input loading and validation
# --------------------------------------------------------------------------- #

def load_portfolios(path: Path = PORTFOLIO_CSV) -> pd.DataFrame:
    """Read and validate the portfolio dimension.

    Raises:
        HoldingsValidationError: On missing columns, duplicate keys or bad dates.
    """
    if not path.exists():
        raise HoldingsValidationError(f"Portfolio file not found: {path}")

    frame = pd.read_csv(path)
    missing = [column for column in PORTFOLIO_COLUMNS if column not in frame.columns]
    if missing:
        raise HoldingsValidationError(f"{path.name} is missing columns: {missing}")

    frame = frame[PORTFOLIO_COLUMNS].copy()
    frame["portfolio_id"] = frame["portfolio_id"].astype(int)
    frame["inception_date"] = pd.to_datetime(frame["inception_date"], errors="raise")

    duplicate_ids = frame.loc[frame["portfolio_id"].duplicated(), "portfolio_id"].tolist()
    if duplicate_ids:
        raise HoldingsValidationError(f"Duplicate portfolio_id values: {duplicate_ids}")

    duplicate_names = frame.loc[frame["portfolio_name"].duplicated(), "portfolio_name"].tolist()
    if duplicate_names:
        raise HoldingsValidationError(f"Duplicate portfolio_name values: {duplicate_names}")

    LOGGER.info("loaded %d portfolios from %s", len(frame), path.name)
    return frame


def load_transactions(path: Path = TRANSACTIONS_CSV) -> pd.DataFrame:
    """Read the transaction ledger and coerce it into replay order.

    Returns:
        Transactions sorted by ``txn_date`` then ``transaction_id``.

    Raises:
        HoldingsValidationError: On missing columns or duplicate transaction IDs.
    """
    if not path.exists():
        raise HoldingsValidationError(f"Transactions file not found: {path}")

    frame = pd.read_csv(path)
    missing = [column for column in TRANSACTION_COLUMNS if column not in frame.columns]
    if missing:
        raise HoldingsValidationError(f"{path.name} is missing columns: {missing}")

    frame = frame[TRANSACTION_COLUMNS].copy()
    frame["transaction_id"] = frame["transaction_id"].astype(int)
    frame["portfolio_id"] = frame["portfolio_id"].astype(int)
    frame["instrument_id"] = frame["instrument_id"].astype(int)
    frame["txn_date"] = pd.to_datetime(frame["txn_date"], errors="raise")
    for column in ("quantity", "price", "fees", "amount"):
        frame[column] = frame[column].astype(float)

    duplicate_ids = frame.loc[frame["transaction_id"].duplicated(), "transaction_id"].tolist()
    if duplicate_ids:
        raise HoldingsValidationError(f"Duplicate transaction_id values: {duplicate_ids[:10]}")

    frame = frame.sort_values(["txn_date", "transaction_id"], kind="stable").reset_index(drop=True)
    LOGGER.info("loaded %d transactions from %s", len(frame), path.name)
    return frame


def validate_transactions(transactions: pd.DataFrame, portfolios: pd.DataFrame) -> None:
    """Assert every business rule the transaction ledger must satisfy.

    Checks referential integrity, transaction types, sign conventions, the cash
    amount formulas, weekday-only trading and trade-after-inception.

    Raises:
        HoldingsValidationError: With a description of the first rule breached.
    """
    unknown_types = sorted(set(transactions["txn_type"]) - VALID_TXN_TYPES)
    if unknown_types:
        raise HoldingsValidationError(f"Unsupported txn_type values: {unknown_types}")

    known_portfolios = set(portfolios["portfolio_id"])
    orphans = sorted(set(transactions["portfolio_id"]) - known_portfolios)
    if orphans:
        raise HoldingsValidationError(f"Transactions reference unknown portfolio_id: {orphans}")

    out_of_range = transactions.loc[
        (transactions["instrument_id"] < MIN_INSTRUMENT_ID)
        | (transactions["instrument_id"] > MAX_INSTRUMENT_ID),
        "instrument_id",
    ]
    if not out_of_range.empty:
        raise HoldingsValidationError(
            f"instrument_id outside {MIN_INSTRUMENT_ID}..{MAX_INSTRUMENT_ID}: "
            f"{sorted(out_of_range.unique())}"
        )

    negatives = transactions[
        (transactions["quantity"] < 0) | (transactions["price"] < 0) | (transactions["fees"] < 0)
    ]
    if not negatives.empty:
        raise HoldingsValidationError(
            f"Negative quantity/price/fees on transaction_id "
            f"{negatives['transaction_id'].head(10).tolist()}"
        )

    weekend = transactions[transactions["txn_date"].dt.dayofweek >= 5]
    if not weekend.empty:
        raise HoldingsValidationError(
            f"Transactions dated on a weekend: {weekend['transaction_id'].head(10).tolist()}"
        )

    inception_by_portfolio = portfolios.set_index("portfolio_id")["inception_date"]
    before_inception = transactions[
        transactions["txn_date"]
        < transactions["portfolio_id"].map(inception_by_portfolio)
    ]
    if not before_inception.empty:
        raise HoldingsValidationError(
            "Transactions dated before portfolio inception: "
            f"{before_inception['transaction_id'].head(10).tolist()}"
        )

    trades = transactions[transactions["txn_type"].isin({TXN_BUY, TXN_SELL})]
    invalid_trades = trades[(trades["quantity"] <= 0) | (trades["price"] <= 0) | (trades["fees"] <= 0)]
    if not invalid_trades.empty:
        raise HoldingsValidationError(
            "BUY/SELL rows require quantity, price and fees > 0; offending transaction_id: "
            f"{invalid_trades['transaction_id'].head(10).tolist()}"
        )

    buys = transactions[transactions["txn_type"] == TXN_BUY]
    expected_buy_amount = -(buys["quantity"] * buys["price"] + buys["fees"])
    buy_mismatch = buys[(buys["amount"] - expected_buy_amount).abs() > MARKET_VALUE_TOLERANCE]
    if not buy_mismatch.empty:
        raise HoldingsValidationError(
            "BUY amount must equal -(quantity * price + fees); offending transaction_id: "
            f"{buy_mismatch['transaction_id'].head(10).tolist()}"
        )

    sells = transactions[transactions["txn_type"] == TXN_SELL]
    expected_sell_amount = sells["quantity"] * sells["price"] - sells["fees"]
    sell_mismatch = sells[(sells["amount"] - expected_sell_amount).abs() > MARKET_VALUE_TOLERANCE]
    if not sell_mismatch.empty:
        raise HoldingsValidationError(
            "SELL amount must equal (quantity * price - fees); offending transaction_id: "
            f"{sell_mismatch['transaction_id'].head(10).tolist()}"
        )

    dividends = transactions[transactions["txn_type"] == TXN_DIVIDEND]
    invalid_dividends = dividends[
        (dividends["quantity"] != 0)
        | (dividends["price"] != 0)
        | (dividends["fees"] != 0)
        | (dividends["amount"] <= 0)
    ]
    if not invalid_dividends.empty:
        raise HoldingsValidationError(
            "DIVIDEND rows require quantity = price = fees = 0 and amount > 0; "
            f"offending transaction_id: {invalid_dividends['transaction_id'].head(10).tolist()}"
        )

    LOGGER.info("transaction ledger passed %d business-rule checks", 10)


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #

def _snapshot_rows(
    portfolio_id: int,
    as_of_date: pd.Timestamp,
    positions: dict[int, Position],
    price_book: PriceBook,
) -> list[dict[str, object]]:
    """Build the end-of-day rows for one portfolio/date.

    Closed positions are excluded, and weights are normalised across the open
    positions so that they sum to 1.0 for the snapshot.
    """
    open_positions = {
        instrument_id: position
        for instrument_id, position in sorted(positions.items())
        if position.is_open
    }
    if not open_positions:
        return []

    market_values = {
        instrument_id: round(
            price_book.value_of(instrument_id, position.quantity), MARKET_VALUE_DECIMALS
        )
        for instrument_id, position in open_positions.items()
    }
    total_market_value = sum(market_values.values())
    if total_market_value <= 0.0:
        raise HoldingsValidationError(
            f"Portfolio {portfolio_id} on {as_of_date.date()} has open positions but a "
            f"total market value of {total_market_value}."
        )

    return [
        {
            "portfolio_id": portfolio_id,
            "instrument_id": instrument_id,
            "as_of_date": as_of_date.date().isoformat(),
            "quantity": round(position.quantity, QUANTITY_DECIMALS),
            "avg_cost": round(position.avg_cost, AVG_COST_DECIMALS),
            "market_value": market_values[instrument_id],
            "weight": round(market_values[instrument_id] / total_market_value, WEIGHT_DECIMALS),
        }
        for instrument_id, position in open_positions.items()
    ]


def replay_transactions(transactions: pd.DataFrame) -> pd.DataFrame:
    """Replay the ledger chronologically and emit end-of-day holdings snapshots.

    Args:
        transactions: Validated ledger sorted by ``txn_date`` then ``transaction_id``.

    Returns:
        Holdings frame without ``holding_id``, one row per open position per
        portfolio per transacting date.

    Raises:
        HoldingsValidationError: On an oversold position, a dividend on an unheld
            instrument, or a date sequence that is not monotonic.
    """
    price_book = PriceBook()
    positions_by_portfolio: dict[int, dict[int, Position]] = {}
    last_date_by_portfolio: dict[int, pd.Timestamp] = {}
    snapshots: list[dict[str, object]] = []

    # Grouping by date keeps the walk-forward price book globally correct: every
    # trade on a date is observed before any portfolio is valued for that date.
    for as_of_date, day_transactions in transactions.groupby("txn_date", sort=True):
        for row in day_transactions.itertuples(index=False):
            price_book.observe(int(row.instrument_id), float(row.price))

        for row in day_transactions.itertuples(index=False):
            portfolio_id = int(row.portfolio_id)
            instrument_id = int(row.instrument_id)
            positions = positions_by_portfolio.setdefault(portfolio_id, {})
            position = positions.setdefault(instrument_id, Position())

            previous_date = last_date_by_portfolio.get(portfolio_id)
            if previous_date is not None and as_of_date < previous_date:
                raise HoldingsValidationError(
                    f"Transaction chronology broken for portfolio {portfolio_id}: "
                    f"{as_of_date.date()} follows {previous_date.date()}."
                )

            if row.txn_type == TXN_BUY:
                position.apply_buy(float(row.quantity), float(row.price))
            elif row.txn_type == TXN_SELL:
                if not position.is_open:
                    raise HoldingsValidationError(
                        f"transaction_id={row.transaction_id} sells instrument "
                        f"{instrument_id} in portfolio {portfolio_id} before any BUY."
                    )
                try:
                    position.apply_sell(float(row.quantity))
                except HoldingsValidationError as error:
                    raise HoldingsValidationError(
                        f"transaction_id={row.transaction_id} on {as_of_date.date()}: {error}"
                    ) from error
            else:  # DIVIDEND: income only, position untouched.
                if not position.is_open:
                    raise HoldingsValidationError(
                        f"transaction_id={row.transaction_id} pays a dividend on instrument "
                        f"{instrument_id} not held by portfolio {portfolio_id}."
                    )

            last_date_by_portfolio[portfolio_id] = as_of_date

        for portfolio_id in sorted(day_transactions["portfolio_id"].unique()):
            snapshots.extend(
                _snapshot_rows(
                    portfolio_id=int(portfolio_id),
                    as_of_date=as_of_date,
                    positions=positions_by_portfolio[int(portfolio_id)],
                    price_book=price_book,
                )
            )

    holdings = pd.DataFrame(snapshots, columns=HOLDING_COLUMNS[1:])
    holdings = holdings.sort_values(
        ["portfolio_id", "as_of_date", "instrument_id"], kind="stable"
    ).reset_index(drop=True)
    holdings.insert(0, "holding_id", np.arange(1, len(holdings) + 1))

    LOGGER.info(
        "replayed %d transactions into %d holding rows across %d portfolios",
        len(transactions), len(holdings), holdings["portfolio_id"].nunique(),
    )
    return holdings


# --------------------------------------------------------------------------- #
# Output validation
# --------------------------------------------------------------------------- #

def validate_keys_and_ranges(holdings: pd.DataFrame, portfolios: pd.DataFrame) -> None:
    """Check primary keys, uniqueness, foreign keys and value ranges."""
    if holdings.empty:
        raise HoldingsValidationError("No holdings were produced from the transaction ledger.")

    duplicate_ids = holdings.loc[holdings["holding_id"].duplicated(), "holding_id"].tolist()
    if duplicate_ids:
        raise HoldingsValidationError(f"Duplicate holding_id values: {duplicate_ids[:10]}")

    natural_key = ["portfolio_id", "instrument_id", "as_of_date"]
    duplicate_rows = holdings[holdings.duplicated(subset=natural_key, keep=False)]
    if not duplicate_rows.empty:
        raise HoldingsValidationError(
            "Duplicate snapshot rows violate uq_holdings_pf_inst_date: "
            f"{duplicate_rows[natural_key].head(10).to_dict('records')}"
        )

    orphans = sorted(set(holdings["portfolio_id"]) - set(portfolios["portfolio_id"]))
    if orphans:
        raise HoldingsValidationError(f"Holdings reference unknown portfolio_id: {orphans}")

    out_of_range = holdings.loc[
        (holdings["instrument_id"] < MIN_INSTRUMENT_ID)
        | (holdings["instrument_id"] > MAX_INSTRUMENT_ID),
        "instrument_id",
    ]
    if not out_of_range.empty:
        raise HoldingsValidationError(
            f"Holdings reference instrument_id outside "
            f"{MIN_INSTRUMENT_ID}..{MAX_INSTRUMENT_ID}: {sorted(out_of_range.unique())}"
        )

    if (holdings["quantity"] <= 0).any():
        offenders = holdings.loc[holdings["quantity"] <= 0, natural_key].head(10)
        raise HoldingsValidationError(
            f"Non-positive quantities present (closed positions must be dropped): "
            f"{offenders.to_dict('records')}"
        )

    if (holdings["market_value"] < 0).any():
        raise HoldingsValidationError("Negative market_value present in holdings.")

    if (holdings["avg_cost"] <= 0).any():
        offenders = holdings.loc[holdings["avg_cost"] <= 0, natural_key].head(10)
        raise HoldingsValidationError(
            f"Non-positive avg_cost on an open position: {offenders.to_dict('records')}"
        )

    if not holdings["weight"].between(0.0, 1.0).all():
        raise HoldingsValidationError("weight outside the 0..1 range present in holdings.")

    LOGGER.info("key, foreign-key and range checks passed")


def validate_weights(holdings: pd.DataFrame) -> None:
    """Assert weights sum to 1.0 for every portfolio/date snapshot."""
    weight_sums = holdings.groupby(["portfolio_id", "as_of_date"])["weight"].sum()
    breaches = weight_sums[(weight_sums - 1.0).abs() > WEIGHT_SUM_TOLERANCE]
    if not breaches.empty:
        worst = breaches.abs().sub(1.0).abs().idxmax()
        raise HoldingsValidationError(
            f"{len(breaches)} snapshot(s) have weights that do not sum to 1.0 "
            f"(tolerance {WEIGHT_SUM_TOLERANCE}); worst: portfolio {worst[0]} on {worst[1]} "
            f"sums to {weight_sums.loc[worst]:.8f}"
        )
    LOGGER.info("weights sum to 1.0 for all %d snapshots", len(weight_sums))


def validate_market_values(holdings: pd.DataFrame, transactions: pd.DataFrame) -> None:
    """Assert every market value equals quantity times the latest traded price.

    Recomputes the as-of price independently with a merge-asof against the traded
    price history, rather than trusting the replay's price book.
    """
    trades = transactions.loc[
        transactions["price"] > 0, ["txn_date", "instrument_id", "transaction_id", "price"]
    ].sort_values(["transaction_id"], kind="stable")
    # Last traded price per instrument per date.
    daily_price = (
        trades.groupby(["instrument_id", "txn_date"], as_index=False)["price"]
        .last()
        .sort_values("txn_date", kind="stable")
    )

    subject = holdings[["portfolio_id", "instrument_id", "as_of_date", "quantity", "market_value"]].copy()
    subject["as_of_date"] = pd.to_datetime(subject["as_of_date"])
    subject = subject.sort_values("as_of_date", kind="stable")

    merged = pd.merge_asof(
        subject,
        daily_price.rename(columns={"txn_date": "as_of_date", "price": "as_of_price"}),
        on="as_of_date",
        by="instrument_id",
        direction="backward",
        allow_exact_matches=True,
    )

    if merged["as_of_price"].isna().any():
        offenders = merged.loc[merged["as_of_price"].isna()].head(10)
        raise HoldingsValidationError(
            f"No traded price on or before the snapshot date for: "
            f"{offenders[['portfolio_id', 'instrument_id', 'as_of_date']].to_dict('records')}"
        )

    expected = (merged["quantity"] * merged["as_of_price"]).round(MARKET_VALUE_DECIMALS)
    deviation = (merged["market_value"] - expected).abs()
    if (deviation > MARKET_VALUE_TOLERANCE).any():
        worst_index = deviation.idxmax()
        worst = merged.loc[worst_index]
        raise HoldingsValidationError(
            "market_value does not equal quantity * latest traded price for portfolio "
            f"{int(worst['portfolio_id'])}, instrument {int(worst['instrument_id'])} on "
            f"{worst['as_of_date'].date()}: {worst['market_value']} vs "
            f"{expected.loc[worst_index]}"
        )
    LOGGER.info("market values reconcile with the traded price history")


def validate_reconciliation(holdings: pd.DataFrame, transactions: pd.DataFrame) -> None:
    """Reconcile every snapshot quantity against a vectorised recompute.

    For each portfolio, signed transaction quantities are pivoted into a
    date-by-instrument matrix and cumulatively summed. This derivation is fully
    independent of the replay loop, so agreement between the two proves the
    holdings match the transaction history exactly.

    Raises:
        HoldingsValidationError: On any quantity mismatch, missing position or
            spurious position.
    """
    signed = transactions.copy()
    direction = signed["txn_type"].map({TXN_BUY: 1.0, TXN_SELL: -1.0, TXN_DIVIDEND: 0.0})
    signed["signed_quantity"] = signed["quantity"] * direction

    holdings_dates = holdings.copy()
    holdings_dates["as_of_date"] = pd.to_datetime(holdings_dates["as_of_date"])

    for portfolio_id, portfolio_txns in signed.groupby("portfolio_id", sort=True):
        matrix = (
            portfolio_txns.pivot_table(
                index="txn_date",
                columns="instrument_id",
                values="signed_quantity",
                aggfunc="sum",
                fill_value=0.0,
            )
            .sort_index()
            .cumsum()
        )

        expected = (
            matrix.stack()
            .rename("expected_quantity")
            .reset_index()
            .rename(columns={"txn_date": "as_of_date"})
        )
        expected = expected[expected["expected_quantity"] > ZERO_QUANTITY_TOLERANCE]

        actual = holdings_dates.loc[
            holdings_dates["portfolio_id"] == portfolio_id,
            ["as_of_date", "instrument_id", "quantity"],
        ]

        comparison = expected.merge(
            actual, on=["as_of_date", "instrument_id"], how="outer", indicator=True
        )

        missing = comparison[comparison["_merge"] == "left_only"]
        if not missing.empty:
            raise HoldingsValidationError(
                f"Portfolio {portfolio_id}: {len(missing)} position(s) implied by transactions "
                f"are absent from holdings, e.g. instrument "
                f"{int(missing.iloc[0]['instrument_id'])} on "
                f"{missing.iloc[0]['as_of_date'].date()}."
            )

        spurious = comparison[comparison["_merge"] == "right_only"]
        if not spurious.empty:
            raise HoldingsValidationError(
                f"Portfolio {portfolio_id}: {len(spurious)} holding row(s) are not implied by "
                f"transactions, e.g. instrument {int(spurious.iloc[0]['instrument_id'])} on "
                f"{spurious.iloc[0]['as_of_date'].date()}."
            )

        deviation = (comparison["expected_quantity"] - comparison["quantity"]).abs()
        if (deviation > RECONCILIATION_TOLERANCE).any():
            worst = comparison.loc[deviation.idxmax()]
            raise HoldingsValidationError(
                f"Portfolio {portfolio_id} does not reconcile: instrument "
                f"{int(worst['instrument_id'])} on {worst['as_of_date'].date()} holds "
                f"{worst['quantity']} but transactions imply {worst['expected_quantity']}."
            )

    LOGGER.info("all portfolios reconcile exactly with the transaction ledger")


def validate_average_cost(holdings: pd.DataFrame, transactions: pd.DataFrame) -> None:
    """Bound-check each average cost against the BUY prices that could produce it.

    A weighted average of BUY prices must lie between the cheapest and dearest BUY
    executed on or before the snapshot date, which catches sells leaking into the
    cost basis as well as arithmetic errors.
    """
    buys = transactions.loc[
        transactions["txn_type"] == TXN_BUY,
        ["portfolio_id", "instrument_id", "txn_date", "price"],
    ].sort_values("txn_date", kind="stable")

    bounds = (
        buys.groupby(["portfolio_id", "instrument_id", "txn_date"])["price"]
        .agg(["min", "max"])
        .reset_index()
        .sort_values("txn_date", kind="stable")
    )
    bounds["running_min"] = bounds.groupby(["portfolio_id", "instrument_id"])["min"].cummin()
    bounds["running_max"] = bounds.groupby(["portfolio_id", "instrument_id"])["max"].cummax()

    subject = holdings[["portfolio_id", "instrument_id", "as_of_date", "avg_cost"]].copy()
    subject["as_of_date"] = pd.to_datetime(subject["as_of_date"])
    subject = subject.sort_values("as_of_date", kind="stable")

    merged = pd.merge_asof(
        subject,
        bounds[["portfolio_id", "instrument_id", "txn_date", "running_min", "running_max"]].rename(
            columns={"txn_date": "as_of_date"}
        ),
        on="as_of_date",
        by=["portfolio_id", "instrument_id"],
        direction="backward",
        allow_exact_matches=True,
    )

    if merged["running_min"].isna().any():
        offenders = merged.loc[merged["running_min"].isna()].head(10)
        raise HoldingsValidationError(
            "Open position with no preceding BUY: "
            f"{offenders[['portfolio_id', 'instrument_id', 'as_of_date']].to_dict('records')}"
        )

    breaches = merged[
        (merged["avg_cost"] < merged["running_min"] - AVG_COST_TOLERANCE)
        | (merged["avg_cost"] > merged["running_max"] + AVG_COST_TOLERANCE)
    ]
    if not breaches.empty:
        worst = breaches.iloc[0]
        raise HoldingsValidationError(
            f"avg_cost {worst['avg_cost']} for portfolio {int(worst['portfolio_id'])}, instrument "
            f"{int(worst['instrument_id'])} on {worst['as_of_date'].date()} lies outside the "
            f"observed BUY price range [{worst['running_min']}, {worst['running_max']}]."
        )
    LOGGER.info("average costs are consistent with the BUY price history")


def validate_holdings(
    holdings: pd.DataFrame, transactions: pd.DataFrame, portfolios: pd.DataFrame
) -> None:
    """Run every output validation. Raises on the first failure, writing nothing."""
    validate_keys_and_ranges(holdings, portfolios)
    validate_weights(holdings)
    validate_market_values(holdings, transactions)
    validate_average_cost(holdings, transactions)
    validate_reconciliation(holdings, transactions)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def write_holdings(holdings: pd.DataFrame, path: Path = HOLDINGS_CSV) -> None:
    """Write the validated holdings snapshots to CSV in schema column order."""
    holdings[HOLDING_COLUMNS].to_csv(path, index=False)
    LOGGER.info("wrote %s (%d rows)", path, len(holdings))


def generate_holdings(
    portfolio_path: Path = PORTFOLIO_CSV,
    transactions_path: Path = TRANSACTIONS_CSV,
    holdings_path: Path = HOLDINGS_CSV,
) -> pd.DataFrame:
    """Run the full derivation: load, validate, replay, validate again, write.

    Args:
        portfolio_path: Source portfolio dimension CSV.
        transactions_path: Source transaction ledger CSV.
        holdings_path: Destination holdings CSV.

    Returns:
        The written holdings frame.

    Raises:
        HoldingsValidationError: If any input or derived rule is violated. Nothing
            is written in that case.
    """
    portfolios = load_portfolios(portfolio_path)
    transactions = load_transactions(transactions_path)

    validate_transactions(transactions, portfolios)
    holdings = replay_transactions(transactions)
    validate_holdings(holdings, transactions, portfolios)

    write_holdings(holdings, holdings_path)
    return holdings


def main() -> None:
    """Command-line entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    holdings = generate_holdings()
    LOGGER.info(
        "summary: %d holding rows over %d portfolio-date snapshots, %s to %s",
        len(holdings),
        holdings.groupby(["portfolio_id", "as_of_date"]).ngroups,
        holdings["as_of_date"].min(),
        holdings["as_of_date"].max(),
    )


if __name__ == "__main__":
    main()
