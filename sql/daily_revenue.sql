-- bqci: lookback_days=7 max_gib=2
-- Revenue per day and currency for the 7 days ending on @run_date (inclusive).
SELECT
  DATE(o.created_at) AS day,
  o.currency,
  COUNT(*) AS orders,
  SUM(o.amount) AS revenue,
  COUNTIF(o.status = 'refunded') AS refunds
FROM `acme-prod.sales.orders` AS o
WHERE o.created_at >= TIMESTAMP(DATE_SUB(@run_date, INTERVAL 6 DAY))
  AND o.created_at < TIMESTAMP(DATE_ADD(@run_date, INTERVAL 1 DAY))
GROUP BY day, o.currency
