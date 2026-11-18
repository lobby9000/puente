-- Silver: typed columns, and each sale counted once. Exports sometimes repeat a sale
-- event, so only one row per sale_id survives.
CREATE OR REPLACE TABLE {target}.silver_sales AS
SELECT
  sale_id,
  CAST(sale_date AS DATE) AS sale_date,
  substring(sale_date, 1, 7) AS period,
  vendor_code,
  CAST(financed_amount AS DECIMAL(14, 2)) AS financed_amount
FROM {target}.bronze_sales
QUALIFY ROW_NUMBER() OVER (PARTITION BY sale_id ORDER BY sale_date, vendor_code, financed_amount) = 1;

-- The plan is not deduplicated on purpose: a rep listed twice with two different
-- rates is a business conflict, and it has to reach a person instead of being guessed.
CREATE OR REPLACE TABLE {target}.silver_comp_plan AS
SELECT
  vendor_code,
  store_id,
  country,
  currency,
  CAST(NULLIF(commission_rate, '') AS DECIMAL(6, 4)) AS commission_rate
FROM {target}.bronze_comp_plan;
