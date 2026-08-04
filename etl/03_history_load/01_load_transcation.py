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

def upsert_transactions(cursor, transaction):

    # Insert a transaction if it doesn't exist.
    # Update it if it already exists.

    query = """
    INSERT INTO transactions
    (
        transaction_id,
        portfolio_id,
        instrument_id,
        txn_date,
        txn_type,
        quantity,
        price,
        fees,
        amount
    )

    VALUES
    (%s, %s, %s, %s, %s, %s, %s, %s, %s)

    ON CONFLICT (transaction_id)

    DO UPDATE
    SET
        portfolio_id = EXCLUDED.portfolio_id,
        instrument_id = EXCLUDED.instrument_id,
        txn_date = EXCLUDED.txn_date,
        txn_type = EXCLUDED.txn_type,
        quantity = EXCLUDED.quantity,
        price = EXCLUDED.price,
        fees = EXCLUDED.fees,
        amount = EXCLUDED.amount;
    """

    cursor.execute(
        query,
        (
            transaction["transaction_id"],
            transaction["portfolio_id"],
            transaction["instrument_id"],
            transaction["txn_date"],
            transaction["txn_type"],
            transaction["quantity"],
            transaction["price"],
            transaction["fees"],
            transaction["amount"],
        ),
    )

def get_last_transaction_date(cursor):

    cursor.execute("""
        SELECT MAX(txn_date)
        FROM transactions;
    """)

    return cursor.fetchone()[0]

def load_transactions():
    print("Loading transactions data...")
    transaction_df = pd.read_csv(
        PROJECT_ROOT / "data" / "05_transactions_universe.csv",
        parse_dates=["txn_date"],
    )

    today = pd.Timestamp(date.today())
    transaction_df = transaction_df[transaction_df["txn_date"] <= today]

    conn = get_connection()
    cursor = conn.cursor()

    last_date = get_last_transaction_date(cursor)

    if last_date is not None:
        transaction_df = transaction_df[transaction_df["txn_date"] > pd.Timestamp(last_date)]

    if transaction_df.empty:
        print("Transactions already up to date.")
        cursor.close()
        conn.close()
        return

    success = 0
    failed = 0

    log_file = PROJECT_ROOT / "reports" / "03_transactions_error_logs.txt"

    start_log(
        log_file=log_file,
        script_name=Path(__file__).name,
    )

    for _, row in transaction_df.iterrows():
        transaction = row.to_dict()

        try:
            upsert_transactions(cursor, transaction)
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
    load_transactions()