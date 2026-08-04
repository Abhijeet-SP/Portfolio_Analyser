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


# Database reference
def get_benchmarks(cursor):

    cursor.execute("""
        SELECT
            benchmark_id,
            symbol
        FROM benchmarks
        ORDER BY benchmark_id;
    """)

    return cursor.fetchall()


# Insert or update benchmark prices
def upsert_benchmark_price(
    cursor,
    benchmark_id,
    price_date,
    close_price,
):

    query = """
    INSERT INTO benchmark_prices
    (
        benchmark_id,
        price_date,
        close
    )

    VALUES
    (%s, %s, %s)

    ON CONFLICT
    (
        benchmark_id,
        price_date
    )

    DO UPDATE
    SET
        close = EXCLUDED.close;
    """

    cursor.execute(
        query,
        (
            benchmark_id,
            price_date,
            float(close_price),
        ),
    )


# Download benchmark prices from Yahoo Finance
def download_benchmark_prices(symbol):

    end_date = date.today()
    start_date = end_date - timedelta(days=BACKFILL_DAYS)

    data = yf.download(
        symbol,
        start=start_date,
        end=end_date,
        progress=False,
        auto_adjust=False,
    )

    return data


def load_benchmark_prices():

    print("=" * 60)
    print("Loading Benchmark Prices")
    print("=" * 60)

    conn = get_connection()
    cursor = conn.cursor()

    success = 0
    failed = 0
    total_rows = 0

    log_file = PROJECT_ROOT / "reports" / "02_price_error_logs.txt"

    start_log(
        log_file=log_file,
        script_name=Path(__file__).name,
    )

    benchmarks = get_benchmarks(cursor)

    try:
        for benchmark_id, symbol in benchmarks:

            print(f"\nDownloading {symbol}")

            try:
                prices = download_benchmark_prices(symbol)

                if prices.empty:
                    print("No data found.")
                    failed += 1
                    continue

                inserted = 0

                for price_date, row in prices.iterrows():

                    upsert_benchmark_price(
                        cursor,
                        benchmark_id,
                        price_date.date(),
                        row["Close"],
                    )

                    inserted += 1

                conn.commit()

                success += 1
                total_rows += inserted

                print(f"{inserted} rows loaded.")

            except Exception as e:

                conn.rollback()
                failed += 1

                print(f"Failed : {symbol}")
                print(e)

                log_error(
                    log_file=log_file,
                    ticker=symbol,
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
    print(f"Benchmarks Loaded : {success}")
    print(f"Benchmarks Failed : {failed}")
    print(f"Total Rows Loaded : {total_rows}")
    print("=" * 60)


if __name__ == "__main__":
    load_benchmark_prices()