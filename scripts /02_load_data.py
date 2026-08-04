from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]

ETL_DIR = PROJECT_ROOT / "etl"

# ETL scripts in execution order. Dimensions first (later loads reference
# their ids), then market prices, then position history, then the derived
# analysis tables which read everything above.
#
# 00_generate_data is deliberately absent: those scripts rewrite the
# synthetic CSVs in data/. Run them by hand when new data is wanted.
ETL_SCRIPTS = [
    "01_dimension_load/01_load_equity_instruments.py",
    "01_dimension_load/02_load_etf_bonds_instrument.py",
    "01_dimension_load/03_load_portfolio.py",

    "02_market_load/01_load_prices.py",
    "02_market_load/02_load_benchmark_prices.py",
    "02_market_load/03_load_economic_inicators.py",

    "03_history_load/01_load_transcation.py",
    "03_history_load/02_load_holdings.py",
    "03_history_load/03_load_cash_flow.py",

    "04_analysis_load/01_daily_returns_load.py",
    "04_analysis_load/02_risk_metrics_load.py",
    "04_analysis_load/03_performance_metircs_load.py",
]


def run_etl_script(script):
    """
    Each loader owns its own connection, error log and commit boundary, so it
    runs as its own process. Returns True when it exited cleanly.
    """

    print("\n" + "#" * 60, flush=True)
    print(f"# {script}", flush=True)
    print("#" * 60, flush=True)

    result = subprocess.run(
        [sys.executable, str(ETL_DIR / script)],
        cwd=PROJECT_ROOT,
    )

    return result.returncode == 0


def run_data_load():

    completed = []

    for script in ETL_SCRIPTS:

        if not run_etl_script(script):

            # Later stages read what earlier ones write, so a crash stops
            # the chain rather than loading on top of missing data.
            print("\n" + "!" * 60)
            print(f"Aborted at : {script}")
            print(f"Completed  : {len(completed)}/{len(ETL_SCRIPTS)}")
            print("Fix the failure above, then re-run. Loads are")
            print("incremental, so finished stages will skip themselves.")
            print("!" * 60)

            sys.exit(1)

        completed.append(script)

    print("\n" + "=" * 60)
    print(f"Data load completed successfully. {len(completed)} scripts run.")
    print("Row-level failures, if any, are in reports/.")
    print("=" * 60)


if __name__ == "__main__":
    run_data_load()
