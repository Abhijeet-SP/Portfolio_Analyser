from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from datetime import date
from etl.db_connection import get_connection
from etl.error_logger import (
    start_log,
    log_error,
    end_log,
)

def upsert_daily_returns(cursor, daily_return):

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
        daily_return = EXCLUDED.daily_return,
        cumulative_return = EXCLUDED.cumulative_return,
        benchmark_return = EXCLUDED.benchmark_return;
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

def get_last_return_cal_date(cursor):

    cursor.execute("""
        SELECT MAX(return_date)
        FROM daily_returns;
    """)

    return cursor.fetchone()[0]

def load_daily_returns():
    print("Calculating returns and loading data...")
    holdings_df = pd.read_csv("data/05_transactions_universe.csv",parse_dates=["txn_date"],)

    today = pd.Timestamp(date.today())
    transaction_df = transaction_df[transaction_df["txn_date"] <= today]

    conn = get_connection()
    cursor = conn.cursor()

    last_date = get_last_return_cal_date(cursor)

    if last_date is not None:
        transaction_df = transaction_df[transaction_df["txn_date"] > pd.Timestamp(last_date)]

    if transaction_df.empty:
        print("Transactions already up to date.")
        cursor.close()
        conn.close()
        return

    success = 0
    failed = 0

    log_file = PROJECT_ROOT / "reports" / "transactions_error_logs.txt"

    start_log(
        log_file=log_file,
        script_name=Path(__file__).name,
    )

    for _, row in transaction_df.iterrows():
        transaction = row.to_dict()

        try:
            upsert_daily_returns(cursor, transaction)
            conn.commit()

            success += 1
            print(f"Loaded : {transaction['transaction_id']}")

        except Exception as e:

            conn.rollback()
            failed += 1

            print(f"Failed : {transaction['transaction_id']}")
            print(e)

            log_error(
                log_file=log_file,
                ticker=f"Transaction ID : {transaction['transaction_id']}",
                error=e,
            )

    try:
        end_log(
            log_file=log_file,
            success=success,
            failed=failed,
        )

    finally:
        cursor.close()
        conn.close()

    print("\n------------------------------")
    print(f"Successful : {success}")
    print(f"Failed     : {failed}")
    print("------------------------------")


if __name__ == "__main__":
    load_daily_returns()