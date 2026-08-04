from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import date, timedelta
from io import StringIO

import pandas as pd
import requests
import yfinance as yf

from etl.db_connection import get_connection
from etl.error_logger import (
    start_log,
    log_error,
    end_log,
)

BACKFILL_DAYS = 365


MARKET_INDICATORS = {
    "INR_USD":   "USDINR=X",   # Indian Rupee / US Dollar spot
    "CRUDE_OIL": "BZ=F",       # Brent Crude, continuous futures
    "GOLD":      "GC=F",       # COMEX Gold, continuous futures
    "US_10Y":    "^TNX",       # CBOE 10Y Treasury yield index.
                                # NOTE: via yfinance/Yahoo this comes
                                # back as the actual yield (e.g. 4.63),
                                # NOT yield*10 like some other vendors
                                # quote it. Don't divide by 10 here.
    "INDIA_VIX": "^INDIAVIX",  # NSE India VIX
    "USD_INDEX": "DX-Y.NYB",   # ICE US Dollar Index (DXY proxy)
}


def get_last_indicator_date(cursor, indicator_code):

    cursor.execute(
        """
        SELECT MAX(observation_date)
        FROM economic_indicator_prices
        WHERE indicator_code = %s;
        """,
        (indicator_code,),
    )

    return cursor.fetchone()[0]


def get_download_start(last_date):
    """
    Resume the day after what is already stored, or fall back to a full
    backfill for an indicator that has never been loaded.
    """

    if last_date is None:
        return date.today() - timedelta(days=BACKFILL_DAYS)

    return last_date + timedelta(days=1)


def download_yfinance_series(symbol, start_date):
    # Yahoo treats end as exclusive, so today needs tomorrow's date to be
    # included. Without this the 16:15 run never picks up today's close.
    end_date = date.today() + timedelta(days=1)

    data = yf.download(
    symbol,
    start=start_date,
    end=end_date,
    progress=False,
    auto_adjust=False
)

    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    return data


def load_market_indicators(conn, cursor, log_file):
    success = 0
    failed = 0
    skipped = 0
    total_rows = 0

    print("\n" + "-" * 60)
    print("1. Market-Quoted Indicators (yfinance)")
    print("-" * 60)

    for indicator_code, symbol in MARKET_INDICATORS.items():

        try:
            last_date = get_last_indicator_date(cursor, indicator_code)
            start_date = get_download_start(last_date)

            if start_date > date.today():
                print(f"\n{indicator_code} : already up to date.")
                skipped += 1
                continue

            print(f"\nDownloading {indicator_code} ({symbol}) from {start_date}")

            prices = download_yfinance_series(symbol, start_date)

            if prices.empty:
                # An empty window on an already-loaded series just means no
                # quotes since last_date (weekend, holiday). Empty on a
                # first-ever load is a real download failure.
                if last_date is not None:
                    print(f"No new data : {indicator_code}")
                    skipped += 1
                    continue

                failed += 1

                print(f"No data found : {indicator_code}")

                log_error(
                    log_file=log_file,
                    ticker=indicator_code,
                    error="No data found on first load",
                )

                continue

            inserted = 0

            for price_date, row in prices.iterrows():
                close = row["Close"]

                # Occasional NaN on illiquid days / index holidays
                if pd.isna(close):
                    continue

                upsert_indicator_value(
                    cursor,
                    indicator_code,
                    price_date.date(),
                    close
                )
                inserted += 1

            conn.commit()

            success += 1
            total_rows += inserted

            print(f"{inserted} rows loaded.")

        except Exception as e:
            conn.rollback()
            failed += 1

            print(f"Failed : {indicator_code}")
            print(e)

            log_error(
                log_file=log_file,
                ticker=indicator_code,
                error=e,
            )

    if skipped:
        print(f"\n{skipped} market indicator(s) already current.")

    # Sections all report (handled, failed, rows); an already-current
    # indicator was handled fine, so it counts there and not as a failure.
    return success + skipped, failed, total_rows


FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

# Already a percent value -- load as-is.
FRED_LEVEL_SERIES = {
    "INDIA_10Y": "INDIRLTLT01STM",  # OECD, India 10Y G-Sec yield, monthly
}

# Raw index/level -- we compute YoY growth ourselves before loading.
FRED_GROWTH_SERIES = {
    "GDP_GROWTH": "NGDPRNSAXDCINQ",  # IMF, India real GDP, quarterly
}


