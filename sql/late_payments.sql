-- bqci: lookback_days=120 max_gib=5
-- Invoices issued in the last 120 days that are more than 30 days past due on @run_date and not fully paid.
WITH paid AS (
  SELECT
    p.invoice_id,
    SUM(p.amount) AS paid_amount
  FROM `acme-prod.billing.payments` AS p
  WHERE p.paid_on <= @run_date
    AND p.paid_on >= DATE_SUB(@run_date, INTERVAL 120 DAY)
  GROUP BY p.invoice_id
)
SELECT
  i.invoice_id,
  i.customer_id,
  i.due_on,
  i.amount,
  IFNULL(p.paid_amount, 0) AS paid_amount,
  DATE_DIFF(@run_date, i.due_on, DAY) AS days_overdue
FROM `acme-prod.billing.invoices` AS i
LEFT JOIN paid AS p
  ON p.invoice_id = i.invoice_id
WHERE i.issued_on >= DATE_SUB(@run_date, INTERVAL 120 DAY)
  AND i.due_on < DATE_SUB(@run_date, INTERVAL 30 DAY)
  AND IFNULL(p.paid_amount, 0) < i.amount
