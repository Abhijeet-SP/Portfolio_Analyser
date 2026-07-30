SET search_path TO risk_platform;

SELECT * FROM benchmarks;
SELECT * FROM instruments;
SELECT * FROM economic_indicator_prices;

SELECT COUNT(*) FROM prices GROUP BY instrument_id;
SELECT indicator_code, COUNT(*) FROM economic_indicator_prices GROUP BY indicator_code;
