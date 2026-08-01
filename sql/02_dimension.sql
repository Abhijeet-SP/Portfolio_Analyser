-- dimensions/master tables for instruments to reduce data redundancy

SET search_path TO risk_platform;

-- Every priced instrument: equities, ETFs, bonds, etc.
-- One source of truth for each for their investment universe, its sector and assest.
DROP TABLE IF EXISTS instruments CASCADE;
CREATE TABLE instruments (
    instrument_id   SERIAL       PRIMARY KEY,
    ticker          VARCHAR(20)  NOT NULL UNIQUE,     -- market symbol, unique per instrument
    instrument_name VARCHAR(200) NOT NULL,
    sector          VARCHAR(100),                      -- nullable: not all assets have a sector
    asset_type      VARCHAR(50)  NOT NULL DEFAULT 'EQUITY',
    currency        CHAR(3)      NOT NULL DEFAULT 'INR', -- ISO-4217 code

    CONSTRAINT chk_instruments_asset_type CHECK (asset_type IN( 'EQUITY',
                                                                'ETF',
                                                                'BOND_ETF')),
    CONSTRAINT chk_instruments_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_instruments_ticker_nonblank CHECK (length(trim(ticker)) > 0)
);

-- Benchmark index series (e.g. S&P 500) used to storing all the benchmark references.
-- Just like portfolio and instruments, its also hold static data that barely changes.
DROP TABLE IF EXISTS benchmarks CASCADE;
CREATE TABLE benchmarks (
    benchmark_id    SERIAL       PRIMARY KEY,
    symbol          VARCHAR(20)  NOT NULL UNIQUE,
    benchmark_name  VARCHAR(200) NOT NULL,
    currency CHAR(3) NOT NULL DEFAULT 'INR',

    CONSTRAINT chk_benchmark_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT chk_benchmarks_symbol_nonblank CHECK (length(trim(symbol)) > 0)
);

INSERT INTO benchmarks (symbol, benchmark_name, currency)
VALUES
('^NSEI', 'Nifty 50 Index', 'INR'),
('^BSESN', 'BSE Sensex', 'INR'),
('^NSEBANK', 'Nifty Bank Index', 'INR'),
('^CNXIT', 'Nifty IT Index', 'INR'),
('NIFTYBEES.NS', 'Nippon India ETF Nifty BeES', 'INR'),
('BANKBEES.NS', 'Nippon India ETF Bank BeES', 'INR'),
('JUNIORBEES.NS', 'Nippon India ETF Junior BeES', 'INR'),
('GOLDBEES.NS', 'Nippon India ETF Gold BeES', 'INR'),
('LIQUIDBEES.NS', 'Nippon India ETF Liquid BeES', 'INR'),
('CPSEETF.NS', 'CPSE ETF', 'INR')
ON CONFLICT (symbol) DO NOTHING;


DROP TABLE IF EXISTS economic_indicators CASCADE;
CREATE TABLE economic_indicators (
    indicator_id     SERIAL       PRIMARY KEY,
    indicator_code   VARCHAR(50)  NOT NULL UNIQUE,
    indicator_name   VARCHAR(200) NOT NULL,
    unit             VARCHAR(50)  NOT NULL,

    CONSTRAINT chk_indicator_code_nonblank CHECK (length(trim(indicator_code)) > 0),
    CONSTRAINT chk_indicator_name_nonblank CHECK (length(trim(indicator_name)) > 0)
);

INSERT INTO economic_indicators (indicator_code, indicator_name, unit)
VALUES
('REPO_RATE',  'Reserve Bank of India Repo Rate',          'PERCENT'),
('CPI',         'Consumer Price Index (India)',            'INDEX'),
('GDP_GROWTH', 'India GDP Growth Rate',                    'PERCENT'),
('INR_USD',     'Indian Rupee / US Dollar Exchange Rate',  'EXCHANGE_RATE'),
('CRUDE_OIL',   'Brent Crude Oil Price',                   'USD_PER_BARREL'),
('GOLD',        'Gold Spot Price',                         'USD_PER_OUNCE'),
('INDIA_10Y',   'India 10-Year Government Bond Yield',     'PERCENT'),
('US_10Y',      'US 10-Year Treasury Yield',               'PERCENT'),
('INDIA_VIX',   'India Volatility Index',                  'INDEX'),
('USD_INDEX',   'US Dollar Index (DXY)',                   'INDEX')
ON CONFLICT (indicator_code) DO NOTHING;

-- we have to budil our own using python.
-- finding indian database for portfolio and everyday trade is tough
-- will generate my own fabricated protfolios and daily trading till end of 2026
-- for daily report i will only fetch the data till todays date.

-- A portfolio is the container that holds positions and gets analyzed.
-- single source of truth for all the portfolio company manages.
DROP TABLE IF EXISTS portfolios CASCADE;
CREATE TABLE portfolios (
    portfolio_id     SERIAL       PRIMARY KEY,
    portfolio_name   VARCHAR(200) NOT NULL UNIQUE,
    base_currency    CHAR(3)      NOT NULL DEFAULT 'INR', -- currency all metrics roll up to
    inception_date   DATE         NOT NULL,

    CONSTRAINT chk_portfolio_currency CHECK (base_currency ~ '^[A-Z]{3}$')
);

