"""A request-scoped LangGraph workflow with bounded parsing and safe exits."""

from typing import Any, Literal, TypedDict
import time
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.errors import AppError
from app.formatting import format_answer, json_safe, serialize_rows
from app.plan_validation import FOLLOWUP, validate_plan
from app.schemas import CandidatePlan, ModelDecision, QueryDetails, QueryError, QueryResponse, ValidatedPlan


WorkflowStatus = Literal["pending", "parsed", "ready", "success", "needs_clarification", "unsupported", "failed"]


class RequestState(TypedDict):
    request_id: str
    question: str
    model_mode: Literal["mock", "live"]
    parse_attempts: int
    model_deadline: float | None
    model_decision: ModelDecision | None
    candidate_plan: CandidatePlan | None
    validated_plan: ValidatedPlan | None
    status: WorkflowStatus
    clarification: str
    template_id: str | None
    sql_template: str | None
    query_params: list | tuple | None
    rows: list[dict[str, Any]]
    error_code: str | None
    safe_error_message: str | None
    error_category: str | None
    http_status: int
    trace: list[dict]
    answer: str


def create_state(question: str, model_mode: str, request_id: str | None = None) -> RequestState:
    if model_mode not in ("mock", "live"):
        raise ValueError("model_mode must be mock or live")
    return {
        "request_id": request_id or str(uuid4()), "question": question, "model_mode": model_mode,
        "parse_attempts": 0, "model_deadline": None, "model_decision": None,
        "candidate_plan": None, "validated_plan": None, "status": "pending", "clarification": "",
        "template_id": None, "sql_template": None, "query_params": None, "rows": [],
        "error_code": None, "safe_error_message": None, "error_category": None,
        "http_status": 200, "trace": [], "answer": "",
    }


def _trace(state: RequestState, node: str, result: str, start: float, *, attempt: int | None = None):
    event = {"node": node, "result": result, "duration_ms": round((time.perf_counter() - start) * 1000, 3)}
    if attempt is not None:
        event["attempt"] = attempt
    return [*state["trace"], event]


def _failure(error: AppError):
    return {
        "status": "failed", "error_code": error.code, "safe_error_message": error.message,
        "error_category": error.category, "http_status": error.http_status, "rows": [],
    }


