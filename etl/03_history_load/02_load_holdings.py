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


def get_last_holding_date(cursor):

    cursor.execute("""
        SELECT MAX(as_of_date)
        FROM holdings;
    """)

    return cursor.fetchone()[0]


def upsert_holdings(cursor, holding):

    query = """
    INSERT INTO holdings
    (
        holding_id,
        portfolio_id,
        instrument_id,
        as_of_date,
        quantity,
        avg_cost,
        market_value,
        weight
    )

    VALUES
    (%s, %s, %s, %s, %s, %s, %s, %s)

    ON CONFLICT (holding_id)

    DO UPDATE
    SET
        portfolio_id = EXCLUDED.portfolio_id,
        instrument_id = EXCLUDED.instrument_id,
        as_of_date = EXCLUDED.as_of_date,
        quantity = EXCLUDED.quantity,
        avg_cost = EXCLUDED.avg_cost,
        market_value = EXCLUDED.market_value,
        weight = EXCLUDED.weight;
    """

    cursor.execute(
        query,
        (
            holding["holding_id"],
            holding["portfolio_id"],
            holding["instrument_id"],
            holding["as_of_date"],
            holding["quantity"],
            holding["avg_cost"],
            holding["market_value"],
            holding["weight"],
        ),
    )


def load_holdings():

    print("=" * 60)
    print("Loading Holdings")
    print("=" * 60)

    holding_df = pd.read_csv(
        PROJECT_ROOT / "data" / "06_holdings_universe.csv",
        parse_dates=["as_of_date"],
    )

    today = pd.Timestamp(date.today())

    holding_df = holding_df[
        holding_df["as_of_date"] <= today
    ]

    conn = get_connection()
    cursor = conn.cursor()

    last_date = get_last_holding_date(cursor)

    if last_date is not None:
        holding_df = holding_df[
            holding_df["as_of_date"] > pd.Timestamp(last_date)
        ]

    if holding_df.empty:
        print("Holdings already up to date.")
        cursor.close()
        conn.close()
        return

    success = 0
    failed = 0

    log_file = PROJECT_ROOT / "reports" / "04_holdings_error_logs.txt"

    start_log(
        log_file=log_file,
        script_name=Path(__file__).name,
    )

    try:

        for _, row in holding_df.iterrows():

            holding = row.to_dict()

            try:

                upsert_holdings(cursor, holding)

                conn.commit()

                success += 1

                print(f"Loaded : {holding['holding_id']}")

            except Exception as e:

                conn.rollback()

                failed += 1

                print(f"Failed : {holding['holding_id']}")
                print(e)

                log_error(
                    log_file=log_file,
                    ticker=f"Holding ID : {holding['holding_id']}",
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
    load_holdings()