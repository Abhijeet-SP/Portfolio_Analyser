-- Market data time-series  (daily data access from the market)

SET search_path TO risk_platform;

-- Daily price history per instrument.
-- BIGSERIAL because this is the highest-volume table in the warehouse
-- (n instruments x every trading day) and will blow past 2^31 rows.

DROP TABLE IF EXISTS prices CASCADE;
CREATE TABLE prices (
    price_id      BIGSERIAL     PRIMARY KEY,
    instrument_id INTEGER       NOT NULL,          -- -> instruments.instrument_id (FK in constraint.sql)
    price_date    DATE          NOT NULL,
    adj_close     NUMERIC(18,6) NOT NULL,
    volume        BIGINT        NOT NULL DEFAULT 0,

    CONSTRAINT uq_prices_instrument_date UNIQUE (instrument_id, price_date), -- one price row per instrument per day
    CONSTRAINT ck_prices_adj_close_pos CHECK (adj_close >= 0),
    CONSTRAINT ck_prices_volume_pos    CHECK (volume    >= 0)
);

-- Daily index level per benchmark.
-- Mirrors prices, but references the benchmark master by id — not symbol.
DROP TABLE IF EXISTS benchmark_prices CASCADE;
CREATE TABLE benchmark_prices (
    benchmark_price_id BIGSERIAL      PRIMARY KEY,
    benchmark_id       INTEGER        NOT NULL,   -- -> benchmarks.benchmark_id (FK in constraint.sql)
    price_date         DATE           NOT NULL,
    close              NUMERIC(18,6)  NOT NULL,

    -- one level per benchmark per day
    CONSTRAINT uq_benchmark_prices_bid_date UNIQUE (benchmark_id, price_date),
    CONSTRAINT ck_benchmark_prices_close_pos CHECK (close >= 0)
);

-- Macro / economic time-series (CPI, unemployment, rates, ...).
-- Keyed by a code so many indicators share one narrow table.
DROP TABLE IF EXISTS economic_indicator_prices CASCADE;
CREATE TABLE economic_indicator_prices (
    indicator_id      BIGSERIAL    PRIMARY KEY,
    indicator_code    VARCHAR(50)  NOT NULL,        
    observation_date  DATE         NOT NULL,
    value             NUMERIC(18,6) NOT NULL,

    -- one observation per indicator per date
    CONSTRAINT uq_econ_code_date UNIQUE (indicator_code, observation_date),
    CONSTRAINT chk_indicator_code_nonblank CHECK(length(trim(indicator_code)) > 0)
);


/*
-- Composite index tuned for the common query "give me one instrument's
-- series over a date range" (instrument_id equality + price_date range/order).
CREATE INDEX ix_prices_instrument_date ON prices (instrument_id, price_date DESC);
-- Standalone date index for cross-sectional "everything on day X" scans.
CREATE INDEX ix_prices_date         ON prices (price_date);


CREATE INDEX ix_benchmark_prices_bid_date ON benchmark_prices (benchmark_id, price_date DESC);
*/ 