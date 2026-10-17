-- bqci: lookback_days=730 max_gib=140
-- Lifetime value per customer from the last two years of orders. Customers with no orders still appear (LEFT JOIN),
-- with zero value, and cancelled orders do not count.
SELECT
  c.customer_id,
  c.name,
  IFNULL(SUM(o.amount), 0) AS lifetime_value,
  COUNT(o.order_id) AS orders,
  SAFE_DIVIDE(SUM(o.amount), COUNT(o.order_id)) AS avg_order_value,
  MAX(o.created_at) AS last_order_at
FROM `acme-prod.sales.customers` AS c
LEFT JOIN `acme-prod.sales.orders` AS o
  ON o.customer_id = c.customer_id
  AND o.status != 'cancelled'
  AND o.created_at >= TIMESTAMP(DATE_SUB(@run_date, INTERVAL 729 DAY))
GROUP BY c.customer_id, c.name
