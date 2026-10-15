"""Strict public DTOs and the model's deliberately incomplete candidate plan."""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

QueryType = Literal["daily_sales", "category_sales", "top_products"]
RankBy = Literal["sales_amount", "sales_quantity"]
DecisionType = Literal["ready", "needs_clarification", "unsupported"]
ResponseStatus = Literal["success", "needs_clarification", "unsupported", "failed"]
ModelMode = Literal["mock", "live"]


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryRequest(StrictDTO):
    question: StrictStr = Field(min_length=1, max_length=1000)

    @field_validator("question", mode="before")
    @classmethod
    def strip_question(cls, value):
        return value.strip() if isinstance(value, str) else value


class CandidatePlan(StrictDTO):
    query_type: QueryType | None = None
    start_date: StrictStr | None = None
    end_date: StrictStr | None = None
    rank_by: RankBy | None = None
    top_n: StrictInt | None = None


class ModelDecision(StrictDTO):
    decision: DecisionType
    plan: CandidatePlan | None
    message: StrictStr = Field(max_length=300)


class ValidatedPlan(StrictDTO):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_type: QueryType
    start_date: date = Field(strict=True)
    end_date: date = Field(strict=True)
    rank_by: RankBy | None = None
    top_n: StrictInt | None = Field(default=None, ge=1, le=10)

    @model_validator(mode="after")
    def valid_combination(self):
        if not 0 < (self.end_date - self.start_date).days <= 90:
            raise ValueError("date interval must contain 1 to 90 days")
        if self.query_type == "top_products":
            if self.rank_by is None or self.top_n is None:
                raise ValueError("top_products requires rank_by and top_n")
        elif self.rank_by is not None or self.top_n is not None:
            raise ValueError("daily/category plans cannot contain ranking parameters")
        return self


class TraceEvent(StrictDTO):
    node: Literal["parse_plan", "validate_plan", "execute_query", "format_answer"]
    result: str
    duration_ms: float = Field(ge=0)
    attempt: int | None = None


class QueryError(StrictDTO):
    code: str
    message: str
    category: str | None = None


class QueryDetails(StrictDTO):
    template_id: str
    sql_template: str
    params: list[Any]


class QueryResponse(StrictDTO):
    request_id: str
    status: ResponseStatus
    model_mode: ModelMode
    answer: str
    plan: ValidatedPlan | None
    data: list[dict[str, Any]]
    trace: list[TraceEvent]
    error: QueryError | None
    query: QueryDetails | None
