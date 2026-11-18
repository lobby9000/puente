-- Bronze: the files exactly as they arrived. Every column stays text, so nothing is
-- lost or coerced at this layer and a bad value can always be traced to its file.
CREATE OR REPLACE TABLE {target}.bronze_sales AS
SELECT sale_id, sale_date, vendor_code, financed_amount
FROM read_files(
  '{volume}/sales_raw.csv',
  format => 'csv',
  header => true,
  schema => 'sale_id STRING, sale_date STRING, vendor_code STRING, financed_amount STRING'
);

CREATE OR REPLACE TABLE {target}.bronze_comp_plan AS
SELECT vendor_code, store_id, country, currency, commission_rate
FROM read_files(
  '{volume}/comp_plan.csv',
  format => 'csv',
  header => true,
  schema => 'vendor_code STRING, store_id STRING, country STRING, currency STRING, commission_rate STRING'
);
