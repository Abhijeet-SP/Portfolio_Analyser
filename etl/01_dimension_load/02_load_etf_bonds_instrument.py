from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import yfinance as yf
from etl.db_connection import get_connection

def get_instrument_metadata(ticker: str) -> dict:
# Fetch instrument metadata from Yahoo Finance.
# info have all the data about the equity, we only select what we needc

    stock = yf.Ticker(ticker)
    info = stock.info

    return {
        "ticker": ticker,
        "instrument_name": info.get("longName"),
        "sector": info.get("sector"),
        "asset_type": info.get("quoteType"),
        "currency": info.get("currency"),
    }

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
    # EXCLUDED is their to update the excluded name or updated name. 
    # pass the values to the VALUES(%s, %s, %s, %s, %s)
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
    
    # _ to skip the index and only get the row
    for _, row in tickers.iterrows():
        ticker = row["ticker"]

        try:
            instrument = get_instrument_metadata(ticker)
            upsert_instrument(cursor, instrument)

            conn.commit() 

            success += 1
            print(f"Loaded : {ticker}")

        except Exception as e:
            conn.rollback()   # Reset transaction after a failure
            failed += 1

            print(f"Failed : {ticker}")
            print(e)

    cursor.close()
    conn.close()

    print("\n------------------------------")
    print(f"Successful : {success}")
    print(f"Failed     : {failed}")
    print("------------------------------")


if __name__ == "__main__":
    load_instruments()