def fetch_fred_series(series_id):
    url = FRED_CSV_URL.format(series_id=series_id)
    response = requests.get(url, timeout=30)
    response.raise_for_status()

    df = pd.read_csv(StringIO(response.text))
    df.columns = ["observation_date", "value"]
    df["observation_date"] = pd.to_datetime(df["observation_date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")

    return df.dropna(subset=["value"]).sort_values("observation_date")


def load_fred_level_indicators(conn, cursor, log_file):
    success = 0
    failed = 0
    total_rows = 0

    cutoff = pd.Timestamp(date.today()) - pd.Timedelta(days=BACKFILL_DAYS)

    for indicator_code, series_id in FRED_LEVEL_SERIES.items():
        print(f"\nDownloading {indicator_code} ({series_id})")

        try:
            df = fetch_fred_series(series_id)
            df = df[df["observation_date"] >= cutoff]

            if df.empty:
                failed += 1

                print(f"No data found : {indicator_code}")

                log_error(
                    log_file=log_file,
                    ticker=indicator_code,
                    error="No data found",
                )

                continue

            inserted = 0

            for _, row in df.iterrows():
                upsert_indicator_value(
                    cursor,
                    indicator_code,
                    row["observation_date"].date(),
                    row["value"],
                )
                inserted += 1

            conn.commit()

            success += 1
            total_rows += inserted

            print(f"{inserted} rows loaded.")

        except Exception as e:
            conn.rollback()
            failed += 1

            print(f"Failed : {indicator_code}")
            print(e)

            log_error(
                log_file=log_file,
                ticker=indicator_code,
                error=e,
            )

    return success, failed, total_rows


def load_fred_growth_indicators(conn, cursor, log_file):
    success = 0
    failed = 0
    total_rows = 0

    cutoff = pd.Timestamp(date.today()) - pd.Timedelta(days=BACKFILL_DAYS)

    for indicator_code, series_id in FRED_GROWTH_SERIES.items():
        print(f"\nDownloading {indicator_code} ({series_id})")

        try:
            df = fetch_fred_series(series_id)

            # Quarterly YoY growth: compare each quarter to the same
            # quarter 4 rows earlier (index is already date-sorted).
            df["yoy_growth"] = df["value"].pct_change(periods=4) * 100
            df = df.dropna(subset=["yoy_growth"])
            df = df[df["observation_date"] >= cutoff]

            if df.empty:
                failed += 1

                print(f"No data found : {indicator_code}")

                log_error(
                    log_file=log_file,
                    ticker=indicator_code,
                    error="No data found",
                )

                continue

            inserted = 0

            for _, row in df.iterrows():
                upsert_indicator_value(
                    cursor,
                    indicator_code,
                    row["observation_date"].date(),
                    row["yoy_growth"],
                )
                inserted += 1

            conn.commit()

            success += 1
            total_rows += inserted

            print(f"{inserted} rows loaded.")

        except Exception as e:
            conn.rollback()
            failed += 1

            print(f"Failed : {indicator_code}")
            print(e)

            log_error(
                log_file=log_file,
                ticker=indicator_code,
                error=e,
            )

    return success, failed, total_rows



REPO_RATE_HISTORY = [

    (date(2025, 10, 1), 5.50),
    (date(2025, 12, 5), 5.25),
    # TODO: add each new MPC outcome here as it's announced
]

CPI_HISTORY = [
    # (month_date, cpi_yoy_percent) -- fill from the MOSPI press
    # release each month, e.g.:
    # (date(2026, 6, 1), 2.1),
]


def seed_manual_indicators(conn, cursor, log_file):
    print("\n" + "-" * 60)
    print("3. Manually-Seeded Indicators (CPI, REPO_RATE)")
    print("-" * 60)

    success = 0
    failed = 0
    total_rows = 0

    try:
        if not REPO_RATE_HISTORY and not CPI_HISTORY:
            failed += 1

            print("No manual indicator data found.")

            log_error(
                log_file=log_file,
                ticker="MANUAL_INDICATORS",
                error="No data found",
            )

            return success, failed, total_rows

        for effective_date, rate in REPO_RATE_HISTORY:
            upsert_indicator_value(
                cursor,
                "REPO_RATE",
                effective_date,
                rate,
            )
            total_rows += 1

        for month_date, value in CPI_HISTORY:
            upsert_indicator_value(
                cursor,
                "CPI",
                month_date,
                value,
            )
            total_rows += 1

        conn.commit()

        success = 1

        print(f"{total_rows} rows loaded.")

    except Exception as e:
        conn.rollback()
        failed = 1

        print("Failed to seed manual indicators")
        print(e)

        log_error(
            log_file=log_file,
            ticker="MANUAL_INDICATORS",
            error=e,
        )

    return success, failed, total_rows


# ---------------------------------------------------------------------------
# Shared upsert
# ---------------------------------------------------------------------------

def upsert_indicator_value(cursor, indicator_code, observation_date, value):
    query = """
    INSERT INTO economic_indicator_prices
    (
        indicator_code,
        observation_date,
        value
    )

    VALUES
    ( %s, %s, %s )

    ON CONFLICT
    (
        indicator_code,
        observation_date
    )

    DO UPDATE
    SET
        value = EXCLUDED.value;
    """

    cursor.execute(
        query,
        (
            indicator_code,
            observation_date,
            float(value)
        )
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def load_economic_indicators():

    print("=" * 60)
    print("Loading Economic Indicators")
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

    try:
        s, f, rows = load_market_indicators(conn, cursor, log_file)
        success += s
        failed += f
        total_rows += rows

        s, f, rows = load_fred_level_indicators(conn, cursor, log_file)
        success += s
        failed += f
        total_rows += rows

        s, f, rows = load_fred_growth_indicators(conn, cursor, log_file)
        success += s
        failed += f
        total_rows += rows

        s, f, rows = seed_manual_indicators(conn, cursor, log_file)
        success += s
        failed += f
        total_rows += rows

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
    print(f"Successful Sources : {success}")
    print(f"Failed Sources     : {failed}")
    print(f"Total Rows Loaded  : {total_rows}")
    print("=" * 60)

if __name__ == "__main__":
    load_economic_indicators()