SET search_path TO risk_platform;

WITH daily_market_value_sum AS (
    SELECT 
    portfolio_id,
    as_of_date,
    SUM(market_value) AS final_value_eod
    FROM holdings
    GROUP BY as_of_date, portfolio_id
),

cumulative_value_sum AS (
    SELECT 
    portfolio_id,
    as_of_date,
    final_value_eod,
    LAG(final_value_eod, 1, final_value_eod) OVER(PARTITION BY portfolio_id ORDER BY as_of_date) AS prev_day_value
    FROM daily_market_value_sum
),

market_calculation AS (
    SELECT 
    portfolio_id,
    as_of_date,
    final_value_eod,
    prev_day_value,
    ROUND((final_value_eod - prev_day_value)*100.0 / prev_day_value, 8) AS daily_return
    FROM previous_day_value
)

SELECT * FROM market_calculation;