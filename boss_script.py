"""
One-command runner for the whole project.

    python3 boss_script.py              setup the database, then run the full ETL
    python3 boss_script.py --generate   regenerate the synthetic CSVs first
    python3 boss_script.py --setup-only just create the schema
    python3 boss_script.py --load-only  just run the ETL

Every stage is an existing script run as a subprocess, so each keeps its own
logging and error handling. The run stops at the first failing stage; loads are
incremental, so a re-run skips whatever already finished.
"""

from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parent

SCRIPTS_DIR = PROJECT_ROOT / "scripts "  # trailing space is the real directory name

GENERATE = [
    PROJECT_ROOT / "data_scripts/00_generate_data/01_generate_synthetic_eod.py",
    PROJECT_ROOT / "data_scripts/00_generate_data/02_generate_portfolio_cashflows.py",
]

SETUP = SCRIPTS_DIR / "01_setup_database.py"
LOAD = SCRIPTS_DIR / "02_load_data.py"


def run(script):

    print("\n" + "=" * 70, flush=True)
    print(f"= {script.relative_to(PROJECT_ROOT)}", flush=True)
    print("=" * 70, flush=True)

    if not script.exists():
        print(f"Missing script : {script}")
        return False

    return subprocess.run(
        [sys.executable, str(script)],
        cwd=PROJECT_ROOT,
    ).returncode == 0


def main():

    flags = set(sys.argv[1:])

    unknown = flags - {"--generate", "--setup-only", "--load-only"}
    if unknown:
        print(f"Unknown flag(s) : {', '.join(sorted(unknown))}")
        print(__doc__)
        return 2

    stages = []

    if "--generate" in flags:
        stages += GENERATE
    if "--load-only" not in flags:
        stages.append(SETUP)
    if "--setup-only" not in flags:
        stages.append(LOAD)

    for i, script in enumerate(stages, 1):
        if not run(script):
            print("\n" + "!" * 70)
            print(f"Aborted at stage {i}/{len(stages)} : {script.name}")
            print("Fix the failure above, then re-run. Row-level failures are")
            print("in reports/; loads are incremental so finished work is skipped.")
            print("!" * 70)
            return 1

    print("\n" + "=" * 70)
    print(f"All done. {len(stages)} stage(s) completed.")
    print("=" * 70)

    if "--generate" in flags:
        # the generator writes prices.csv / transactions.csv / holdings.csv,
        # while the ETL reads 04_prices_universe.csv / 05_..._universe.csv /
        # 06_..._universe.csv — rename before the load picks them up
        print("Note: rename the generated prices/transactions/holdings CSVs to")
        print("their 04_/05_/06_*_universe.csv names before loading them.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
