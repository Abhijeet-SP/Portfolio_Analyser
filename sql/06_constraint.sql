-- Referential integrity — wire every child table to its parent.
-- Runs LAST: all tables must exist before relationships can be built.
SET search_path TO risk_platform;

-- ---- market layer ----
ALTER TABLE prices
  ADD CONSTRAINT fk_prices_instrument
  FOREIGN KEY (instrument_id) REFERENCES instruments (instrument_id) ON DELETE RESTRICT;

ALTER TABLE benchmark_prices
  ADD CONSTRAINT fk_benchmark_prices_benchmark
  FOREIGN KEY (benchmark_id) REFERENCES benchmarks (benchmark_id) ON DELETE RESTRICT;

-- ---- position layer ----
ALTER TABLE transactions
  ADD CONSTRAINT fk_transactions_portfolio
  FOREIGN KEY (portfolio_id) REFERENCES portfolio (portfolio_id) ON DELETE RESTRICT;
ALTER TABLE transactions
  ADD CONSTRAINT fk_transactions_instrument
  FOREIGN KEY (instrument_id) REFERENCES instruments (instrument_id) ON DELETE RESTRICT;

ALTER TABLE holdings
  ADD CONSTRAINT fk_holdings_portfolio
  FOREIGN KEY (portfolio_id) REFERENCES portfolio (portfolio_id) ON DELETE RESTRICT;
ALTER TABLE holdings
  ADD CONSTRAINT fk_holdings_instrument
  FOREIGN KEY (instrument_id) REFERENCES instruments (instrument_id) ON DELETE RESTRICT;

-- ---- analytics layer ----
ALTER TABLE daily_returns
  ADD CONSTRAINT fk_daily_returns_portfolio
  FOREIGN KEY (portfolio_id) REFERENCES portfolio (portfolio_id) ON DELETE RESTRICT;

ALTER TABLE risk_metrics
  ADD CONSTRAINT fk_risk_metrics_portfolio
  FOREIGN KEY (portfolio_id) REFERENCES portfolio (portfolio_id) ON DELETE RESTRICT;
ALTER TABLE risk_metrics
  ADD CONSTRAINT fk_risk_metrics_benchmark
  FOREIGN KEY (benchmark_id) REFERENCES benchmarks (benchmark_id) ON DELETE RESTRICT;

ALTER TABLE performance_metrics
  ADD CONSTRAINT fk_performance_metrics_portfolio
  FOREIGN KEY (portfolio_id) REFERENCES portfolio (portfolio_id) ON DELETE RESTRICT;