from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import date, timedelta
import yfinance as yf
from etl.db_connection import get_connection

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

# inserting the data inside benchmark prices
def upsert_benchmark_price(
    cursor,
    benchmark_id,
    price_date,
    close_price
):

    query = """
    INSERT INTO benchmark_prices
    (
        benchmark_id,
        price_date,
        close
    )

    VALUES
    ( %s, %s, %s )

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
            float(close_price)
        )
    )

def download_benchmark_prices(symbol):

    end_date = date.today()
    start_date = end_date - timedelta(days=BACKFILL_DAYS)

    data = yf.download(
        symbol,
        start=start_date,
        end=end_date,
        progress=False,
        auto_adjust=False
    )

    return data

def load_benchmark_prices():

    print("=" * 60)
    print("Loading Benchmark Prices")
    print("=" * 60)

    conn = get_connection()
    cursor = conn.cursor()
    benchmarks = get_benchmarks(cursor)
    total_rows = 0

    for benchmark_id, symbol in benchmarks:
        print(f"\nDownloading {symbol}")

        try:
            prices = download_benchmark_prices(symbol)
            if prices.empty:
                print("No data found.")
                continue

            inserted = 0

            for price_date, row in prices.iterrows():
                upsert_benchmark_price(
                    cursor,
                    benchmark_id,
                    price_date.date(),
                    row["Close"]
                )

                inserted += 1
            conn.commit()
            total_rows += inserted

            print(f"{inserted} rows loaded.")

        except Exception as e:
            conn.rollback()
            print(f"Failed : {symbol}")
            print(e)

    cursor.close()
    conn.close()

    print("\n" + "=" * 60)
    print(f"Total Rows Loaded : {total_rows}")
    print("=" * 60)

if __name__ == "__main__":
    load_benchmark_prices()