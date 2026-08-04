from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import date

from etl.db_connection import get_connection
from etl.error_logger import (
    start_log,
    log_record_error,
    end_log,
)

# Benchmark the return series is compared against.
# ponytail: single house benchmark (Nifty 50). Add a portfolios.benchmark_id
# column and read it per portfolio if portfolios ever need different ones.
BENCHMARK_ID = 1


# holdings + portfolio_cashflows -> money-weighted daily return, compounded
# into a cumulative series, benchmark return joined on the same date.
# Mirrors sql/Calculation_check/01_return_cal.sql.
RETURNS_QUERY = """
WITH daily_market_value AS (
    SELECT
        portfolio_id,
        as_of_date,
        SUM(market_value) AS eod_value
    FROM holdings
    WHERE as_of_date <= %(today)s
    GROUP BY portfolio_id, as_of_date
),

benchmark AS (
    SELECT
        price_date,
        close / NULLIF(
            LAG(close) OVER (ORDER BY price_date),
            0
        ) - 1 AS benchmark_return
    FROM benchmark_prices
    WHERE benchmark_id = %(benchmark_id)s
      AND price_date <= %(today)s
),

portfolio_values AS (
    SELECT
        dmv.portfolio_id,
        dmv.as_of_date,
        dmv.eod_value,

        LAG(dmv.eod_value)
            OVER (
                PARTITION BY dmv.portfolio_id
                ORDER BY dmv.as_of_date
            ) AS prev_day_value,

        COALESCE(pcf.net_cash_flow, 0)   AS net_cash_flow,
        COALESCE(pcf.dividend_income, 0) AS dividend_income

    FROM daily_market_value dmv

    LEFT JOIN portfolio_cashflows pcf
        ON  pcf.portfolio_id = dmv.portfolio_id
        AND pcf.flow_date    = dmv.as_of_date
),

returns AS (
    SELECT
        portfolio_id,
        as_of_date,

        CASE
            -- first observation has no prior day
            WHEN prev_day_value IS NULL THEN NULL
            -- no capital at risk: return is undefined, not zero
            WHEN (prev_day_value + net_cash_flow) <= 0 THEN NULL
            ELSE ROUND(
                (
                    eod_value
                    + dividend_income
                    - prev_day_value
                    - net_cash_flow
                )
                /
                (
                    prev_day_value
                    + net_cash_flow
                ),
                8
            )
        END AS daily_return

    FROM portfolio_values
),

cumulative AS (
    SELECT
        portfolio_id,
        as_of_date,
        daily_return,

        ROUND(
            EXP(
                SUM(
                    -- NULL days drop out of the sum; a total wipeout
                    -- (-1.0) would blow up LN, so it drops out too
                    CASE
                        WHEN daily_return > -1 THEN LN(1 + daily_return)
                    END
                ) OVER (
                    PARTITION BY portfolio_id
                    ORDER BY as_of_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                )
            ) - 1,
            8
        ) AS cumulative_return

    FROM returns
)

SELECT
    c.portfolio_id,
    c.as_of_date,
    c.daily_return,
    c.cumulative_return,
    ROUND(b.benchmark_return, 8) AS benchmark_return
FROM cumulative c

LEFT JOIN benchmark b
    ON b.price_date = c.as_of_date

WHERE c.as_of_date > %(from_date)s
ORDER BY c.portfolio_id, c.as_of_date;
"""


def get_last_return_date(cursor):

    cursor.execute("""
        SELECT MAX(return_date)
        FROM daily_returns;
    """)

    return cursor.fetchone()[0]


def calculate_daily_returns(cursor, from_date, today):
    """
    Compute the whole return series in the database and hand back only the
    rows after from_date. The window functions still see full history, so
    cumulative_return stays correct on an incremental run.
    """

    cursor.execute(
        RETURNS_QUERY,
        {
            "today": today,
            "benchmark_id": BENCHMARK_ID,
            "from_date": from_date,
        },
    )

    return cursor.fetchall()


def upsert_daily_return(cursor, daily_return):

    query = """
    INSERT INTO daily_returns
    (
        portfolio_id,
        return_date,
        daily_return,
        cumulative_return,
        benchmark_return
    )

    VALUES
    (%s, %s, %s, %s, %s)

    ON CONFLICT (portfolio_id, return_date)

    DO UPDATE
    SET
        daily_return      = EXCLUDED.daily_return,
        cumulative_return = EXCLUDED.cumulative_return,
        benchmark_return  = EXCLUDED.benchmark_return;
    """

    cursor.execute(
        query,
        (
            daily_return["portfolio_id"],
            daily_return["return_date"],
            daily_return["daily_return"],
            daily_return["cumulative_return"],
            daily_return["benchmark_return"],
        ),
    )


def load_daily_returns():

    print("=" * 60)
    print("Loading Daily Returns")
    print("=" * 60)

    today = date.today()

    conn = get_connection()
    cursor = conn.cursor()

    last_date = get_last_return_date(cursor)

    # nothing loaded yet -> take everything
    from_date = last_date if last_date is not None else date(1900, 1, 1)

    rows = calculate_daily_returns(cursor, from_date, today)

    if not rows:
        print("Daily Returns already up to date.")
        cursor.close()
        conn.close()
        return

    success = 0
    failed = 0

    log_file = PROJECT_ROOT / "reports" / "06_daily_returns_error_logs.txt"

    start_log(
        log_file=log_file,
        script_name=Path(__file__).name,
    )

    try:

        for row in rows:

            daily_return = {
                "portfolio_id": row[0],
                "return_date": row[1],
                "daily_return": row[2],
                "cumulative_return": row[3],
                "benchmark_return": row[4],
            }

            try:

                upsert_daily_return(cursor, daily_return)

                conn.commit()

                success += 1

                print(
                    f"Loaded : Portfolio {daily_return['portfolio_id']} | "
                    f"{daily_return['return_date']} | "
                    f"return={daily_return['daily_return']}"
                )

            except Exception as e:

                conn.rollback()

                failed += 1

                print(
                    f"Failed : Portfolio {daily_return['portfolio_id']} | "
                    f"{daily_return['return_date']}"
                )
                print(e)

                log_record_error(
                    log_file=log_file,
                    record=(
                        f"Portfolio={daily_return['portfolio_id']}, "
                        f"Date={daily_return['return_date']}"
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
    load_daily_returns()
