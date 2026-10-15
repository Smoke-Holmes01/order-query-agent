from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import AppError
from app.main import create_app
from app.model_adapter import MockModelAdapter
from app.schemas import ModelDecision

QUESTION = "从2026-10-01（含）到2026-10-04（不含）的每日销售"


class Adapter:
    def __init__(self, decision="ready", error=None, plan=None):
        self.decision = decision
        self.error = error
        self.calls = 0
        self.plan = plan

    def parse(self, question, *, deadline):
        self.calls += 1
        if self.error:
            raise self.error
        return ModelDecision(decision=self.decision, plan=self.plan or {
            "query_type": "daily_sales", "start_date": "2026-10-01", "end_date": "2026-10-04",
            "rank_by": None, "top_n": None,
        }, message="请补充信息" if self.decision == "needs_clarification" else "")


class Repo:
    def __init__(self, error=None, rows=None):
        self.error = error
        self.calls = 0
        self.rows = [{"payment_date": date(2026, 10, 1), "sales_amount": Decimal("340.00"), "sales_quantity": 6}] if rows is None else rows

    def health(self):
        if self.error:
            raise self.error
        return {"database": "mysql", "database_ready": True, "server_version": "8.4.test-double"}

    def execute(self, plan):
        self.calls += 1
        if self.error:
            raise self.error
        return {"template_id": "daily_sales", "sql_template": "SELECT ... WHERE paid_at >= %s AND paid_at < %s LIMIT %s",
                "query_params": ["2026-10-01 00:00:00", "2026-10-04 00:00:00", 90],
                "rows": self.rows}


def client(adapter=None, repository=None, model_mode="mock"):
    return TestClient(create_app(Settings(db_password="unit-test-placeholder", model_mode=model_mode,
                                         model_base_url="https://model.example/v1", model_name="unit-test-model",
                                         model_api_key="unit-test-only-placeholder"),
                                 adapter=adapter or Adapter(), repository=repository or Repo()))


def test_http_success_decimal_trace_and_request_id():
    with client() as api:
        response = api.post("/api/query", json={"question": "  " + QUESTION + "  "})
        body = response.json()
        assert response.status_code == 200
        assert body["status"] == "success"
        assert body["model_mode"] == "mock"
        assert body["error"] is None
        assert body["data"][0]["sales_amount"] == "340.00"
        assert body["data"][0]["sales_quantity"] == 6
        assert body["query"]["params"][0].startswith("2026-10-01")
        assert [event["node"] for event in body["trace"]] == ["parse_plan", "validate_plan", "execute_query", "format_answer"]
        assert body["request_id"] != api.post("/api/query", json={"question": QUESTION}).json()["request_id"]


@pytest.mark.parametrize("body", [
    {}, {"question": " "}, {"question": "x" * 1001}, {"question": 12},
    {"question": "x", "state": {"rows": []}}, {"question": "x", "model_mode": "live"},
    {"question": "x", "sql": "DELETE FROM orders"}, {"question": "x", "parse_attempts": 0},
])
def test_invalid_requests_never_call_model_or_database(body):
    adapter, repo = Adapter(), Repo()
    with client(adapter, repo) as api:
        assert api.post("/api/query", json=body).status_code == 422
    assert adapter.calls == repo.calls == 0


def test_malformed_json_422():
    with client() as api:
        assert api.post("/api/query", content="{", headers={"Content-Type": "application/json"}).status_code == 422


@pytest.mark.parametrize("decision", ["unsupported", "needs_clarification"])
def test_no_query_for_non_success(decision):
    repo = Repo()
    with client(Adapter(decision), repo) as api:
        body = api.post("/api/query", json={"question": "演示"}).json()
        assert body["status"] == decision
        assert body["data"] == []
        assert body["query"] is None
        assert "execute_query" not in [entry["node"] for entry in body["trace"]]
    assert repo.calls == 0


@pytest.mark.parametrize("code,http_status", [
    ("DB_UNAVAILABLE", 503), ("QUERY_TIMEOUT", 504), ("QUERY_FAILED", 500),
])
def test_database_error_contract(code, http_status):
    repo = Repo(AppError(code, "安全数据库错误", http_status))
    with client(repository=repo) as api:
        response = api.post("/api/query", json={"question": QUESTION})
    assert response.status_code == http_status
    assert response.json()["status"] == "failed"
    assert response.json()["error"]["code"] == code
    assert response.json()["data"] == []
    assert repo.calls == 1


