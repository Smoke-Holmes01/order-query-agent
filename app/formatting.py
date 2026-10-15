"""Deterministic presentation of the current state and actual SQL rows."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.errors import AppError


def json_safe(value: Any):
    if isinstance(value, Decimal):
        return format(value, ".2f")
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    return value


def serialize_rows(rows: list[dict]) -> list[dict]:
    result = []
    for source in rows:
        row = dict(source)
        amount = row.get("sales_amount")
        quantity = row.get("sales_quantity")
        if not isinstance(amount, Decimal):
            raise AppError("QUERY_FAILED", "查询结果金额类型不符合约定。", 500, "result")
        if isinstance(quantity, Decimal):
            if quantity != quantity.to_integral_value():
                raise AppError("QUERY_FAILED", "查询结果销量类型不符合约定。", 500, "result")
            row["sales_quantity"] = int(quantity)
        elif type(quantity) is not int:
            raise AppError("QUERY_FAILED", "查询结果销量类型不符合约定。", 500, "result")
        result.append(json_safe(row))
    return result


def format_answer(state: dict) -> str:
    status = state["status"]
    if status == "needs_clarification":
        return state["clarification"]
    if status == "unsupported":
        return state["clarification"]
    if status == "failed":
        return "查询失败：" + (state.get("safe_error_message") or "服务内部错误，请稍后重试。")
    plan = state["validated_plan"]
    rows = serialize_rows(state["rows"])
    prefix = (
        "以下结果全部来自模拟数据。统计口径：仅已支付订单，按支付时间，人民币；"
        f"Asia/Shanghai 当地日期区间 [{plan.start_date.isoformat()}, {plan.end_date.isoformat()})"
        "（开始包含，结束不含），销售额为成交单价×数量，销量为商品件数。"
    )
    if not rows:
        return prefix + "该区间没有符合口径的记录。"
    if plan.query_type == "daily_sales":
        lines = [f"{row['payment_date']}：销售额 {row['sales_amount']} 元，销量 {row['sales_quantity']}。" for row in rows]
        rule = "按支付日期升序；仅返回有记录的日期，不补零。"
    elif plan.query_type == "category_sales":
        lines = [f"{row['category']}：销售额 {row['sales_amount']} 元，销量 {row['sales_quantity']}。" for row in rows]
        rule = "按销售额降序，金额相同时按品类升序。"
    else:
        metric = "销售额" if plan.rank_by == "sales_amount" else "销量"
        rule = f"按{metric}降序取前 {plan.top_n} 个商品，分数相同时按商品 ID 升序。"
        lines = [
            f"{index}. {row['sku']} {row['product_name']}：销售额 {row['sales_amount']} 元，销量 {row['sales_quantity']}。"
            for index, row in enumerate(rows, 1)
        ]
    return prefix + rule + "\n" + "\n".join(lines)
