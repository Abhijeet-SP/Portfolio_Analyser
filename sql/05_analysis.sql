-- Created empty here; the engine is the only writer. Tableau reads, never computes.
SET search_path TO risk_platform;

-- Day-by-day return series per portfolio. The base the engine derives everything from.
DROP TABLE IF EXISTS daily_returns CASCADE;
CREATE TABLE daily_returns (
    return_id         BIGSERIAL      PRIMARY KEY,
    portfolio_id      INTEGER        NOT NULL,   -- -> portfolio.portfolio_id (FK in constraint.sql)
    return_date       DATE           NOT NULL,
    daily_return      NUMERIC(12,8),             -- null on the first observation (no prior day)
    cumulative_return NUMERIC(18,8),
    benchmark_return  NUMERIC(12,8),

    CONSTRAINT uq_daily_returns_pf_date UNIQUE (portfolio_id, return_date)
);

-- Risk verdict per portfolio, as-of a date, over a window.
DROP TABLE IF EXISTS risk_metrics CASCADE;
CREATE TABLE risk_metrics (
    metric_id         SERIAL         PRIMARY KEY,
    portfolio_id      INTEGER        NOT NULL,   -- -> portfolio.portfolio_id
    benchmark_id      INTEGER        NOT NULL,   -- -> benchmarks.benchmark_id (beta/alpha are relative)
    as_of_date        DATE           NOT NULL,
    period            VARCHAR(10)    NOT NULL,   -- '1M','3M','1Y','ITD', ...
    volatility        NUMERIC(12,8),
    sharpe            NUMERIC(12,8),
    sortino           NUMERIC(12,8),
    beta              NUMERIC(12,8),
    alpha             NUMERIC(12,8),
    information_ratio NUMERIC(12,8),
    max_drawdown      NUMERIC(9,6),              -- fraction, e.g. -0.234500
    var_95            NUMERIC(12,8),
    expected_shortfall NUMERIC(12,8),

    CONSTRAINT uq_risk_pf_bm_date_period UNIQUE (portfolio_id, benchmark_id, as_of_date, period),
    CONSTRAINT ck_risk_period CHECK (period IN ('1M','3M','6M','1Y','3Y','5Y','ITD'))
);

-- Return verdict per portfolio, as-of a date, over a window.
DROP TABLE IF EXISTS performance_metrics CASCADE;
CREATE TABLE performance_metrics (
    perf_id           SERIAL         PRIMARY KEY,
    portfolio_id      INTEGER        NOT NULL,   -- -> portfolio.portfolio_id
    as_of_date        DATE           NOT NULL,
    period            VARCHAR(10)    NOT NULL,
    total_return      NUMERIC(18,8),
    cagr              NUMERIC(12,8),
    cumulative_return NUMERIC(18,8),

    CONSTRAINT uq_perf_pf_date_period UNIQUE (portfolio_id, as_of_date, period),
    CONSTRAINT ck_perf_period CHECK (period IN ('1M','3M','6M','1Y','3Y','5Y','ITD'))
);