"""Even words from the supported language cannot imply unrepresented clauses."""

from types import SimpleNamespace

import pytest

from app.graph import build_graph, create_state, to_response
from app.model_adapter import MockModelAdapter
from app.plan_validation import validate_plan
from app.schemas import CandidatePlan, ModelDecision


@pytest.mark.parametrize("query_type,label,condition", [
    ("daily_sales", "每日", "按销售额排名"),
    ("category_sales", "品类", "按销量排名"),
    ("daily_sales", "每日", "前2名"),
    ("category_sales", "品类", "前2个"),
    ("daily_sales", "每日", "按销量"),
    ("daily_sales", "每日", "排除销售额"),
    ("category_sales", "品类", "不含销量"),
    ("daily_sales", "每日", "金额大于100元"),
])
@pytest.mark.parametrize("mode", ["mock", "live"])
def test_unrepresented_clauses_never_execute(query_type, label, condition, mode):
    question = f"查询从2026-10-01（含）到2026-10-04（不含）的{label}销售额和销量，{condition}"
    plan = CandidatePlan(query_type=query_type, start_date="2026-10-01", end_date="2026-10-04")

    class ReadyAdapter:
        def parse(self, question, *, deadline):
            return ModelDecision(decision="ready", plan=plan, message="")

    class NoQueryRepository:
        calls = 0

        def execute(self, plan):
            self.calls += 1
            raise AssertionError("An unrepresented clause must never reach the database")

    assert validate_plan(plan, question=question).status == "needs_clarification"
    repository = NoQueryRepository()
    adapter = MockModelAdapter() if mode == "mock" else ReadyAdapter()
    graph = build_graph(adapter, repository, SimpleNamespace(model_total_timeout=15))
    response = to_response(graph.invoke(create_state(question, mode), config={"recursion_limit": 8}))
    assert response.status == "needs_clarification"
    assert response.plan is None and response.query is None and response.data == []
    assert repository.calls == 0
    assert all(event.node != "execute_query" for event in response.trace)
