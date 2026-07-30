SET search_path TO risk_platform;

SELECT * FROM benchmarks;
SELECT * FROM instruments;
SELECT * FROM economic_indicator_prices;

SELECT COUNT(*) FROM prices GROUP BY instrument_id;
SELECT indicator_code, COUNT(*) FROM economic_indicator_prices GROUP BY indicator_code;

SELECT
    indicator_code,
    MIN(observation_date) AS first_date,
    MAX(observation_date) AS last_date,
    COUNT(*) AS rows
FROM economic_indicator_prices
GROUP BY indicator_code
ORDER BY indicator_code;