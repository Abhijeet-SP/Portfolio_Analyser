"""
Metric math check for etl/04_analysis_load. No DB rows are touched: only the
pure calculation functions are exercised, against values worked out by hand.

Run: python3 tests/test_analysis_metrics.py
"""

from pathlib import Path
import importlib.util
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd


def load_module(relative_path, name):
    """
    The loaders are named 01_..., 02_... so they cannot be imported normally.
    """

    spec = importlib.util.spec_from_file_location(
        name,
        PROJECT_ROOT / relative_path,
    )

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


risk = load_module("etl/04_analysis_load/02_risk_metrics_load.py", "risk_load")
perf = load_module("etl/04_analysis_load/03_performance_metircs_load.py", "perf_load")


def close(actual, expected, tolerance=1e-8):
    return abs(actual - expected) < tolerance


def test_max_drawdown():
    # wealth: 1.10, 0.55, 0.66 -> worst fall is 1.10 -> 0.55
    returns = pd.Series([0.10, -0.50, 0.20])

    assert close(risk.max_drawdown(returns), -0.5), risk.max_drawdown(returns)


def test_flat_series_has_no_volatility():
    returns = pd.Series([0.001] * 30)
    benchmark = pd.Series([float("nan")] * 30)

    metrics = risk.calculate_risk_metrics(returns, benchmark, 0.065)

    assert close(metrics["volatility"], 0.0)
    # zero dispersion -> Sharpe/Sortino undefined, not infinite
    assert metrics["sharpe"] is None
    assert metrics["sortino"] is None
    # no benchmark overlap -> nothing benchmark-relative
    assert metrics["beta"] is None
    assert metrics["alpha"] is None
    assert metrics["information_ratio"] is None


def test_portfolio_equal_to_benchmark():
    # 24 alternating days, enough to clear MIN_OBS
    returns = pd.Series([0.01, -0.008] * 12)

    metrics = risk.calculate_risk_metrics(returns, returns.copy(), 0.065)

    assert close(metrics["beta"], 1.0), metrics["beta"]
    assert close(metrics["alpha"], 0.0), metrics["alpha"]
    # zero tracking error -> information ratio undefined
    assert metrics["information_ratio"] is None


def test_var_and_shortfall_are_left_tail():
    returns = pd.Series([-0.10] + [0.01] * 19)

    metrics = risk.calculate_risk_metrics(returns, pd.Series([float("nan")] * 20), 0.065)

    assert metrics["var_95"] < 0
    # the tail average sits at or below the VaR cut
    assert metrics["expected_shortfall"] <= metrics["var_95"]


def test_cagr_annualises_two_years():
    # 1.21 over 730 days compounds back to 1.10 a year
    assert close(perf.calculate_cagr(0.21, 730, None), 0.1, 1e-9)


def test_cagr_skips_sub_year_periods():
    # a month of returns must not be extrapolated to a yearly rate
    assert perf.calculate_cagr(0.05, 30, 30) is None
    # a wipeout has no real annualised rate
    assert perf.calculate_cagr(-1.0, 730, None) is None


def test_clean_nulls_out_non_finite():
    assert risk.clean(None) is None
    assert risk.clean(float("nan")) is None
    assert risk.clean(float("inf")) is None
    assert risk.clean(0.123456789) == 0.12345679
    # max_drawdown column is NUMERIC(9,6)
    assert risk.clean(-0.1234567, 6) == -0.123457


if __name__ == "__main__":

    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_")
    ]

    for test in tests:
        test()
        print(f"ok : {test.__name__}")

    print(f"\n{len(tests)} passed")
