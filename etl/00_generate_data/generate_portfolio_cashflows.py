"""Derive the per-portfolio daily cash-flow summary from the transaction ledger.

    data/05_transactions_universe.csv
                    |
                    v   aggregate by (portfolio_id, txn_date)
    data/07_portfolio_cashflows_universe.csv

Why this file exists
--------------------
`holdings` is an end-of-day snapshot and the holdings generator models no cash
account. A BUY therefore raises portfolio market value without debiting
anything, a SELL lowers it without crediting anything, and DIVIDEND cash never
reaches holdings at all. Every one of those is an *external* flow as far as the
holdings series is concerned, so a naive MV_t / MV_t-1 - 1 books contributions
as performance.

This dataset is the flow term the daily-return ETL needs to strip that out. It
lets the ETL read `holdings` + `portfolio_cashflows` instead of re-aggregating
the whole ledger on every run.

    r_t = (MV_t + dividend_income_t - MV_t-1 - net_cash_flow_t)
          / (MV_t-1 + net_cash_flow_t)

`net_cash_flow` deliberately EXCLUDES dividends: a BUY/SELL is a capital
movement that must be netted out of the return, whereas a dividend is income
the portfolio actually earned and must stay in it. Keeping them in separate
columns lets the ETL apply each on the correct side of the formula.

Derived, never hand-edited. Reads one file, writes one file, touches nothing
else.

Usage:
    python etl/00_generate_data/generate_portfolio_cashflows.py
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

TRANSACTIONS_CSV = DATA_DIR / "05_transactions_universe.csv"
CASHFLOWS_CSV = DATA_DIR / "07_portfolio_cashflows_universe.csv"

TXN_BUY = "BUY"
TXN_SELL = "SELL"
TXN_DIVIDEND = "DIVIDEND"

TXN_TYPES = (TXN_BUY, TXN_SELL, TXN_DIVIDEND)

# Money columns are rounded to the paisa, matching the precision the ledger
# itself carries, so cross-file totals reconcile exactly.
MONEY_DP = 2

OUTPUT_COLUMNS = [
    "portfolio_id",
    "flow_date",
    "buy_flow",
    "sell_flow",
    "net_cash_flow",
    "dividend_income",
    "buy_count",
    "sell_count",
    "dividend_count",
]

MONEY_COLUMNS = ["buy_flow", "sell_flow", "net_cash_flow", "dividend_income"]
COUNT_COLUMNS = ["buy_count", "sell_count", "dividend_count"]

logger = logging.getLogger("cashflows")


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #

def load_transactions(path: Path = TRANSACTIONS_CSV) -> pd.DataFrame:
    """Read the ledger and fail loudly on anything that would corrupt the roll-up."""
    logger.info("Reading ledger: %s", path.relative_to(PROJECT_ROOT))
    ledger = pd.read_csv(path, parse_dates=["txn_date"])

    required = {"portfolio_id", "txn_date", "txn_type", "amount"}
    missing = required - set(ledger.columns)
    if missing:
        raise ValueError(f"{path.name} is missing required columns: {sorted(missing)}")

    unknown = set(ledger["txn_type"].unique()) - set(TXN_TYPES)
    if unknown:
        raise ValueError(f"unexpected txn_type values in ledger: {sorted(unknown)}")

    if ledger[["portfolio_id", "txn_date", "txn_type", "amount"]].isna().any().any():
        raise ValueError("ledger contains NULLs in a column the roll-up depends on")

    logger.info(
        "  %s rows, %s portfolios, %s to %s",
        f"{len(ledger):,}",
        ledger["portfolio_id"].nunique(),
        ledger["txn_date"].min().date(),
        ledger["txn_date"].max().date(),
    )
    return ledger


# --------------------------------------------------------------------------- #
# Transform
# --------------------------------------------------------------------------- #

def build_cashflows(ledger: pd.DataFrame) -> pd.DataFrame:
    """Collapse the ledger to one row per (portfolio_id, flow_date).

    Only portfolio-days that actually had a transaction produce a row. Days
    with no activity are absent by design, not zero-filled: the return ETL
    left-joins this onto the holdings calendar and coalesces to 0, which keeps
    the file small and keeps "no activity" distinguishable from "activity that
    happened to net to zero".
    """
    logger.info("Aggregating by (portfolio_id, flow_date)")

    # Sum and count in one pass, then unstack the txn_type level into columns.
    grouped = (
        ledger.groupby(["portfolio_id", "txn_date", "txn_type"])["amount"]
        .agg(["sum", "size"])
        .unstack("txn_type")
    )

    cashflows = pd.DataFrame(index=grouped.index)

    for txn_type, amount_col, count_col in (
        (TXN_BUY, "buy_flow", "buy_count"),
        (TXN_SELL, "sell_flow", "sell_count"),
        (TXN_DIVIDEND, "dividend_income", "dividend_count"),
    ):
        # A type absent from the whole ledger yields no column to unstack.
        has_sum = ("sum", txn_type) in grouped.columns
        has_size = ("size", txn_type) in grouped.columns
        cashflows[amount_col] = (
            grouped[("sum", txn_type)] if has_sum else 0.0
        )
        cashflows[count_col] = (
            grouped[("size", txn_type)] if has_size else 0
        )

    # A portfolio-day with no BUY (or SELL, or DIVIDEND) reports 0, not NULL.
    amount_columns = ["buy_flow", "sell_flow", "dividend_income"]
    cashflows[amount_columns] = cashflows[amount_columns].fillna(0.0)
    cashflows[COUNT_COLUMNS] = cashflows[COUNT_COLUMNS].fillna(0)

    # Capital movement only. Dividends are income, not a flow to net out, so
    # they stay out of this term and are applied on the numerator side instead.
    cashflows["net_cash_flow"] = cashflows["buy_flow"] - cashflows["sell_flow"]

    cashflows = cashflows.reset_index().rename(columns={"txn_date": "flow_date"})

    cashflows[MONEY_COLUMNS] = cashflows[MONEY_COLUMNS].round(MONEY_DP)
    cashflows[COUNT_COLUMNS] = cashflows[COUNT_COLUMNS].astype("int64")
    cashflows["portfolio_id"] = cashflows["portfolio_id"].astype("int64")
    cashflows["flow_date"] = cashflows["flow_date"].dt.date

    cashflows = cashflows.sort_values(
        ["portfolio_id", "flow_date"], kind="mergesort"
    ).reset_index(drop=True)

    return cashflows[OUTPUT_COLUMNS]


# --------------------------------------------------------------------------- #
# Validate
# --------------------------------------------------------------------------- #

def validate(cashflows: pd.DataFrame, ledger: pd.DataFrame) -> list[str]:
    """Reconcile the roll-up against the ledger. Returns a list of failures."""
    problems: list[str] = []

    def check(condition: bool, message: str) -> None:
        status = "PASS" if condition else "FAIL"
        logger.info("  [%s] %s", status, message)
        if not condition:
            problems.append(message)

    logger.info("Validation")

    # --- totals reconcile to the ledger, to the paisa ---
    for txn_type, column in (
        (TXN_BUY, "buy_flow"),
        (TXN_SELL, "sell_flow"),
        (TXN_DIVIDEND, "dividend_income"),
    ):
        expected = round(
            float(ledger.loc[ledger["txn_type"] == txn_type, "amount"].sum()), MONEY_DP
        )
        actual = round(float(cashflows[column].sum()), MONEY_DP)
        check(
            abs(expected - actual) < 0.01,
            f"{column} total {actual:,.2f} == ledger {txn_type} total {expected:,.2f}",
        )

    # --- counts reconcile ---
    for txn_type, column in (
        (TXN_BUY, "buy_count"),
        (TXN_SELL, "sell_count"),
        (TXN_DIVIDEND, "dividend_count"),
    ):
        expected = int((ledger["txn_type"] == txn_type).sum())
        actual = int(cashflows[column].sum())
        check(
            expected == actual,
            f"{column} total {actual:,} == ledger {txn_type} row count {expected:,}",
        )

    total_rows = int(cashflows[COUNT_COLUMNS].to_numpy().sum())
    check(
        total_rows == len(ledger),
        f"all counts sum to {total_rows:,} == ledger rows {len(ledger):,}",
    )

    # --- grain ---
    expected_grain = ledger.groupby(["portfolio_id", "txn_date"]).ngroups
    check(
        len(cashflows) == expected_grain,
        f"row count {len(cashflows):,} == distinct (portfolio_id, txn_date) {expected_grain:,}",
    )
    check(
        not cashflows.duplicated(["portfolio_id", "flow_date"]).any(),
        "no duplicate (portfolio_id, flow_date)",
    )

    # --- internal consistency ---
    net_drift = (
        cashflows["net_cash_flow"] - (cashflows["buy_flow"] - cashflows["sell_flow"])
    ).abs().max()
    check(float(net_drift) < 0.01, f"net_cash_flow == buy_flow - sell_flow (max drift {net_drift})")

    check(not cashflows.isna().any().any(), "no NULLs anywhere in the output")
    check(
        (cashflows[["buy_flow", "sell_flow", "dividend_income"]] >= 0).all().all(),
        "buy_flow / sell_flow / dividend_income are all non-negative",
    )
    check(
        (cashflows[COUNT_COLUMNS] >= 0).all().all(),
        "all counts are non-negative",
    )
    check(
        (cashflows[COUNT_COLUMNS].sum(axis=1) > 0).all(),
        "every row carries at least one transaction",
    )

    # A zero count must pair with a zero amount, and vice versa.
    for amount_col, count_col in (
        ("buy_flow", "buy_count"),
        ("sell_flow", "sell_count"),
        ("dividend_income", "dividend_count"),
    ):
        mismatched = (
            (cashflows[count_col] == 0) & (cashflows[amount_col] != 0)
        ).sum()
        check(int(mismatched) == 0, f"{amount_col} is 0 wherever {count_col} is 0")

    # --- referential: every portfolio-day here must exist in the ledger ---
    ledger_keys = set(
        zip(ledger["portfolio_id"], ledger["txn_date"].dt.date)
    )
    output_keys = set(zip(cashflows["portfolio_id"], cashflows["flow_date"]))
    check(output_keys == ledger_keys, "portfolio-day keys match the ledger exactly")

    return problems


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def report(cashflows: pd.DataFrame) -> None:
    """Print the summary statistics block."""
    logger.info("Summary")
    logger.info("  rows                  : %s", f"{len(cashflows):,}")
    logger.info("  portfolios            : %s", cashflows["portfolio_id"].nunique())
    logger.info("  distinct flow dates   : %s", cashflows["flow_date"].nunique())
    logger.info(
        "  date range            : %s to %s",
        cashflows["flow_date"].min(),
        cashflows["flow_date"].max(),
    )
    for label, column in (
        ("buy_flow", "buy_flow"),
        ("sell_flow", "sell_flow"),
        ("net_cash_flow", "net_cash_flow"),
        ("dividend_income", "dividend_income"),
    ):
        # Pre-formatted: printf-style logging has no thousands separator.
        logger.info("  total %-15s : %22s", label, f"{cashflows[column].sum():,.2f}")
    logger.info(
        "  txn counts            : BUY %s / SELL %s / DIVIDEND %s",
        f"{int(cashflows['buy_count'].sum()):,}",
        f"{int(cashflows['sell_count'].sum()):,}",
        f"{int(cashflows['dividend_count'].sum()):,}",
    )
    logger.info(
        "  active days/portfolio : min %s, median %s, max %s",
        int(cashflows.groupby("portfolio_id").size().min()),
        int(cashflows.groupby("portfolio_id").size().median()),
        int(cashflows.groupby("portfolio_id").size().max()),
    )
    dividend_days = int((cashflows["dividend_count"] > 0).sum())
    logger.info(
        "  rows with a dividend  : %s (%.1f%%)",
        f"{dividend_days:,}",
        100.0 * dividend_days / len(cashflows),
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info("=" * 70)
    logger.info("Portfolio cash-flow roll-up")
    logger.info("=" * 70)

    ledger = load_transactions()
    cashflows = build_cashflows(ledger)

    problems = validate(cashflows, ledger)
    if problems:
        for problem in problems:
            logger.error("  FAILED: %s", problem)
        raise SystemExit("validation failed, nothing written")

    # Money columns are written at fixed 2dp so the file reconciles byte-for-byte
    # against the ledger totals it was derived from.
    cashflows.to_csv(CASHFLOWS_CSV, index=False, float_format="%.2f")

    report(cashflows)
    logger.info(
        "Wrote %s (%.2f MB)",
        CASHFLOWS_CSV.relative_to(PROJECT_ROOT),
        CASHFLOWS_CSV.stat().st_size / 1e6,
    )
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
