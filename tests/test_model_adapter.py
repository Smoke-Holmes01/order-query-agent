import asyncio
import json
import time
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest

from app.errors import AppError
from app.model_adapter import LiveModelAdapter, MockModelAdapter, create_adapter


QUESTION = "按销售额查询从2026-10-01（含）到2026-10-04（不含）的前2个商品"
DECISION = {"decision": "ready", "plan": {"query_type": "top_products", "start_date": "2026-10-01", "end_date": "2026-10-04", "rank_by": "sales_amount", "top_n": 2}, "message": ""}


def settings(**changes):
    values = dict(model_mode="live", model_base_url="https://model.example/v1/", model_name="configured-model", model_api_key=SecretStr("test-placeholder"), model_json_mode="json_object", model_total_timeout=15, model_connect_timeout=3, model_read_timeout=10, model_write_timeout=3, model_pool_timeout=2, db_password=SecretStr("database-must-never-be-sent"))
    return SimpleNamespace(**(values | changes))


def adapter_with(handler, **changes):
    return LiveModelAdapter(settings(**changes), transport=httpx.MockTransport(handler))


def test_mock_parses_supported_explicit_expression_without_results():
    result = MockModelAdapter().parse(QUESTION, deadline=time.monotonic() + 15)
    assert result.decision == "ready" and result.plan.top_n == 2
    assert result.plan.rank_by == "sales_amount"
    assert set(result.model_dump()) == {"decision", "plan", "message"}


def test_mock_inclusive_end_and_amount_quantity_branch():
    result = MockModelAdapter().parse("按销量查询从2026-10-01（含）到2026-10-03（含）的前1个商品", deadline=time.monotonic() + 15)
    assert result.plan.end_date == "2026-10-04"
    assert result.plan.rank_by == "sales_quantity" and result.decision == "ready"


@pytest.mark.parametrize("question,decision", [("昨天销量", "needs_clarification"), ("预测销售额", "unsupported"), ("DELETE FROM orders", "unsupported"), ("从2026-10-01到2026-10-04的每日销售额", "needs_clarification")])
def test_mock_honest_limits(question, decision):
    assert MockModelAdapter().parse(question, deadline=time.monotonic() + 15).decision == decision


@pytest.mark.parametrize("question", [
    "按销售额和按销量查询从2026-10-01（含）到2026-10-04（不含）的前2个商品",
    "按销售额和销量查询从2026-10-01（含）到2026-10-04（不含）的前2个商品",
    "查询从2026-10-01（含）到2026-10-04（不含）的每日品类销售额和销量",
])
def test_mock_does_not_choose_between_conflicting_supported_intents(question):
    assert MockModelAdapter().parse(question, deadline=time.monotonic() + 15).decision == "needs_clarification"


@pytest.mark.parametrize("mode", ["json_object", "json_schema"])
def test_live_uses_explicit_json_mode_and_sends_no_database_credentials(mode):
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(DECISION)}}]})
    result = adapter_with(handler, model_json_mode=mode).parse(QUESTION, deadline=time.monotonic() + 15)
    assert result.plan.top_n == 2 and len(captured) == 1
    request = captured[0]
    payload = json.loads(request.content)
    assert str(request.url) == "https://model.example/v1/chat/completions"
    assert payload["response_format"]["type"] == mode
    assert "database-must-never-be-sent" not in request.content.decode()
    assert "test-placeholder" not in request.content.decode()
    if mode == "json_schema":
        schema = payload["response_format"]["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        candidate = schema["$defs"]["CandidatePlan"]
        assert candidate["required"] == list(candidate["properties"])


@pytest.mark.parametrize("body", [
    {"choices": []}, {"choices": [{"message": {"content": "not JSON"}}]},
    {"choices": [{"message": {"content": json.dumps(DECISION | {"sql": "SELECT 1"})}}]},
    {"choices": [{"message": {"content": json.dumps(DECISION | {"plan": DECISION["plan"] | {"top_n": True}})}}]},
])
def test_invalid_outputs_are_typed_format_error(body):
    with pytest.raises(AppError) as caught:
        adapter_with(lambda request: httpx.Response(200, json=body)).parse(QUESTION, deadline=time.monotonic() + 15)
    assert caught.value.code == "MODEL_OUTPUT_INVALID"


@pytest.mark.parametrize("status,category", [(401, "authentication"), (403, "authentication"), (429, "rate_limit"), (500, "http_error"), (302, "http_error")])
def test_provider_errors_are_classified_and_body_redacted(status, category):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="sensitive-provider-response", headers={"Location": "https://elsewhere.example/"})
    with pytest.raises(AppError) as caught:
        adapter_with(handler).parse(QUESTION, deadline=time.monotonic() + 15)
    assert caught.value.code == "MODEL_PROVIDER_ERROR" and caught.value.category == category
    assert "sensitive" not in str(caught.value) and len(calls) == 1


def test_network_error_classification():
    def handler(request):
        raise httpx.ConnectError("sensitive network detail", request=request)
    with pytest.raises(AppError) as caught:
        adapter_with(handler).parse(QUESTION, deadline=time.monotonic() + 15)
    assert caught.value.code == "MODEL_PROVIDER_ERROR" and caught.value.category == "network"
    assert "sensitive" not in str(caught.value)


def test_socket_timeout_classification():
    def handler(request):
        raise httpx.ReadTimeout("sensitive timeout detail", request=request)
    with pytest.raises(AppError) as caught:
        adapter_with(handler).parse(QUESTION, deadline=time.monotonic() + 15)
    assert caught.value.code == "MODEL_TIMEOUT" and caught.value.http_status == 504


def test_total_deadline_cancels_waiting_provider():
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})
    start = time.monotonic()
    with pytest.raises(AppError) as caught:
        adapter_with(handler).parse(QUESTION, deadline=start + 0.02)
    assert caught.value.code == "MODEL_TIMEOUT"
    assert time.monotonic() - start < 0.75


def test_response_body_limit_is_enforced():
    with pytest.raises(AppError) as caught:
        adapter_with(lambda request: httpx.Response(200, content=b"x" * 65537)).parse(QUESTION, deadline=time.monotonic() + 15)
    assert caught.value.code == "MODEL_OUTPUT_INVALID"


def test_live_missing_config_never_falls_back_to_mock():
    with pytest.raises(ValueError):
        create_adapter(settings(model_api_key=SecretStr("")))
