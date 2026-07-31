from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

def load_portfolio():
    print("Loading portfolio data...")
    portfolios = pd.read_csv("data/03_portfolio_universe.csv")
    # whole data will be directly taken from a csv file.

    for _, row in portfolios.iterrows():
        portfolio = row.to_dict()
        print(portfolio)

load_portfolio()

def load_instruments():
    print("Loading ticker universe...")
    tickers = pd.read_csv("data/01_nifty_500_ticker_universe.csv")

    # _ to skip the index and only get the row
    for _, row in tickers.iterrows():
        ticker = row["ticker"]
        print(ticker)
        
load_instruments()