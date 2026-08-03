
SET search_path TO risk_platform;

-- Event log: every buy/sell/etc. Append-only, never updated.
DROP TABLE IF EXISTS transactions CASCADE;
CREATE TABLE transactions (
    transaction_id SERIAL         PRIMARY KEY,
    portfolio_id   INTEGER        NOT NULL,   -- -> portfolio.portfolio_id (FK in constraint.sql)
    instrument_id  INTEGER        NOT NULL,   -- -> instruments.instrument_id  (FK in constraint.sql)
    txn_date       DATE           NOT NULL,
    txn_type       VARCHAR(10)    NOT NULL,
    quantity       NUMERIC(18,6)  NOT NULL DEFAULT 0,  -- positive magnitude; direction is in txn_type
    price          NUMERIC(18,6)  NOT NULL DEFAULT 0,  -- execution price per share
    fees           NUMERIC(18,6)  NOT NULL DEFAULT 0,
    amount         NUMERIC(18,6),                       -- net cash impact (esp. for dividends, qty = 0)

    CONSTRAINT ck_txn_type    CHECK (txn_type IN ('BUY','SELL','DIVIDEND')),
    CONSTRAINT ck_txn_qty_pos CHECK (quantity >= 0),
    CONSTRAINT ck_txn_price_pos CHECK (price >= 0),
    CONSTRAINT ck_txn_fees_pos  CHECK (fees  >= 0)
);

-- Snapshot: what the portfolio holds as of a given date (a slowly-changing
-- fact, one row per position per snapshot date).
DROP TABLE IF EXISTS holdings CASCADE;
CREATE TABLE holdings (
    holding_id    SERIAL        PRIMARY KEY,
    portfolio_id  INTEGER       NOT NULL,         -- -> portfolio.portfolio_id
    instrument_id INTEGER       NOT NULL,         -- -> instruments.instrument_id
    as_of_date    DATE          NOT NULL,
    quantity     NUMERIC(18,6) NOT NULL,
    avg_cost     NUMERIC(18,6),
    market_value NUMERIC(18,4) NOT NULL,
    weight       NUMERIC(9,6),                   -- fraction of portfolio, 0..1

    -- one row per position per snapshot: prevents duplicate snapshots
    CONSTRAINT uq_holdings_pf_inst_date UNIQUE (portfolio_id, instrument_id, as_of_date),
    CONSTRAINT ck_holdings_qty_nonneg CHECK (quantity >= 0)
);

DROP TABLE IF EXISTS portfolio_cashflows CASCADE;
CREATE TABLE portfolio_cashflows (
    cashflow_id      BIGSERIAL      PRIMARY KEY,
    portfolio_id     INTEGER        NOT NULL,   -- -> portfolio.portfolio_id
    flow_date        DATE           NOT NULL,
    buy_flow         NUMERIC(18,4)  NOT NULL DEFAULT 0,
    sell_flow        NUMERIC(18,4)  NOT NULL DEFAULT 0,
    net_cash_flow    NUMERIC(18,4)  NOT NULL DEFAULT 0,
    dividend_income  NUMERIC(18,4)  NOT NULL DEFAULT 0,
    buy_count        INTEGER        NOT NULL DEFAULT 0,
    sell_count       INTEGER        NOT NULL DEFAULT 0,
    dividend_count   INTEGER        NOT NULL DEFAULT 0,

    -- One summary row per portfolio per day
    CONSTRAINT uq_cashflows_pf_date UNIQUE (portfolio_id, flow_date),

    -- Monetary values cannot be negative
    CONSTRAINT ck_buy_flow_nonneg CHECK (buy_flow >= 0),
    CONSTRAINT ck_sell_flow_nonneg CHECK (sell_flow >= 0),
    CONSTRAINT ck_dividend_income_nonneg CHECK (dividend_income >= 0),

    -- Counts cannot be negative
    CONSTRAINT ck_buy_count_nonneg CHECK (buy_count >= 0),
    CONSTRAINT ck_sell_count_nonneg CHECK (sell_count >= 0),
    CONSTRAINT ck_dividend_count_nonneg CHECK (dividend_count >= 0),

    -- Ensure the stored net flow is consistent
    CONSTRAINT ck_net_cash_flow CHECK (net_cash_flow = buy_flow - sell_flow)
);

/*
-- Pull the ledger for a portfolio in date order (statements, replays).
CREATE INDEX ix_txn_portfolio_date ON transactions (portfolio_id, txn_date);
-- Trace all activity in a single instrument across portfolios.
CREATE INDEX ix_txn_instrument     ON transactions (instrument_id);


-- Reconstruct a full portfolio snapshot for one date.
CREATE INDEX ix_holdings_portfolio_date ON holdings (portfolio_id, as_of_date DESC);
*/