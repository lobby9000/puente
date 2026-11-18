-- Gold: one commission per sales rep per month, the table Puente delivers.
-- LEFT JOIN on purpose: sales by a rep who is missing from the plan stay visible
-- with no rate, instead of disappearing from the totals without a trace.
CREATE OR REPLACE TABLE {target}.gold_vendor_commissions AS
SELECT
  s.vendor_code,
  c.store_id,
  c.country,
  c.currency,
  s.period,
  SUM(CASE WHEN s.financed_amount > 0 THEN 1 ELSE 0 END) AS financed_sales,
  CAST(SUM(s.financed_amount) AS DECIMAL(16, 2)) AS financed_amount,
  c.commission_rate,
  CAST(ROUND(SUM(s.financed_amount) * c.commission_rate, 2) AS DECIMAL(16, 2)) AS commission_amount
FROM {target}.silver_sales AS s
LEFT JOIN {target}.silver_comp_plan AS c ON c.vendor_code = s.vendor_code
GROUP BY s.vendor_code, c.store_id, c.country, c.currency, s.period, c.commission_rate;
