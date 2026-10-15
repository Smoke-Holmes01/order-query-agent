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
        "周末", "工作日", "节假日", "休息日",
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


_BOUNDARY_ACTION_PATTERN = r"不包含|不包括|不含|排除|包含|包括|含|排他上界"
_BOUNDARY_ENDPOINT_PATTERN = r"开始日期|起始日期|开始日|起始日|开始|起始|结束日期|截止日期|结束日|截止日|结束|截止"
_BOUNDARY_ACTION_RE = re.compile(_BOUNDARY_ACTION_PATTERN)
NAMED_BOUNDARY_RE = re.compile(
    rf"(?P<action_before>{_BOUNDARY_ACTION_PATTERN})\s*(?P<endpoint_after>{_BOUNDARY_ENDPOINT_PATTERN})"
    rf"|(?P<endpoint_before>{_BOUNDARY_ENDPOINT_PATTERN})\s*"
    rf"(?:(?:应当|应该|需要|为|是|应|需|要|：|:)\s*)?"
    rf"(?P<action_after>{_BOUNDARY_ACTION_PATTERN})"
)


def _boundary_values(fragment: str) -> set[bool]:
    """Consume each longest action once so a negation never also means inclusion."""
    return {
        not match.group().startswith(("不", "排除", "排他"))
        for match in _BOUNDARY_ACTION_RE.finditer(fragment)
    }


def _leading_annotations(fragment: str) -> list[str]:
    annotations = []
    while match := re.match(r"\s*[（(]([^）)]*)[）)]", fragment):
        annotations.append(match[1])
        fragment = fragment[match.end():]
    return annotations


def _adjacent_boundary_values(fragment: str) -> set[bool]:
    values = set()
    for annotation in _leading_annotations(fragment):
        # Explicitly named endpoints are handled sentence-wide, even inside notes.
        values.update(_boundary_values(NAMED_BOUNDARY_RE.sub("", annotation)))
    if not NAMED_BOUNDARY_RE.match(fragment.lstrip()) and (match := re.match(rf"\s*({_BOUNDARY_ACTION_PATTERN})", fragment)):
        values.update(_boundary_values(match[1]))
    return values


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
    start_values = _adjacent_boundary_values(between)
    end_values = _adjacent_boundary_values(tail)
    for reference in NAMED_BOUNDARY_RE.finditer(question):
        endpoint = reference["endpoint_after"] or reference["endpoint_before"]
        action = reference["action_before"] or reference["action_after"]
        values = start_values if endpoint.startswith(("开始", "起始")) else end_values
        values.update(_boundary_values(action))
    if prefix.rstrip().endswith("[") and "," in between:
        start_values.add(True)
        if re.match(r"\s*\)", tail):
            end_values.add(False)
        elif re.match(r"\s*\]", tail):
            end_values.add(True)
    # The supported lower boundary is always inclusive. An explicit exclusion
    # anywhere, with or without a conflicting local note, requires clarification.
    if False in start_values or len(end_values) != 1:
        return None
    if False in end_values:
        return start, end
    if True in end_values:
        try:
            return start, end + timedelta(days=1)
        except OverflowError:
            return None
    return None


def _clarify(message: str) -> ValidationResult:
    return ValidationResult("needs_clarification", message=message + FOLLOWUP)


def unrecognized_question_content(question: str) -> str:
    """Fail closed for text the five-field plan cannot account for.

    This is the same small language for both model modes. Boundary text may be
    consumed only beside a date or as a complete named endpoint statement;
    arbitrary exclusion words are deliberately not grammar filler. The caller
    independently verifies the actual interval, query family and ranking first.
    """
    annotation = r"(?:不包含|不包括|不含|排除|包含|包括|含)"
    remainder = re.sub(
        DATE_RE.pattern + rf"(?:\s*[（(]\s*{annotation}\s*[）)])*(?:\s*{annotation})?",
        " ", question,
    )
    remainder = NAMED_BOUNDARY_RE.sub(" ", remainder)
    remainder = re.sub(r"前\s*[+-]?\d+(?![\d.])\s*(?:个|名|款|种)?", " ", remainder)
    grammar = (
        "销售额和销量", "销售额与销量", "销售额以及销量", "卖得最好的", "卖得最好", "该区间",
        "只统计已支付订单", "仅统计已支付订单", "只看已支付订单", "仅看已支付订单",
        "已支付订单", "销售数量", "销售金额", "销售额", "销量", "支付时间", "人民币",
        "按日期", "每日", "每天", "按日", "品类", "类别", "商品", "排行", "排名",
        "统计", "查询", "查看", "帮我", "展示", "显示", "前", "个", "名", "款", "种",
        "按", "从", "到", "至", "的", "和", "与", "请", "销售",
    )
    for token in sorted(grammar, key=len, reverse=True):
        remainder = remainder.replace(token, " ")
    return re.sub(r"[\s，,。！？!?、：:；;（）()\[\]【】]", "", remainder)


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
    if question is not None and candidate.query_type != "top_products":
        if re.search(r"排行|排名|前\s*[+-]?\d+|按\s*(?:销售额|金额|销量|销售数量)", question):
            return _clarify("每日销售和品类销售使用固定排序与完整结果，无法表达附加排行口径或前 N 条限制，请调整问题。")
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
    if question is not None and unrecognized_question_content(question):
        return _clarify("问题包含当前五字段计划无法可靠表达的附加条件或未知表述，请使用支持的查询格式；不会忽略这些条件执行查询。")
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
