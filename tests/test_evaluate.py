"""Unit tests for evaluator accounting, independent of MySQL acceptance."""

from copy import deepcopy
from pathlib import Path

import pytest

from scripts.evaluate import CountingRepository, check_case, load_cases, rows_equal, run_suite


CASES = load_cases(Path(__file__).resolve().parents[1] / "evals" / "cases.json")


def response_for(case):
    plan = deepcopy(case["expected_plan"])
    data = deepcopy(case["expected_rows"])
    answer = "固定模拟数据，已支付口径，人民币 "
    if plan:
        answer += f"[{plan['start_date']}, {plan['end_date']}) "
    if data:
        answer += "，".join(row["sales_amount"] for row in data)
    else:
        answer += "该区间没有符合口径的记录"
    answer += " 销售额或销量，请补充条件后重新提交原问题。"
    return {"status": case["expected_status"], "model_mode": "mock", "plan": plan,
            "data": data, "answer": answer, "error": None,
            "query": {"template_id": "unit-test-only"} if plan else None}


def test_cases_cover_eight_queries_and_six_non_queries():
    assert len(CASES) == 14
    assert sum(case["expected_status"] == "success" for case in CASES) == 8
    assert sum(case["expected_status"] == "needs_clarification" for case in CASES) == 3
    assert sum(case["expected_status"] == "unsupported" for case in CASES) == 3


def test_exact_money_and_integer_types():
    expected = [{"sales_amount": "340.00", "sales_quantity": 6}]
    assert rows_equal(expected, deepcopy(expected))
    assert not rows_equal([{"sales_amount": 340.0, "sales_quantity": 6}], expected)
    assert not rows_equal([{"sales_amount": "340", "sales_quantity": 6}], expected)
    assert not rows_equal([{"sales_amount": "340.00", "sales_quantity": 6.0}], expected)
    assert not rows_equal([{"sales_amount": "340.00", "sales_quantity": True}], expected)


def test_comparison_preserves_duplicates_null_and_rank_order():
    first, second = CASES[2]["expected_rows"]
    assert not rows_equal([second, first], [first, second])
    assert rows_equal([second, first], [first, second], ordered=False)
    assert not rows_equal([first], [first, first], ordered=False)
    assert not rows_equal(None, [])
    assert not rows_equal([{"category": None}], [{"category": ""}])


def test_empty_result_wrong_interval_never_counts_as_correct():
    case = CASES[6]
    response = response_for(case)
    assert check_case(case, response, 1)["result_correct"]
    response["plan"]["start_date"] = "2026-10-02"
    checked = check_case(case, response, 1)
    assert not checked["result_correct"]
    assert "PLAN_MISMATCH" in checked["failures"]


def test_non_queries_are_not_sql_success_and_must_not_query():
    case = CASES[8]
    response = response_for(case)
    checked = check_case(case, response, 0)
    assert checked["passed"]
    assert checked["result_correct"] is None
    checked = check_case(case, response, 1)
    assert not checked["passed"]
    assert "QUERY_EXECUTION_MISMATCH" in checked["failures"]


def test_independent_sum_and_answer_consistency():
    case = deepcopy(CASES[7])
    response = response_for(case)
    assert check_case(case, response, 1)["passed"]
    case["expected_total_sales_amount"] = "500.01"
    checked = check_case(case, response, 1)
    assert "TOTAL_MISMATCH" in checked["failures"]
    response["answer"] = "金额 1.00"
    assert not check_case(CASES[7], response, 1)["answer_passed"]


def test_counter_delegates_and_propagates_errors():
    class RepositoryStub:
        def execute(self, plan):
            assert plan == "unit-test-plan"
            return {"rows": []}

        def health(self):
            return {"database": "unit-test-only"}

    counted = CountingRepository(RepositoryStub())
    assert counted.execute("unit-test-plan") == {"rows": []}
    assert counted.query_count == 1
    assert counted.health() == {"database": "unit-test-only"}
    with pytest.raises(AssertionError):
        counted.execute("invalid-unit-test-plan")
    assert counted.query_count == 2


def test_suite_separates_query_and_non_query_metrics(monkeypatch):
    import app.graph

    cases = [CASES[0], CASES[6], CASES[8], CASES[12]]
    by_question = {case["question"]: case for case in cases}

    class RepositoryStub:
        def execute(self, plan):
            return {"rows": []}

    repository = CountingRepository(RepositoryStub())

    class ResponseStub:
        def __init__(self, payload):
            self.payload = payload

        def model_dump(self, **kwargs):
            return self.payload

    class GraphStub:
        def invoke(self, question, config):
            assert config["recursion_limit"] == 8
            case = by_question[question]
            if case["expected_status"] == "success":
                repository.execute(case["expected_plan"])
            return response_for(case)

    monkeypatch.setattr(app.graph, "create_state", lambda question, mode: question)
    monkeypatch.setattr(app.graph, "to_response", ResponseStub)
    report = run_suite(GraphStub(), repository, "mock", cases)
    assert report["total_cases"] == 4
    assert report["plan_status_passed"] == 4
    assert report["query_result_cases"] == 2
    assert report["query_results_correct"] == 2
    assert report["non_query_cases"] == report["non_query_passed"] == 2
    assert report["exit_status"] == 0


def test_suite_internal_error_is_safe_and_not_a_success(monkeypatch):
    import app.graph

    class GraphStub:
        def invoke(self, state, config):
            raise RuntimeError("secret-should-never-be-in-report")

    monkeypatch.setattr(app.graph, "create_state", lambda question, mode: question)
    report = run_suite(GraphStub(), CountingRepository(object()), "live", [CASES[0]])
    assert report["exit_status"] == 1
    assert report["query_results_correct"] == report["plan_status_passed"] == 0
    assert report["error_classification"]["EVALUATION_INTERNAL_ERROR"] >= 1
    assert "secret-should-never-be-in-report" not in str(report)
