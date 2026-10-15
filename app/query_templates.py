"""Only this module selects business SQL. No free SQL interface exists."""

from datetime import datetime, time

from app.errors import AppError
from app.schemas import ValidatedPlan


DAILY_SALES = """SELECT DATE(o.paid_at) AS payment_date,
       SUM(i.quantity * i.unit_price) AS sales_amount,
       SUM(i.quantity) AS sales_quantity
FROM orders AS o
JOIN order_items AS i ON i.order_id = o.id
WHERE o.status = 'paid' AND o.paid_at >= %s AND o.paid_at < %s
GROUP BY DATE(o.paid_at)
ORDER BY payment_date ASC
LIMIT %s"""

CATEGORY_SALES = """SELECT p.category,
       SUM(i.quantity * i.unit_price) AS sales_amount,
       SUM(i.quantity) AS sales_quantity
FROM orders AS o
JOIN order_items AS i ON i.order_id = o.id
JOIN products AS p ON p.id = i.product_id
WHERE o.status = 'paid' AND o.paid_at >= %s AND o.paid_at < %s
GROUP BY p.category
ORDER BY sales_amount DESC, p.category ASC
LIMIT %s"""

TOP_PRODUCTS_AMOUNT = """SELECT p.id AS product_id, p.sku, p.name AS product_name, p.category,
       SUM(i.quantity * i.unit_price) AS sales_amount,
       SUM(i.quantity) AS sales_quantity
FROM orders AS o
JOIN order_items AS i ON i.order_id = o.id
JOIN products AS p ON p.id = i.product_id
WHERE o.status = 'paid' AND o.paid_at >= %s AND o.paid_at < %s
GROUP BY p.id, p.sku, p.name, p.category
ORDER BY sales_amount DESC, p.id ASC
LIMIT %s"""

TOP_PRODUCTS_QUANTITY = """SELECT p.id AS product_id, p.sku, p.name AS product_name, p.category,
       SUM(i.quantity * i.unit_price) AS sales_amount,
       SUM(i.quantity) AS sales_quantity
FROM orders AS o
JOIN order_items AS i ON i.order_id = o.id
JOIN products AS p ON p.id = i.product_id
WHERE o.status = 'paid' AND o.paid_at >= %s AND o.paid_at < %s
GROUP BY p.id, p.sku, p.name, p.category
ORDER BY sales_quantity DESC, p.id ASC
LIMIT %s"""


def select_template(plan: ValidatedPlan) -> tuple[str, str, tuple]:
    """Dates are naive local midnight by the documented DATETIME convention."""
    if not 0 < (plan.end_date - plan.start_date).days <= 90:
        raise AppError("INTERNAL_ERROR", "查询计划没有通过服务端范围校验。", 500)
    bounds = (datetime.combine(plan.start_date, time.min), datetime.combine(plan.end_date, time.min))
    if plan.query_type == "daily_sales" and plan.rank_by is None and plan.top_n is None:
        return "daily_sales_v1", DAILY_SALES, (*bounds, 90)
    if plan.query_type == "category_sales" and plan.rank_by is None and plan.top_n is None:
        return "category_sales_v1", CATEGORY_SALES, (*bounds, 101)
    if plan.query_type == "top_products" and type(plan.top_n) is int and 1 <= plan.top_n <= 10:
        if plan.rank_by == "sales_amount":
            return "top_products_amount_v1", TOP_PRODUCTS_AMOUNT, (*bounds, plan.top_n)
        if plan.rank_by == "sales_quantity":
            return "top_products_quantity_v1", TOP_PRODUCTS_QUANTITY, (*bounds, plan.top_n)
    raise AppError("INTERNAL_ERROR", "查询计划没有匹配固定模板。", 500)
