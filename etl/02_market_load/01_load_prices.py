from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import date, timedelta
import yfinance as yf

from etl.db_connection import get_connection
from etl.error_logger import (
    start_log,
    log_error,
    end_log,
)

BACKFILL_DAYS = 365


def get_instruments(cursor):
    # Returns: List of (instrument_id, ticker)

    cursor.execute("""
        SELECT
            instrument_id,
            ticker
        FROM instruments
        ORDER BY instrument_id;
    """)

    return cursor.fetchall()


def upsert_price(
    cursor,
    instrument_id,
    price_date,
    adj_close,
    volume,
):
    # Update the daily prices in the table prices wrt the master reference table

    query = """
    INSERT INTO prices
    (
        instrument_id,
        price_date,
        adj_close,
        volume
    )

    VALUES
    (%s, %s, %s, %s)

    ON CONFLICT
    (
        instrument_id,
        price_date
    )

    DO UPDATE SET

        adj_close = EXCLUDED.adj_close,
        volume    = EXCLUDED.volume;
    """

    cursor.execute(
        query,
        (
            instrument_id,
            price_date,
            float(adj_close),
            int(volume),
        ),
    )


# Download the data from Yahoo Finance
def download_prices(ticker):

    end_date = date.today()
    start_date = end_date - timedelta(days=BACKFILL_DAYS)

    data = yf.download(
        ticker,
        start=start_date,
        end=end_date,
        progress=False,
        auto_adjust=False,
    )

    return data


# Load the prices
def load_prices():

    print("=" * 60)
    print("Loading Historical Prices")
    print("=" * 60)

    conn = get_connection()
    cursor = conn.cursor()

    success = 0
    failed = 0
    total_loaded = 0

    log_file = PROJECT_ROOT / "reports" / "fact_error_logs.txt"

    start_log(
        log_file=log_file,
        script_name=Path(__file__).name,
    )

    instruments = get_instruments(cursor)

    try:
        for instrument_id, ticker in instruments:

            print(f"\nDownloading {ticker}...")

            try:
                prices = download_prices(ticker)

                if prices.empty:
                    print("No data found.")
                    failed += 1
                    continue

                inserted = 0

                for price_date, row in prices.iterrows():

                    upsert_price(
                        cursor,
                        instrument_id,
                        price_date.date(),
                        row["Adj Close"],
                        row["Volume"],
                    )

                    inserted += 1

                conn.commit()

                success += 1
                total_loaded += inserted

                print(f"{inserted} rows loaded.")

            except Exception as e:

                conn.rollback()
                failed += 1

                print(f"Failed : {ticker}")
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

    print("\n" + "=" * 60)
    print(f"Instruments Loaded : {success}")
    print(f"Instruments Failed : {failed}")
    print(f"Total Rows Loaded : {total_loaded}")
    print("=" * 60)


if __name__ == "__main__":
    load_prices()