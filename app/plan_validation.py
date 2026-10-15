"""Local business rules. No model decision grants database access by itself."""

from dataclasses import dataclass
from datetime import date, timedelta
import re

from app.schemas import CandidatePlan, DecisionType, ValidatedPlan


DATE_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
FOLLOWUP = "请将原问题和补充条件一起重新提交；本服务没有跨请求记忆。"
SUPPORTED = "仅支持已支付订单的每日销售、品类销售及按销售额或销量的商品排行。"


@dataclass(frozen=True)
class ValidationResult:
    status: str
    plan: ValidatedPlan | None = None
    message: str = ""


def unsupported_reason(question: str) -> str | None:
    """Conservative guards for explicit out-of-scope instructions, plus schema isolation."""
    forbidden = (
        "退款", "退货", "预测", "利润", "推荐", "导出", "数据库管理", "订单明细",
        "删除", "删掉", "清空", "修改订单", "更新订单", "写入", "插入", "建表", "删表",
        "忽略规则", "忽略以上", "忽略之前", "输出SQL", "生成SQL", "执行SQL", "拼接SQL",
        "原因", "为什么", "同比", "环比", "增长率", "用户偏好",
        "只看", "仅看", "只统计", "仅统计", "筛选", "品类为", "商品为", "订单状态为",
        "数码", "家居", "键盘", "鼠标", "杯子", "渠道", "店铺", "地区", "用户", "客户",
        "城市", "省份", "订单状态", "未支付", "已取消", "取消订单", "未付款",
    )
    compact = re.sub(r"\s+", "", question).upper()
    for supported_paid_scope in ("只统计已支付订单", "仅统计已支付订单", "只看已支付订单", "仅看已支付订单"):
        compact = compact.replace(supported_paid_scope, "")
    if any(word.upper() in compact for word in forbidden):
        return SUPPORTED + "不支持写操作、任意筛选、SQL 指令或原因推断。"
    if re.search(r"\b(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT|CREATE)\b", question, re.I):
        return SUPPORTED + "不接受 SQL 或数据库操作指令。"
    if re.search(r"ignore\s+(?:all\s+)?(?:previous|rules|instructions)", question, re.I):
        return SUPPORTED + "不能通过用户指令扩大权限。"
    if re.search(r"\bP[123]\b|\b(?:unpaid|cancelled)\b", question, re.I):
        return SUPPORTED + "不支持附加商品或订单状态筛选。"
    return None


def query_types_in_question(question: str) -> set[str]:
    """Recognize only explicit query families; do not invent a missing family."""
    types = set()
    if any(token in question for token in ("每日", "每天", "按日", "按日期")):
        types.add("daily_sales")
    if "品类" in question or "类别" in question:
        types.add("category_sales")
    if "商品" in question and any(token in question for token in ("前", "排行", "排名", "最好")):
        types.add("top_products")
    return types


def ranking_in_question(question: str) -> tuple[str | None, int | None]:
    """Require an explicit ranking metric and one Arabic N; never use model defaults.

    An explicit '按销售额' may coexist with displaying both result metrics. Without
    an explicit ordering phrase, mentioning both metrics cannot establish rank_by.
    """
    explicit_amount = bool(re.search(r"按\s*(?:销售额|金额)", question))
    explicit_quantity = bool(re.search(r"按\s*(?:销量|销售数量)", question))
    joined_metrics = bool(re.search(
        r"按\s*(?:(?:销售额|金额)\s*(?:和|与|及|以及|、|/|或|还是)\s*(?:按\s*)?(?:销量|销售数量)"
        r"|(?:销量|销售数量)\s*(?:和|与|及|以及|、|/|或|还是)\s*(?:按\s*)?(?:销售额|金额))",
        question,
    ))
    amount = "销售额" in question or "金额" in question
    quantity = "销量" in question or "销售数量" in question
    metric = None
    if not joined_metrics and not (explicit_amount and explicit_quantity):
        if explicit_amount or (amount and not quantity):
            metric = "sales_amount"
        elif explicit_quantity or (quantity and not amount):
            metric = "sales_quantity"
    numbers = re.findall(r"前\s*([+-]?\d+)(?![\d.])\s*(?:个|名|款|种)?", question)
    parsed_numbers = {int(number) for number in numbers}
    top_n = next(iter(parsed_numbers)) if len(parsed_numbers) == 1 else None
    return metric, top_n


def _leading_annotations(fragment: str) -> list[str]:
    annotations = []
    while match := re.match(r"\s*[（(]([^）)]*)[）)]", fragment):
        annotations.append(match[1])
        fragment = fragment[match.end():]
    return annotations


