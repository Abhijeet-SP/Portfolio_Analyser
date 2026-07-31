from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from datetime import date
from etl.db_connection import get_connection

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


def load_transactions():
    print("Loading transactions data...")
    transaction_df = pd.read_csv("data/04_transaction_universe.csv")

    transaction_df["txn_date"] = pd.to_datetime(transaction_df["txn_date"])
    transaction_df = transaction_df[transaction_df["txn_date"].dt.date <= date.today()]

    conn = get_connection()
    cursor = conn.cursor()

    success = 0
    failed = 0

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

    cursor.close()
    conn.close()

    print("\n------------------------------")
    print(f"Successful : {success}")
    print(f"Failed     : {failed}")
    print("------------------------------")


if __name__ == "__main__":
    load_transactions()