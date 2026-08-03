SET search_path TO risk_platform;

WITH daily_market_value AS (
    SELECT
        portfolio_id,
        as_of_date,
        SUM(market_value) AS final_value_eod
    FROM holdings
    GROUP BY portfolio_id, as_of_date
),

portfolio_values AS (
    SELECT
        dmv.portfolio_id,
        dmv.as_of_date,
        dmv.final_value_eod,

        LAG(dmv.final_value_eod)
            OVER (
                PARTITION BY dmv.portfolio_id
                ORDER BY dmv.as_of_date
            ) AS prev_day_value,

        COALESCE(pcf.net_cash_flow, 0) AS net_cash_flow,
        COALESCE(pcf.dividend_income, 0) AS dividend_income

    FROM daily_market_value dmv

    LEFT JOIN portfolio_cashflows pcf
        ON dmv.portfolio_id = pcf.portfolio_id
       AND dmv.as_of_date = pcf.flow_date
),

daily_returns AS (
    SELECT
        portfolio_id,
        as_of_date,
        final_value_eod,
        prev_day_value,
        net_cash_flow,
        dividend_income,

        CASE
            WHEN prev_day_value IS NULL THEN NULL
            WHEN (prev_day_value + net_cash_flow) <= 0 THEN NULL
            ELSE ROUND(
                (
                    final_value_eod
                    + dividend_income
                    - prev_day_value
                    - net_cash_flow
                )
                /
                (
                    prev_day_value
                    + net_cash_flow
                ),
                8
            )
        END AS daily_return

    FROM portfolio_values
),

final_returns AS (
    SELECT
        portfolio_id,
        as_of_date,
        final_value_eod,
        prev_day_value,
        net_cash_flow,
        dividend_income,
        daily_return,

        ROUND(
            EXP(
                SUM(
                    LN(1 + daily_return)
                ) OVER (
                    PARTITION BY portfolio_id
                    ORDER BY as_of_date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                )
            ) - 1,
            8
        ) AS cumulative_return

    FROM daily_returns
)

SELECT
    portfolio_id,
    as_of_date,
    final_value_eod,
    prev_day_value,
    net_cash_flow,
    dividend_income,
    daily_return,
    cumulative_return
FROM final_returns
ORDER BY portfolio_id, as_of_date;