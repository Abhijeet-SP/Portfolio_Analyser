from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import date, timedelta
import yfinance as yf
from etl.db_connection import get_connection

BACKFILL_DAYS = 365

def get_instruments(cursor):
#  Returns: List of (instruments_id, ticker)

    cursor.execute("""
        SELECT
            instrument_id,
            ticker
        FROM instruments
        ORDER BY instrument_id;
    """)

    return cursor.fetchall()

def upsert_price(cursor,
                 instrument_id,
                 price_date,
                 adj_close,
                 volume):
    
# Update the daily prices in the table prices wrt to the master reference table
    query = """
    INSERT INTO prices
    (
        instrument_id,
        price_date,
        adj_close,
        volume
    )

    VALUES
    (%s, %s, %s,%s)

    ON CONFLICT
    (   instrument_id,
        price_date)

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
            int(volume)
        )

    )

# downlaod the data from yahoo finance
def download_prices(ticker):

    end_date = date.today()
    start_date = end_date - timedelta(days=BACKFILL_DAYS)

    data = yf.download(
        ticker,
        start=start_date,
        end=end_date,
        progress=False,
        auto_adjust=False
    )

    return data

# load the prices
def load_prices():

    print("=" * 60)
    print("Loading Historical Prices")
    print("=" * 60)

    conn = get_connection()
    cursor = conn.cursor()

    instruments = get_instruments(cursor)
    total_loaded = 0

    for instrument_id, ticker in instruments:

        print(f"\nDownloading {ticker}...")

        try:
            prices = download_prices(ticker)
            if prices.empty:
                print("No data found.")
                continue
            inserted = 0

            for price_date, row in prices.iterrows():

                upsert_price(
                    cursor,
                    instrument_id,
                    price_date.date(),
                    row["Adj Close"],
                    row["Volume"]
                )
                inserted += 1

            conn.commit()
            total_loaded += inserted
            print(f"{inserted} rows loaded.")

        except Exception as e:

            conn.rollback()
            print(f"Failed: {ticker}")
            print(e)

    cursor.close()
    conn.close()

    print("\n" + "=" * 60)
    print(f"Total rows loaded : {total_loaded}")
    print("=" * 60)

if __name__ == "__main__":
    load_prices()