from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import date, timedelta

import numpy as np
import pandas as pd

from etl.db_connection import get_connection
from etl.error_logger import (
    start_log,
    log_record_error,
    end_log,
)

DAYS_IN_YEAR = 365

# Lookback windows, in calendar days. ITD = inception to date.
PERIOD_DAYS = {
    "1M": 30,
    "3M": 91,
    "6M": 182,
    "1Y": DAYS_IN_YEAR,
    "3Y": DAYS_IN_YEAR * 3,
    "5Y": DAYS_IN_YEAR * 5,
    "ITD": None,
}

# Two days is the minimum that compounds to anything.
MIN_OBS = 2


def fetch_return_series(cursor, today):

    cursor.execute(
        """
        SELECT
            portfolio_id,
            return_date,
            daily_return,
            cumulative_return
        FROM daily_returns
        WHERE return_date <= %s
        ORDER BY portfolio_id, return_date;
        """,
        (today,),
    )

    returns_df = pd.DataFrame(
        cursor.fetchall(),
        columns=[
            "portfolio_id",
            "return_date",
            "daily_return",
            "cumulative_return",
        ],
    )

    for column in ["daily_return", "cumulative_return"]:
        returns_df[column] = pd.to_numeric(
            returns_df[column],
            errors="coerce",
        )

    return returns_df


def get_last_perf_date(cursor):

    cursor.execute("""
        SELECT MAX(as_of_date)
        FROM performance_metrics;
    """)

    return cursor.fetchone()[0]


def calculate_cagr(total_return, span_days, period_days):
    """
    Annualised growth rate over the window's actual span.

    Sub-year windows (1M/3M/6M) return None: annualising a month of returns
    extrapolates noise into a headline number. Year-and-up windows annualise,
    including a young portfolio whose 3Y/5Y window only holds what exists.
    """

    if period_days is not None and period_days < DAYS_IN_YEAR:
        return None

    if span_days <= 0:
        return None

    growth = 1 + total_return

    # a total wipeout has no real annualised rate
    if growth <= 0:
        return None

    return growth ** (DAYS_IN_YEAR / span_days) - 1


def clean(value, decimals=8):
    """
    Round to the column scale and turn NaN / inf into NULL.
    """

    if value is None:
        return None

    value = float(value)

    if not np.isfinite(value):
        return None

    return round(value, decimals)


def upsert_performance_metrics(cursor, metric):

    query = """
    INSERT INTO performance_metrics
    (
        portfolio_id,
        as_of_date,
        period,
        total_return,
        cagr,
        cumulative_return
    )

    VALUES
    (%s, %s, %s, %s, %s, %s)

    ON CONFLICT (portfolio_id, as_of_date, period)

    DO UPDATE
    SET
        total_return      = EXCLUDED.total_return,
        cagr              = EXCLUDED.cagr,
        cumulative_return = EXCLUDED.cumulative_return;
    """

    cursor.execute(
        query,
        (
            metric["portfolio_id"],
            metric["as_of_date"],
            metric["period"],
            metric["total_return"],
            metric["cagr"],
            metric["cumulative_return"],
        ),
    )


def build_metric_rows(returns_df):
    """
    One row per portfolio per period, as of that portfolio's latest return
    date. total_return is compounded inside the window; cumulative_return is
    the inception-to-date figure already carried by daily_returns.
    """

    rows = []

    for portfolio_id, portfolio_df in returns_df.groupby("portfolio_id"):

        portfolio_df = portfolio_df.sort_values("return_date")

        as_of_date = portfolio_df["return_date"].max()

        inception_to_date = portfolio_df["cumulative_return"].dropna()

        cumulative_return = (
            inception_to_date.iloc[-1] if not inception_to_date.empty else None
        )

        for period, days in PERIOD_DAYS.items():

            if days is None:
                window = portfolio_df
            else:
                window = portfolio_df[
                    portfolio_df["return_date"] > as_of_date - timedelta(days=days)
                ]

            window = window.dropna(subset=["daily_return"])

            if len(window) < MIN_OBS:
                continue

            total_return = (1 + window["daily_return"]).prod() - 1

            span_days = (as_of_date - window["return_date"].min()).days

            rows.append(
                {
                    "portfolio_id": int(portfolio_id),
                    "as_of_date": as_of_date,
                    "period": period,
                    "total_return": clean(total_return),
                    "cagr": clean(calculate_cagr(total_return, span_days, days)),
                    "cumulative_return": clean(cumulative_return),
                }
            )

    return rows


def load_performance_metrics():

    print("=" * 60)
    print("Loading Performance Metrics")
    print("=" * 60)

    today = date.today()

    conn = get_connection()
    cursor = conn.cursor()

    returns_df = fetch_return_series(cursor, today)

    if returns_df.empty:
        print("No daily returns found. Run 01_daily_returns_load.py first.")
        cursor.close()
        conn.close()
        return

    latest_return_date = returns_df["return_date"].max()
    last_perf_date = get_last_perf_date(cursor)

    if last_perf_date is not None and last_perf_date >= latest_return_date:
        print("Performance Metrics already up to date.")
        cursor.close()
        conn.close()
        return

    rows = build_metric_rows(returns_df)

    if not rows:
        print(f"No period has the {MIN_OBS} observations needed. Nothing to load.")
        cursor.close()
        conn.close()
        return

    success = 0
    failed = 0

    log_file = PROJECT_ROOT / "reports" / "08_performance_metrics_error_logs.txt"

    start_log(
        log_file=log_file,
        script_name=Path(__file__).name,
    )

    try:

        for metric in rows:

            try:

                upsert_performance_metrics(cursor, metric)

                conn.commit()

                success += 1

                print(
                    f"Loaded : Portfolio {metric['portfolio_id']} | "
                    f"{metric['as_of_date']} | {metric['period']} | "
                    f"total_return={metric['total_return']}"
                )

            except Exception as e:

                conn.rollback()

                failed += 1

                print(
                    f"Failed : Portfolio {metric['portfolio_id']} | "
                    f"{metric['as_of_date']} | {metric['period']}"
                )
                print(e)

                log_record_error(
                    log_file=log_file,
                    record=(
                        f"Portfolio={metric['portfolio_id']}, "
                        f"Date={metric['as_of_date']}, "
                        f"Period={metric['period']}"
                    ),
                    error=e,
                )

    finally:

        try:
            end_log(
                log_file=log_file,
                success=success,
                failed=failed,
            )
        finally:
            cursor.close()
            conn.close()

    print("\n" + "=" * 30)
    print(f"Successful : {success}")
    print(f"Failed     : {failed}")
    print("=" * 30)


if __name__ == "__main__":
    load_performance_metrics()