def build_graph(adapter, repository, settings):
    def parse_plan(state: RequestState):
        start = time.perf_counter()
        attempt = state["parse_attempts"] + 1
        deadline = state["model_deadline"]
        if deadline is None:
            deadline = time.monotonic() + settings.model_total_timeout
        updates = {
            "parse_attempts": attempt, "model_deadline": deadline,
            "candidate_plan": None, "model_decision": None,
        }
        try:
            decision = adapter.parse(state["question"], deadline=deadline)
            if not isinstance(decision, ModelDecision):
                decision = ModelDecision.model_validate(decision)
            updates.update(
                status="parsed", model_decision=decision, candidate_plan=decision.plan,
                error_code=None, safe_error_message=None, error_category=None,
            )
            result = "parsed"
        except (AppError, ValidationError) as error:
            if isinstance(error, ValidationError):
                error = AppError("MODEL_OUTPUT_INVALID", "模型没有返回符合约束的查询计划。", 502, "format")
            if error.code == "MODEL_OUTPUT_INVALID" and attempt < 2:
                updates.update(status="pending")
                result = "format_retry"
            else:
                updates.update(_failure(error))
                result = error.code + (":" + error.category if error.category else "")
        except Exception:
            updates.update(_failure(AppError("INTERNAL_ERROR", "服务内部错误，请稍后重试。", 500)))
            result = "INTERNAL_ERROR"
        updates["trace"] = _trace(state, "parse_plan", result, start, attempt=attempt)
        return updates

    def route_parse(state: RequestState) -> Literal["parse_plan", "validate_plan", "format_answer"]:
        if state["status"] == "pending" and state["parse_attempts"] < 2:
            return "parse_plan"
        return "validate_plan" if state["status"] == "parsed" else "format_answer"

    def validate_plan_node(state: RequestState):
        start = time.perf_counter()
        decision = state["model_decision"]
        try:
            outcome = validate_plan(
                state["candidate_plan"], decision=decision.decision,
                message=decision.message, question=state["question"],
            )
            updates = {"status": outcome.status, "validated_plan": outcome.plan, "clarification": outcome.message}
            result = outcome.status
        except Exception:
            updates = _failure(AppError("INTERNAL_ERROR", "查询计划校验失败，请稍后重试。", 500))
            result = "INTERNAL_ERROR"
        updates["trace"] = _trace(state, "validate_plan", result, start)
        return updates

    def route_validation(state: RequestState) -> Literal["execute_query", "format_answer"]:
        return "execute_query" if state["status"] == "ready" else "format_answer"

    def execute_query(state: RequestState):
        start = time.perf_counter()
        try:
            execution = repository.execute(state["validated_plan"])
            updates = {
                "status": "success", "template_id": execution["template_id"],
                "sql_template": execution["sql_template"], "query_params": execution["query_params"],
                "rows": execution["rows"],
            }
            result = "success"
        except AppError as error:
            if error.category == "needs_clarification":
                updates = {
                    "status": "needs_clarification", "clarification": error.message + FOLLOWUP,
                    "error_code": error.code, "safe_error_message": error.message,
                    "error_category": error.category, "http_status": 200, "rows": [],
                }
            else:
                updates = _failure(error)
            query = getattr(error, "query", None)
            if query is not None:
                updates.update({key: query[key] for key in ("template_id", "sql_template", "query_params")})
            result = error.code
        except Exception:
            updates = _failure(AppError("INTERNAL_ERROR", "查询执行发生内部错误，请稍后重试。", 500))
            result = "INTERNAL_ERROR"
        updates["trace"] = _trace(state, "execute_query", result, start)
        return updates

    def format_answer_node(state: RequestState):
        start = time.perf_counter()
        updates = {}
        try:
            answer = format_answer(state)
            result = state["status"]
        except AppError as error:
            updates.update(_failure(error))
            answer = "查询失败：" + error.message
            result = error.code
        except Exception:
            updates.update(_failure(AppError("INTERNAL_ERROR", "结果格式化失败，请稍后重试。", 500)))
            answer = "查询失败：结果格式化失败，请稍后重试。"
            result = "INTERNAL_ERROR"
        updates.update(answer=answer, trace=_trace(state, "format_answer", result, start))
        return updates

    builder = StateGraph(RequestState)
    builder.add_node("parse_plan", parse_plan)
    builder.add_node("validate_plan", validate_plan_node)
    builder.add_node("execute_query", execute_query)
    builder.add_node("format_answer", format_answer_node)
    builder.add_edge(START, "parse_plan")
    builder.add_conditional_edges("parse_plan", route_parse, {name: name for name in ("parse_plan", "validate_plan", "format_answer")})
    builder.add_conditional_edges("validate_plan", route_validation, {name: name for name in ("execute_query", "format_answer")})
    builder.add_edge("execute_query", "format_answer")
    builder.add_edge("format_answer", END)
    return builder.compile()


def to_response(state: RequestState) -> QueryResponse:
    error = None
    if state["error_code"] is not None:
        error = QueryError(code=state["error_code"], message=state["safe_error_message"], category=state["error_category"])
    query = None
    if state["template_id"] is not None:
        query = QueryDetails(
            template_id=state["template_id"], sql_template=state["sql_template"], params=json_safe(state["query_params"])
        )
    return QueryResponse(
        request_id=state["request_id"], status=state["status"], model_mode=state["model_mode"],
        answer=state["answer"], plan=state["validated_plan"],
        data=serialize_rows(state["rows"]) if state["status"] == "success" else [],
        trace=state["trace"], error=error, query=query,
    )