@pytest.mark.parametrize("code,http_status", [("MODEL_PROVIDER_ERROR", 502), ("MODEL_TIMEOUT", 504)])
def test_model_error_contract(code, http_status):
    adapter, repo = Adapter(error=AppError(code, "安全模型错误", http_status)), Repo()
    with client(adapter, repo) as api:
        response = api.post("/api/query", json={"question": "演示"})
    assert response.status_code == http_status
    assert response.json()["error"]["code"] == code
    assert adapter.calls == 1
    assert repo.calls == 0


def test_health_is_not_a_model_probe():
    adapter = Adapter()
    with client(adapter) as api:
        response = api.get("/health")
        assert response.status_code == 200
        assert response.json()["model_mode"] == "mock"
        assert response.json()["database_ready"] is True
        assert api.get("/docs").status_code == 200
    assert adapter.calls == 0


def test_unhealthy_database_503():
    with client(repository=Repo(AppError("DB_UNAVAILABLE", "数据库暂不可用", 503))) as api:
        response = api.get("/health")
        assert response.status_code == 503
        assert response.json()["database_ready"] is False


@pytest.mark.parametrize("model_mode", ["mock", "live"])
def test_trailing_start_exclusion_has_safe_http_contract_and_zero_database_calls(model_mode):
    repo = Repo()
    adapter = MockModelAdapter() if model_mode == "mock" else Adapter()
    question = "查询从2026-10-01（含）到2026-10-04（不含）的每日销售额和销量，不含开始日期"
    with client(adapter, repo, model_mode=model_mode) as api:
        response = api.post("/api/query", json={"question": question})
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "needs_clarification" and body["model_mode"] == model_mode
    assert body["plan"] is None and body["query"] is None and body["data"] == []
    assert repo.calls == 0
    assert all(event["node"] != "execute_query" for event in body["trace"])


@pytest.mark.parametrize("model_mode", ["mock", "live"])
@pytest.mark.parametrize("query_type,question", [
    ("daily_sales", "查询从2026-10-01（含）到2026-10-04（不含）的每日销售额"),
    ("category_sales", "查询从2026-10-01（含）到2026-10-04（不含）的品类销售额"),
    ("top_products", "按销售额查询从2026-10-01（含）到2026-10-04（不含）的前2个商品"),
])
@pytest.mark.parametrize("qualifier,status", [
    ("排除周末", "unsupported"),
    ("销售额大于100元", "needs_clarification"),
    ("紫色限定款", "needs_clarification"),
])
def test_extra_qualifier_http_contract_rejects_even_model_ready(model_mode, query_type, question, qualifier, status):
    repo = Repo(rows=[])
    adapter = MockModelAdapter() if model_mode == "mock" else Adapter(plan={
        "query_type": query_type, "start_date": "2026-10-01", "end_date": "2026-10-04",
        "rank_by": "sales_amount" if query_type == "top_products" else None,
        "top_n": 2 if query_type == "top_products" else None,
    })
    with client(adapter, repo, model_mode=model_mode) as api:
        response = api.post("/api/query", json={"question": question + "，" + qualifier})
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == status and body["model_mode"] == model_mode
    assert body["plan"] is None and body["query"] is None and body["data"] == []
    assert repo.calls == 0
    assert all(event["node"] != "execute_query" for event in body["trace"])


@pytest.mark.parametrize("model_mode", ["mock", "live"])
@pytest.mark.parametrize("question", [
    "查询从2026-10-01（含）到2026-10-04（不含）的每日销售额和销量",
    "查询从2026-10-01（含）到2026-10-03（含）的每日销售额和销量",
])
def test_supported_date_annotations_http_positive_regression(model_mode, question):
    repo = Repo()
    adapter = MockModelAdapter() if model_mode == "mock" else Adapter()
    with client(adapter, repo, model_mode=model_mode) as api:
        response = api.post("/api/query", json={"question": question})
    body = response.json()
    assert response.status_code == 200 and body["status"] == "success"
    assert body["plan"]["end_date"] == "2026-10-04"
    assert body["query"] is not None and repo.calls == 1
