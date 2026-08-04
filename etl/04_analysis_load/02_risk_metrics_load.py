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

# Benchmark beta / alpha / information ratio are measured against.
# Must match the benchmark used by 01_daily_returns_load.py.
BENCHMARK_ID = 1

TRADING_DAYS = 252

# Lookback windows, in calendar days. ITD = inception to date.
PERIOD_DAYS = {
    "1M": 30,
    "3M": 91,
    "6M": 182,
    "1Y": 365,
    "3Y": 365 * 3,
    "5Y": 365 * 5,
    "ITD": None,
}

# Below this many return observations the statistics are noise, so the
# period is skipped rather than written with a misleading number.
MIN_OBS = 20

# Used when the indicator table has no India 10Y yield on/before as_of_date.
DEFAULT_RISK_FREE = 0.065

# Dispersion floor. A flat series does not compute to std == 0.0 exactly
# (float noise leaves ~1e-19), and dividing by that noise yields a ratio in
# the 1e17 range which overflows NUMERIC(12,8). Anything under this counts
# as no dispersion, so the ratio is reported as NULL instead.
EPSILON = 1e-12


def get_risk_free_rate(cursor, as_of_date):
    """
    Annual risk free rate as a fraction. Latest India 10Y yield on or before
    as_of_date, falling back to a flat assumption.
    """

    cursor.execute(
        """
        SELECT value
        FROM economic_indicator_prices
        WHERE indicator_code = 'INDIA_10Y'
          AND observation_date <= %s
        ORDER BY observation_date DESC
        LIMIT 1;
        """,
        (as_of_date,),
    )

    row = cursor.fetchone()

    if row is None:
        return DEFAULT_RISK_FREE

    # stored in percent
    return float(row[0]) / 100.0


