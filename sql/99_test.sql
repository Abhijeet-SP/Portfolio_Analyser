SET search_path TO risk_platform;

SELECT * FROM benchmarks;
SELECT * FROM instruments;
SELECT * FROM economic_indicator_prices;

SELECT COUNT(*) FROM prices GROUP BY instrument_id;
SELECT indicator_code, COUNT(*) FROM economic_indicator_prices GROUP BY indicator_code;

SELECT COUNT(*) FROM transactions;
SELECT COUNT(*) FROM holdings;
SELECT * FROM portfolios;

SELECT
    MIN(as_of_date),
    MAX(as_of_date),
    COUNT(DISTINCT as_of_date)
FROM holdings;

SELECT
    MIN(txn_date),
    MAX(txn_date),
    COUNT(DISTINCT txn_date)
FROM transactions;