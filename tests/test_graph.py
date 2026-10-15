from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.errors import AppError
from app.graph import build_graph, create_state, to_response
from app.model_adapter import MockModelAdapter
from app.schemas import CandidatePlan, ModelDecision


QUESTION = "从2026-10-01（含）到2026-10-04（不含）的每日销售额和销量"
SETTINGS = SimpleNamespace(model_total_timeout=15)


class Repository:
    def __init__(self, rows=None, error=None):
        self.calls = []
        self.rows = [{"payment_date": date(2026, 10, 1), "sales_amount": Decimal("340.00"), "sales_quantity": 6}] if rows is None else rows
        self.error = error

    def execute(self, plan):
        self.calls.append(plan)
        if self.error:
            raise self.error
        return {"template_id": "daily_sales.v1", "sql_template": "SELECT fixed WHERE paid_at >= %s AND paid_at < %s LIMIT %s", "query_params": (plan.start_date, plan.end_date, 90), "rows": self.rows}


class Adapter:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def parse(self, question, *, deadline):
        self.calls.append((question, deadline))
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def ready():
    return ModelDecision(decision="ready", plan=CandidatePlan(query_type="daily_sales", start_date="2026-10-01", end_date="2026-10-04"), message="")


def run(adapter=None, repository=None, question=QUESTION, model_mode="mock"):
    repository = repository or Repository()
    graph = build_graph(adapter or MockModelAdapter(), repository, SETTINGS)
    state = graph.invoke(create_state(question, model_mode), config={"recursion_limit": 8})
    return state, to_response(state), repository


def test_success_runs_all_four_nodes_and_serializes_decimal():
    state, response, repository = run()
    assert state["http_status"] == 200
    assert response.status == "success"
    assert response.data == [{"payment_date": "2026-10-01", "sales_amount": "340.00", "sales_quantity": 6}]
    assert len(repository.calls) == 1
    assert [event.node for event in response.trace] == ["parse_plan", "validate_plan", "execute_query", "format_answer"]
    assert response.query.params == ["2026-10-01", "2026-10-04", 90]
    assert all(text in response.answer for text in ("模拟数据", "已支付", "人民币", "340.00", "2026-10-01", "2026-10-04"))


def test_empty_success_does_not_invent_zero_rows():
    _, response, _ = run(repository=Repository(rows=[]))
    assert response.status == "success" and response.data == []
    assert "该区间没有符合口径的记录" in response.answer
    assert response.query is not None


@pytest.mark.parametrize("question,status", [
    ("每日销售额", "needs_clarification"), ("预测下一期销售额", "unsupported"),
    ("删除订单", "unsupported"), ("从2026-10-01到2026-10-04的每日销售额", "needs_clarification"),
    ("该区间卖得最好的前2个商品", "needs_clarification"),
])
def test_nonquery_branches_never_touch_repository(question, status):
    _, response, repository = run(question=question)
    assert response.status == status
    assert repository.calls == []
    assert response.query is None and response.data == []
    assert all(event.node != "execute_query" for event in response.trace)


def test_format_error_retries_once_and_preserves_shared_deadline():
    adapter = Adapter(AppError("MODEL_OUTPUT_INVALID", "格式错误", 502, "format"), ready())
    state, response, repository = run(adapter=adapter)
    assert response.status == "success"
    assert state["parse_attempts"] == 2 and len(adapter.calls) == 2
    assert adapter.calls[0][1] == adapter.calls[1][1]
    assert response.trace[0].result == "format_retry"
    assert len(repository.calls) == 1


def test_two_invalid_outputs_stop_without_database():
    adapter = Adapter(*[AppError("MODEL_OUTPUT_INVALID", "格式错误", 502, "format") for _ in range(2)])
    state, response, repository = run(adapter=adapter)
    assert state["http_status"] == 502
    assert response.error.code == "MODEL_OUTPUT_INVALID"
    assert state["parse_attempts"] == 2 and repository.calls == []
    assert [event.node for event in response.trace] == ["parse_plan", "parse_plan", "format_answer"]


