from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from datetime import date

from etl.db_connection import get_connection
from etl.error_logger import (
    start_log,
    log_record_error,
    end_log,
)


def get_last_flow_date(cursor):

    cursor.execute("""
        SELECT MAX(flow_date)
        FROM portfolio_cashflows;
    """)

    return cursor.fetchone()[0]


def upsert_cashflow(cursor, cashflow):

    query = """
    INSERT INTO portfolio_cashflows
(
    portfolio_id,
    flow_date,
    buy_flow,
    sell_flow,
    net_cash_flow,
    dividend_income,
    buy_count,
    sell_count,
    dividend_count
)
VALUES
(%s, %s, %s, %s, %s, %s, %s, %s, %s)

ON CONFLICT (portfolio_id, flow_date)

DO UPDATE
SET
    buy_flow = EXCLUDED.buy_flow,
    sell_flow = EXCLUDED.sell_flow,
    net_cash_flow = EXCLUDED.net_cash_flow,
    dividend_income = EXCLUDED.dividend_income,
    buy_count = EXCLUDED.buy_count,
    sell_count = EXCLUDED.sell_count,
    dividend_count = EXCLUDED.dividend_count;
    """

    cursor.execute(
    query,
    (
        cashflow["portfolio_id"],
        cashflow["flow_date"],
        cashflow["buy_flow"],
        cashflow["sell_flow"],
        cashflow["net_cash_flow"],
        cashflow["dividend_income"],
        cashflow["buy_count"],
        cashflow["sell_count"],
        cashflow["dividend_count"],
        ),
    )


def load_cashflows():

    print("=" * 60)
    print("Loading Portfolio Cash Flows")
    print("=" * 60)

    cashflow_df = pd.read_csv(
        PROJECT_ROOT / "data" / "07_portfolio_cashflows_universe.csv",
        parse_dates=["flow_date"],
    )

    today = pd.Timestamp(date.today())

    cashflow_df = cashflow_df[
        cashflow_df["flow_date"] <= today
    ]

    conn = get_connection()
    cursor = conn.cursor()

    last_date = get_last_flow_date(cursor)

    if last_date is not None:
        cashflow_df = cashflow_df[
            cashflow_df["flow_date"] > pd.Timestamp(last_date)
        ]

    if cashflow_df.empty:
        print("Portfolio Cash Flows already up to date.")
        cursor.close()
        conn.close()
        return

    success = 0
    failed = 0

    log_file = PROJECT_ROOT / "reports" / "05_cashflow_error_logs.txt"

    start_log(
        log_file=log_file,
        script_name=Path(__file__).name,
    )

    try:

        for _, row in cashflow_df.iterrows():

            cashflow = row.to_dict()

            try:

                upsert_cashflow(cursor, cashflow)

                conn.commit()

                success += 1

                print(
                        f"Loaded : Portfolio {cashflow['portfolio_id']} | "
                        f"{cashflow['flow_date'].date()}"
                    )

            except Exception as e:

                conn.rollback()

                failed += 1

                print(
                        f"Failed : Portfolio {cashflow['portfolio_id']} | "
                        f"{cashflow['flow_date'].date()}"
                    )
                print(e)

                log_record_error(
                    log_file=log_file,
                    record=(
                        f"Portfolio={cashflow['portfolio_id']}, "
                        f"Date={cashflow['flow_date'].date()}"
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
    load_cashflows()