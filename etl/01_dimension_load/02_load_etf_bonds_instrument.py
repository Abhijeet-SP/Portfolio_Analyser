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


def clean_row(row):
    # CSV blanks come back from pandas as NaN.
    # sector is the only nullable column, so send it as None instead of "NaN".

    instrument = row.to_dict()
    instrument["sector"] = None if pd.isna(instrument["sector"]) else instrument["sector"]
    return instrument


def upsert_instrument(cursor, instrument):
    # Insert an instrument if it doesn't exist.
    # Update it if it already exists

    query = """
    INSERT INTO instruments
    (ticker, instrument_name, sector, asset_type, currency)

    VALUES
    (%s, %s, %s, %s, %s)

    ON CONFLICT (ticker)
    DO UPDATE
    SET
        instrument_name = EXCLUDED.instrument_name,
        sector = EXCLUDED.sector,
        asset_type = EXCLUDED.asset_type,
        currency = EXCLUDED.currency;
    """

    # EXCLUDED refers to the incoming row in an UPSERT.
    cursor.execute(
        query,
        (
            instrument["ticker"],
            instrument["instrument_name"],
            instrument["sector"],
            instrument["asset_type"],
            instrument["currency"],
        ),
    )


def load_instruments():
    print("Loading ticker universe...")
    tickers = pd.read_csv("data/02_etf_bonds_ticker_universe.csv")

    conn = get_connection()
    cursor = conn.cursor()

    success = 0
    failed = 0

    log_file = PROJECT_ROOT / "reports" / "dimension_error_logs.txt"

    start_log(
        log_file=log_file,
        script_name=Path(__file__).name,
    )

    try:
        # _ to skip the index and only get the row
        for _, row in tickers.iterrows():
            ticker = clean_row(row)

            try:
                upsert_instrument(cursor, ticker)

                conn.commit()

                success += 1
                print(f"Loaded : {ticker["instrument_name"]}")

            except Exception as e:
                conn.rollback()  # Reset transaction after a failure
                failed += 1

                print(f"Failed : {ticker['instrument_name']}")
                print(e)

                log_error(
                    log_file=log_file,
                    ticker=ticker,
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

    print("\n------------------------------")
    print(f"Successful : {success}")
    print(f"Failed     : {failed}")
    print("------------------------------")


if __name__ == "__main__":
    load_instruments()