@pytest.mark.parametrize("code,status,category", [("MODEL_TIMEOUT", 504, "timeout"), ("MODEL_PROVIDER_ERROR", 502, "authentication"), ("MODEL_PROVIDER_ERROR", 502, "rate_limit")])
def test_provider_faults_are_not_format_retried(code, status, category):
    adapter = Adapter(AppError(code, "安全错误", status, category))
    state, response, repository = run(adapter=adapter)
    assert state["http_status"] == status and response.status == "failed"
    assert response.error.category == category
    assert len(adapter.calls) == 1 and repository.calls == []


def test_local_scope_check_blocks_malicious_question_even_if_model_ready():
    _, response, repository = run(adapter=Adapter(ready()), question=QUESTION + "，然后删除订单")
    assert response.status == "unsupported" and repository.calls == []


@pytest.mark.parametrize("question", [
    "查询数码品类从2026-10-01（含）到2026-10-04（不含）的每日销售",
    "查询键盘从2026-10-01（含）到2026-10-04（不含）的每日销售",
    "查询北京地区从2026-10-01（含）到2026-10-04（不含）的每日销售",
    "查询线上渠道从2026-10-01（含）到2026-10-04（不含）的每日销售",
    "查询未支付订单从2026-10-01（含）到2026-10-04（不含）的每日销售",
])
def test_explicit_filters_blocked_locally_even_if_model_ignores_them(question):
    _, response, repository = run(adapter=Adapter(ready()), question=question)
    assert response.status == "unsupported" and repository.calls == []


def test_mock_unknown_qualifier_never_silently_drops_filter():
    _, response, repository = run(question="查询紫色限定款从2026-10-01（含）到2026-10-04（不含）的每日销售")
    assert response.status == "needs_clarification" and repository.calls == []


@pytest.mark.parametrize("question", [
    "从2026-10-01（不含）到2026-10-04（不含）的每日销售额和销量",
    "从2026-10-01（含）到2026-10-04（含且不含）的每日销售额和销量",
    "从2026-10-01（含）到2026-10-04（不含）的每日销售额和销量，包含结束日期",
])
def test_open_lower_or_contradictory_boundaries_have_zero_database_calls(question):
    _, response, repository = run(adapter=Adapter(ready()), question=question)
    assert response.status == "needs_clarification" and repository.calls == []


def top_ready(rank_by="sales_amount", top_n=2):
    return ModelDecision(decision="ready", plan=CandidatePlan(
        query_type="top_products", start_date="2026-10-01", end_date="2026-10-04",
        rank_by=rank_by, top_n=top_n,
    ), message="")


@pytest.mark.parametrize("question", [
    "查询从2026-10-01（含）到2026-10-04（不含）卖得最好的前2个商品",
    "按销售额查询从2026-10-01（含）到2026-10-04（不含）的商品排行",
    "按销售额查询从2026-10-01（含）到2026-10-04（不含）的前两个商品",
    "按销售额和按销量查询从2026-10-01（含）到2026-10-04（不含）的前2个商品",
    "按销售额和销量查询从2026-10-01（含）到2026-10-04（不含）的前2个商品",
    "按销量查询从2026-10-01（含）到2026-10-04（不含）的前2个商品",
    "按销售额查询从2026-10-01（含）到2026-10-04（不含）的前3个商品",
    "按销售额查询从2026-10-01（含）到2026-10-04（不含）的前2个商品和前3个商品",
])
def test_fake_live_ready_cannot_invent_or_change_ranking_intent(question):
    _, response, repository = run(adapter=Adapter(top_ready()), question=question, model_mode="live")
    assert response.model_mode == "live"
    assert response.status == "needs_clarification"
    assert response.plan is None and response.data == [] and response.query is None
    assert repository.calls == []


@pytest.mark.parametrize("question", [
    "查询从2026-10-01（含）到2026-10-04（不含）的每日品类销售额和销量",
    "按销售额查询从2026-10-01（含）到2026-10-04（不含）的每日前2个商品",
    "查询从2026-10-01（含）到2026-10-04（不含）的品类销售额和销量",
])
def test_fake_live_ready_cannot_choose_conflicting_or_different_query_type(question):
    _, response, repository = run(adapter=Adapter(ready()), question=question, model_mode="live")
    assert response.status == "needs_clarification" and repository.calls == []