def fetch_return_series(cursor, today):

    cursor.execute(
        """
        SELECT
            portfolio_id,
            return_date,
            daily_return,
            benchmark_return
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
            "benchmark_return",
        ],
    )

    for column in ["daily_return", "benchmark_return"]:
        returns_df[column] = pd.to_numeric(
            returns_df[column],
            errors="coerce",
        )

    return returns_df


def get_last_metric_date(cursor):

    cursor.execute("""
        SELECT MAX(as_of_date)
        FROM risk_metrics;
    """)

    return cursor.fetchone()[0]


def max_drawdown(portfolio_returns):
    """
    Deepest peak-to-trough fall of the compounded wealth curve, as a
    negative fraction.
    """

    wealth = (1 + portfolio_returns).cumprod()

    return float((wealth / wealth.cummax() - 1).min())


def calculate_risk_metrics(portfolio_returns, benchmark_returns, risk_free_annual):
    """
    Annualised risk statistics for one portfolio over one window.
    Benchmark-relative metrics come back None when the benchmark series
    does not overlap the window enough to be meaningful.
    """

    risk_free_daily = risk_free_annual / TRADING_DAYS

    excess = portfolio_returns - risk_free_daily

    std_daily = portfolio_returns.std(ddof=1)

    volatility = std_daily * np.sqrt(TRADING_DAYS)

    sharpe = (
        excess.mean() / std_daily * np.sqrt(TRADING_DAYS)
        if std_daily > EPSILON
        else None
    )

    # Sortino punishes downside only: RMS of shortfalls against the
    # risk free rate, zero-filled on upside days.
    downside = excess.where(excess < 0, 0.0)
    downside_std = np.sqrt((downside ** 2).mean())

    sortino = (
        excess.mean() / downside_std * np.sqrt(TRADING_DAYS)
        if downside_std > EPSILON
        else None
    )

    beta = None
    alpha = None
    information_ratio = None

    paired = pd.concat(
        [portfolio_returns, benchmark_returns],
        axis=1,
        keys=["portfolio", "benchmark"],
    ).dropna()

    if len(paired) >= MIN_OBS:

        benchmark_variance = paired["benchmark"].var(ddof=1)

        if benchmark_variance > EPSILON:

            beta = (
                paired["portfolio"].cov(paired["benchmark"])
                / benchmark_variance
            )

            alpha = (
                (paired["portfolio"].mean() - risk_free_daily)
                - beta * (paired["benchmark"].mean() - risk_free_daily)
            ) * TRADING_DAYS

        active = paired["portfolio"] - paired["benchmark"]
        tracking_error = active.std(ddof=1)

        if tracking_error > EPSILON:
            information_ratio = (
                active.mean() / tracking_error * np.sqrt(TRADING_DAYS)
            )

    # Historical VaR: the 5th percentile day. Expected shortfall is the
    # average of the days at or beyond it.
    # "lower" takes an actually observed day rather than interpolating
    # between two: on a short window, interpolation can land above the worst
    # loss and report a positive VaR, understating the tail.
    var_95 = portfolio_returns.quantile(0.05, interpolation="lower")
    tail = portfolio_returns[portfolio_returns <= var_95]

    return {
        "volatility": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "beta": beta,
        "alpha": alpha,
        "information_ratio": information_ratio,
        "max_drawdown": max_drawdown(portfolio_returns),
        "var_95": var_95,
        "expected_shortfall": tail.mean() if not tail.empty else None,
    }


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


def upsert_risk_metrics(cursor, metric):

    query = """
    INSERT INTO risk_metrics
    (
        portfolio_id,
        benchmark_id,
        as_of_date,
        period,
        volatility,
        sharpe,
        sortino,
        beta,
        alpha,
        information_ratio,
        max_drawdown,
        var_95,
        expected_shortfall
    )

    VALUES
    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)

    ON CONFLICT (portfolio_id, benchmark_id, as_of_date, period)

    DO UPDATE
    SET
        volatility         = EXCLUDED.volatility,
        sharpe             = EXCLUDED.sharpe,
        sortino            = EXCLUDED.sortino,
        beta               = EXCLUDED.beta,
        alpha              = EXCLUDED.alpha,
        information_ratio  = EXCLUDED.information_ratio,
        max_drawdown       = EXCLUDED.max_drawdown,
        var_95             = EXCLUDED.var_95,
        expected_shortfall = EXCLUDED.expected_shortfall;
    """

    cursor.execute(
        query,
        (
            metric["portfolio_id"],
            metric["benchmark_id"],
            metric["as_of_date"],
            metric["period"],
            metric["volatility"],
            metric["sharpe"],
            metric["sortino"],
            metric["beta"],
            metric["alpha"],
            metric["information_ratio"],
            metric["max_drawdown"],
            metric["var_95"],
            metric["expected_shortfall"],
        ),
    )


def build_metric_rows(returns_df, risk_free_annual):
    """
    One row per portfolio per period, as of that portfolio's latest
    return date.
    """

    rows = []

    for portfolio_id, portfolio_df in returns_df.groupby("portfolio_id"):

        portfolio_df = portfolio_df.sort_values("return_date")

        as_of_date = portfolio_df["return_date"].max()

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

            portfolio_returns = window["daily_return"].reset_index(drop=True)
            benchmark_returns = window["benchmark_return"].reset_index(drop=True)

            metrics = calculate_risk_metrics(
                portfolio_returns,
                benchmark_returns,
                risk_free_annual,
            )

            rows.append(
                {
                    "portfolio_id": int(portfolio_id),
                    "benchmark_id": BENCHMARK_ID,
                    "as_of_date": as_of_date,
                    "period": period,
                    "volatility": clean(metrics["volatility"]),
                    "sharpe": clean(metrics["sharpe"]),
                    "sortino": clean(metrics["sortino"]),
                    "beta": clean(metrics["beta"]),
                    "alpha": clean(metrics["alpha"]),
                    "information_ratio": clean(metrics["information_ratio"]),
                    # NUMERIC(9,6) in the schema
                    "max_drawdown": clean(metrics["max_drawdown"], 6),
                    "var_95": clean(metrics["var_95"]),
                    "expected_shortfall": clean(metrics["expected_shortfall"]),
                }
            )

    return rows


def load_risk_metrics():

    print("=" * 60)
    print("Loading Risk Metrics")
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
    last_metric_date = get_last_metric_date(cursor)

    if last_metric_date is not None and last_metric_date >= latest_return_date:
        print("Risk Metrics already up to date.")
        cursor.close()
        conn.close()
        return

    risk_free_annual = get_risk_free_rate(cursor, latest_return_date)
    print(f"Risk free rate : {risk_free_annual:.4%} annual")

    rows = build_metric_rows(returns_df, risk_free_annual)

    if not rows:
        print(f"No period has the {MIN_OBS} observations needed. Nothing to load.")
        cursor.close()
        conn.close()
        return

    success = 0
    failed = 0

    log_file = PROJECT_ROOT / "reports" / "07_risk_metrics_error_logs.txt"

    start_log(
        log_file=log_file,
        script_name=Path(__file__).name,
    )

    try:

        for metric in rows:

            try:

                upsert_risk_metrics(cursor, metric)

                conn.commit()

                success += 1

                print(
                    f"Loaded : Portfolio {metric['portfolio_id']} | "
                    f"{metric['as_of_date']} | {metric['period']}"
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
    load_risk_metrics()