def explicit_interval(question: str) -> tuple[date, date] | None:
    """Recognize two absolute ISO dates with an explicit exclusive/inclusive upper bound.

    This small grammar is shared by mock parsing and local verification of live dates.
    It intentionally does not interpret relative dates or guess an ambiguous end day.
    """
    matches = list(DATE_RE.finditer(question))
    if len(matches) != 2:
        return None
    if re.search(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?|\d+\s*(?:点|时|分|秒)", question):
        return None
    if any(token in question for token in ("零点", "中午", "午夜", "上午", "下午", "晚上", "早上", "凌晨")):
        return None
    try:
        start, end = (date.fromisoformat(match.group()) for match in matches)
    except ValueError:
        return None
    tail = question[matches[1].end():]
    between = question[matches[0].end():matches[1].start()]
    prefix = question[:matches[0].start()]
    if prefix.rstrip().endswith("(") or prefix.rstrip().endswith("（"):
        # An explicitly open start is outside the supported left-closed date grammar.
        return None
    if re.match(r"\s*(?:[（(]\s*)?(?:不含|不包含|排除)", between):
        return None
    start_annotations = _leading_annotations(between)
    if any(token in annotation for annotation in start_annotations for token in ("不含", "不包含", "排除")):
        return None
    end_annotations = _leading_annotations(tail)
    annotation_exclusive = False
    annotation_inclusive = False
    for annotation in end_annotations:
        negative = bool(re.search(r"不包含|不含|排除", annotation))
        stripped = re.sub(r"不包含|不含|排除", "", annotation)
        annotation_exclusive = annotation_exclusive or negative
        annotation_inclusive = annotation_inclusive or bool(re.search(r"包含|包括|含", stripped))
    if annotation_exclusive and annotation_inclusive:
        return None
    exclusive = (
        annotation_exclusive
        or
        bool(re.match(r"\s*(?:[（(]\s*)?(?:不含|不包含|排除)", tail))
        or (prefix.rstrip().endswith("[") and "," in between and bool(re.match(r"\s*\)", tail)))
        or "不含结束日期" in question or "结束日期不含" in question
        or "不包含结束日期" in question or "结束日期不包含" in question
        or "结束日期为排他上界" in question
    )
    inclusive = (
        annotation_inclusive
        or
        bool(re.match(r"\s*(?:[（(]\s*)?(?:含|包含|包括)", tail))
        or (prefix.rstrip().endswith("[") and "," in between and bool(re.match(r"\s*\]", tail)))
        or bool(re.search(r"(?<!不)(?:包含|包括)结束日期|结束日期(?:包含|包括)", question))
    )
    if exclusive and inclusive:
        return None
    if exclusive:
        return start, end
    if inclusive:
        try:
            return start, end + timedelta(days=1)
        except OverflowError:
            return None
    return None


def _clarify(message: str) -> ValidationResult:
    return ValidationResult("needs_clarification", message=message + FOLLOWUP)


def validate_plan(
    candidate: CandidatePlan | None,
    *,
    decision: DecisionType = "ready",
    message: str = "",
    question: str | None = None,
) -> ValidationResult:
    if question is not None:
        reason = unsupported_reason(question)
        if reason:
            return ValidationResult("unsupported", message=reason)
    if decision == "unsupported":
        return ValidationResult("unsupported", message=SUPPORTED + "此问题超出支持范围。")
    if candidate is None or candidate.query_type is None:
        return _clarify("请明确选择每日销售、品类销售或商品排行，并提供日期区间。")
    if question is not None:
        requested_types = query_types_in_question(question)
        if len(requested_types) != 1:
            return _clarify("请明确选择一个查询类型：每日销售、品类销售或商品排行。")
        if candidate.query_type not in requested_types:
            return _clarify("解析出的查询类型与原问题不一致，请明确选择每日销售、品类销售或商品排行。")
    if candidate.query_type == "top_products":
        if question is not None:
            requested_rank, requested_n = ranking_in_question(question)
            if requested_rank is None:
                return _clarify("请明确商品排行采用一个口径：按销售额还是按销量。")
            if requested_n is None:
                return _clarify("请明确商品排行数量，使用阿拉伯整数表示前 N 个商品（N 为 1—10）。")
            if candidate.rank_by != requested_rank or candidate.top_n != requested_n:
                return _clarify("解析出的排行口径或数量与原问题不一致，请明确按销售额或销量及前 N 个商品。")
        if candidate.rank_by is None:
            return _clarify("请明确商品排行按销售额还是销量。")
        if candidate.top_n is None:
            return _clarify("请明确需要前几个商品（N 为 1—10 的整数）。")
        if not 1 <= candidate.top_n <= 10:
            return _clarify("商品排行数量 N 必须为 1—10，请调整。")
    elif candidate.rank_by is not None or candidate.top_n is not None:
        return _clarify("每日销售和品类销售不使用商品排行参数，请调整问题。")
    if candidate.start_date is None or candidate.end_date is None:
        return _clarify("请提供明确的开始日期和结束日期（YYYY-MM-DD），并注明结束日期是否包含。")
    if not DATE_RE.fullmatch(candidate.start_date) or not DATE_RE.fullmatch(candidate.end_date):
        return _clarify("日期必须采用有效的 YYYY-MM-DD 格式。")
    try:
        start = date.fromisoformat(candidate.start_date)
        end = date.fromisoformat(candidate.end_date)
    except ValueError:
        return _clarify("日期无效，请提供存在的 YYYY-MM-DD 日期。")
    if question is not None:
        interval = explicit_interval(question)
        if interval is None:
            return _clarify("请提供两个明确的 YYYY-MM-DD 日期，并注明结束日期是否包含；相对日期暂不支持。")
        if (start, end) != interval:
            return _clarify("解析出的日期区间与原问题不一致，请明确日期和结束边界后重新提交。")
    span = (end - start).days
    if span <= 0:
        return _clarify("开始日期必须早于结束日期（结束日期为不包含的上界）。")
    if span > 90:
        return _clarify("日期跨度最多为 90 个自然日，请缩小区间。")
    if decision == "needs_clarification":
        return _clarify(message or "问题仍有业务歧义，请明确查询口径。")
    plan = ValidatedPlan(
        query_type=candidate.query_type,
        start_date=start,
        end_date=end,
        rank_by=candidate.rank_by,
        top_n=candidate.top_n,
    )
    return ValidationResult("ready", plan=plan)