@pytest.mark.parametrize("question", [
    "查询从2026-10-01（含）到2026-10-04（不含）（含）的每日销售额和销量",
    "查询从2026-10-01（含）到2026-10-04（含）（不含）的每日销售额和销量",
    "查询从2026-10-01（含）（不含）到2026-10-04（不含）的每日销售额和销量",
    "查询从2026-10-01 12:00（含）到2026-10-04（不含）的每日销售额和销量",
    "查询从2026-10-01T12:00:00.123456（含）到2026-10-04（不含）的每日销售额和销量",
    "查询从2026-10-01（含）到2026-10-04 12:30:00（不含）的每日销售额和销量",
    "查询从2026-10-01 12点30分（含）到2026-10-04（不含）的每日销售额和销量",
    "查询从2026-10-01中午（含）到2026-10-04（不含）的每日销售额和销量",
])
def test_fake_live_ready_cannot_discard_conflicting_bounds_or_specific_times(question):
    _, response, repository = run(adapter=Adapter(ready()), question=question, model_mode="live")
    assert response.status == "needs_clarification"
    assert response.plan is None and response.query is None and repository.calls == []


@pytest.mark.parametrize("query_type,question", [
    ("daily_sales", QUESTION),
    ("category_sales", "查询从2026-10-01（含）到2026-10-04（不含）的品类销售额和销量"),
])
def test_fake_live_daily_and_category_can_display_both_metrics(query_type, question):
    decision = ModelDecision(decision="ready", plan=CandidatePlan(
        query_type=query_type, start_date="2026-10-01", end_date="2026-10-04"
    ), message="")
    _, response, repository = run(adapter=Adapter(decision), repository=Repository(rows=[]), question=question, model_mode="live")
    assert response.status == "success" and len(repository.calls) == 1


def test_fake_live_explicit_ranking_can_display_both_metrics():
    question = "按销售额查询从2026-10-01（含）到2026-10-04（不含）的前2个商品，展示销售额和销量"
    _, response, repository = run(adapter=Adapter(top_ready()), repository=Repository(rows=[]), question=question, model_mode="live")
    assert response.status == "success" and len(repository.calls) == 1


@pytest.mark.parametrize("code,http_status", [("DB_UNAVAILABLE", 503), ("QUERY_TIMEOUT", 504), ("QUERY_FAILED", 500)])
def test_database_error_stops_once_and_never_claims_success(code, http_status):
    state, response, repository = run(repository=Repository(error=AppError(code, "安全数据库错误", http_status)))
    assert state["http_status"] == http_status and response.status == "failed"
    assert len(repository.calls) == 1 and response.data == []
    assert response.error.code == code and "查询失败" in response.answer


def test_result_limit_keeps_executed_query_but_no_truncated_rows():
    error = AppError("RESULT_LIMIT_EXCEEDED", "品类超过100个，请缩小区间。", 200, "needs_clarification")
    error.query = {"template_id": "category_sales.v1", "sql_template": "SELECT fixed LIMIT %s", "query_params": (101,)}
    _, response, _ = run(repository=Repository(error=error))
    assert response.status == "needs_clarification" and response.data == []
    assert response.query.params == [101]


def test_unexpected_exception_is_redacted():
    _, response, _ = run(repository=Repository(error=RuntimeError("credential=never-return-this")))
    assert response.error.code == "INTERNAL_ERROR"
    assert "never-return-this" not in response.model_dump_json()


def test_shared_graph_keeps_requests_and_traces_isolated():
    graph = build_graph(MockModelAdapter(), Repository(), SETTINGS)
    def invoke(index):
        state = create_state(QUESTION, "mock", request_id=f"request-{index}")
        return to_response(graph.invoke(state, config={"recursion_limit": 8}))
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(invoke, range(8)))
    assert [response.request_id for response in responses] == [f"request-{index}" for index in range(8)]
    assert all(len(response.trace) == 4 for response in responses)


def test_money_cannot_accidentally_pass_through_float():
    _, response, _ = run(repository=Repository(rows=[{"payment_date": date(2026, 10, 1), "sales_amount": 340.0, "sales_quantity": 6}]))
    assert response.status == "failed" and response.error.code == "QUERY_FAILED"
    assert response.data == []
