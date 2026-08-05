-- Referential integrity — wire every child table to its parent.
-- Runs LAST: all tables must exist before relationships can be built.
SET search_path TO risk_platform;

-- ---- market layer ----

ALTER TABLE prices
    DROP CONSTRAINT IF EXISTS fk_prices_instrument,
    ADD CONSTRAINT fk_prices_instrument
        FOREIGN KEY (instrument_id)
        REFERENCES instruments (instrument_id)
        ON DELETE RESTRICT;

ALTER TABLE benchmark_prices
    DROP CONSTRAINT IF EXISTS fk_benchmark_prices_benchmark,
    ADD CONSTRAINT fk_benchmark_prices_benchmark
        FOREIGN KEY (benchmark_id)
        REFERENCES benchmarks (benchmark_id)
        ON DELETE RESTRICT;


-- ---- position layer ----

ALTER TABLE transactions
    DROP CONSTRAINT IF EXISTS fk_transactions_portfolio,
    ADD CONSTRAINT fk_transactions_portfolio
        FOREIGN KEY (portfolio_id)
        REFERENCES portfolios (portfolio_id)
        ON DELETE RESTRICT;

ALTER TABLE transactions
    DROP CONSTRAINT IF EXISTS fk_transactions_instrument,
    ADD CONSTRAINT fk_transactions_instrument
        FOREIGN KEY (instrument_id)
        REFERENCES instruments (instrument_id)
        ON DELETE RESTRICT;

ALTER TABLE holdings
    DROP CONSTRAINT IF EXISTS fk_holdings_portfolio,
    ADD CONSTRAINT fk_holdings_portfolio
        FOREIGN KEY (portfolio_id)
        REFERENCES portfolios (portfolio_id)
        ON DELETE RESTRICT;

ALTER TABLE holdings
    DROP CONSTRAINT IF EXISTS fk_holdings_instrument,
    ADD CONSTRAINT fk_holdings_instrument
        FOREIGN KEY (instrument_id)
        REFERENCES instruments (instrument_id)
        ON DELETE RESTRICT;


-- ---- analytics layer ----

ALTER TABLE daily_returns
    DROP CONSTRAINT IF EXISTS fk_daily_returns_portfolio,
    ADD CONSTRAINT fk_daily_returns_portfolio
        FOREIGN KEY (portfolio_id)
        REFERENCES portfolios (portfolio_id)
        ON DELETE RESTRICT;

ALTER TABLE risk_metrics
    DROP CONSTRAINT IF EXISTS fk_risk_metrics_portfolio,
    ADD CONSTRAINT fk_risk_metrics_portfolio
        FOREIGN KEY (portfolio_id)
        REFERENCES portfolios (portfolio_id)
        ON DELETE RESTRICT;

ALTER TABLE risk_metrics
    DROP CONSTRAINT IF EXISTS fk_risk_metrics_benchmark,
    ADD CONSTRAINT fk_risk_metrics_benchmark
        FOREIGN KEY (benchmark_id)
        REFERENCES benchmarks (benchmark_id)
        ON DELETE RESTRICT;

ALTER TABLE performance_metrics
    DROP CONSTRAINT IF EXISTS fk_performance_metrics_portfolio,
    ADD CONSTRAINT fk_performance_metrics_portfolio
        FOREIGN KEY (portfolio_id)
        REFERENCES portfolios (portfolio_id)
        ON DELETE RESTRICT;

ALTER TABLE portfolio_cashflows
    DROP CONSTRAINT IF EXISTS fk_cashflows_portfolio,
    ADD CONSTRAINT fk_cashflows_portfolio
        FOREIGN KEY (portfolio_id)
        REFERENCES portfolios (portfolio_id)
        ON DELETE RESTRICT;