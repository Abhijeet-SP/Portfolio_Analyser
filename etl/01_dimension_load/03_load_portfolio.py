from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from etl.db_connection import get_connection
from etl.error_logger import (
    start_log,
    log_error,
    end_log,
)

def upsert_portfolio(cursor, portfolio):
# Insert a portfolio if it doesn't exist.
# Update it if it already exists

    query = """
    INSERT INTO portfolios
    (portfolio_id, portfolio_name, base_currency, inception_date)

    VALUES
    (%s,%s,%s, %s)

    ON CONFLICT (portfolio_id)

    DO UPDATE
    SET
        portfolio_name = EXCLUDED.portfolio_name,
        base_currency = EXCLUDED.base_currency,
        inception_date = EXCLUDED.inception_date;
    """

    cursor.execute(
        query,
        (
            portfolio["portfolio_id"],
            portfolio["portfolio_name"],
            portfolio["base_currency"],
            portfolio["inception_date"],
        ),
    )


def load_portfolio():
    print("Loading portfolio data...")
    portfolios = pd.read_csv(PROJECT_ROOT / "data" / "03_portfolio_universe.csv")
    # whole data will be directly taken from a csv file.

    conn = get_connection()
    cursor = conn.cursor()

    success = 0
    failed = 0

    log_file = PROJECT_ROOT / "reports" / "01_dimension_error_logs.txt"

    start_log(
        log_file=log_file,
        script_name=Path(__file__).name,
    )

    for _, row in portfolios.iterrows():
        portfolio = row.to_dict()

        try:
            upsert_portfolio(cursor, portfolio)
            conn.commit()

            success += 1
            print(f"Loaded : {portfolio['portfolio_name']}")

        except Exception as e:
            conn.rollback()   # Reset transaction after a failure
            failed += 1

            print(f"Failed : {portfolio['portfolio_name']}")
            print(e)

            log_error(
                log_file=log_file,
                ticker=portfolio["portfolio_name"],
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
    load_portfolio()