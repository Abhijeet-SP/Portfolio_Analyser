from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from datetime import date
from etl.db_connection import get_connection

def upsert_holdings(cursor, holding):
    """
    Insert a holding if it doesn't exist.
    Update it if it already exists.
    """

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
    print("Loading holdings data...")
    holding_df = pd.read_csv("data/05_holding_universe.csv")

    holding_df["as_of_date"] = pd.to_datetime(holding_df["as_of_date"])
    holding_df = holding_df[holding_df["as_of_date"].dt.date <= date.today()]

    conn = get_connection()
    cursor = conn.cursor()

    success = 0
    failed = 0

    for _, row in holding_df.iterrows():
        holdings = row.to_dict()

        try:
            upsert_holdings(cursor, holdings)
            conn.commit() 

            success += 1
            print(f"Loaded : {holdings['holding_id']}")

        except Exception as e:
            conn.rollback()   # Reset transaction after a failure
            failed += 1

            print(f"Failed : {holdings['holding_id']}")
            print(e)

    cursor.close()
    conn.close()

    print("\n------------------------------")
    print(f"Successful : {success}")
    print(f"Failed     : {failed}")
    print("------------------------------")


if __name__ == "__main__":
    load_holdings()