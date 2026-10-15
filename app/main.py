"""HTTP boundary. Blocking graph and PyMySQL run in FastAPI's worker threads."""

from contextlib import asynccontextmanager
import logging
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import Settings, load_settings
from app.errors import AppError
from app.graph import build_graph, create_state, to_response
from app.model_adapter import create_adapter
from app.repository import Repository
from app.schemas import QueryRequest, QueryResponse

logger = logging.getLogger("order_query")


def create_app(settings: Settings | None = None, *, adapter=None, repository=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        # No DB call on the event loop and no paid model call on startup.
        runtime = settings if settings is not None else load_settings()
        repo = repository if repository is not None else Repository(runtime)
        model = adapter if adapter is not None else create_adapter(runtime)
        application.state.settings = runtime
        application.state.repository = repo
        application.state.graph = build_graph(model, repo, runtime)
        yield

    application = FastAPI(
        title="电商经营分析工作流助手",
        description="受限工具型电商查询 Agent。业务数据均为模拟数据；仅支持三个固定查询类型。",
        version="1.0.0",
        lifespan=lifespan,
    )

    @application.get("/health")
    def health():
        runtime = application.state.settings
        try:
            result = application.state.repository.health()
            return {**result, "model_mode": runtime.model_mode}
        except AppError as exc:
            return JSONResponse(status_code=503, content={
                "model_mode": runtime.model_mode, "database": "mysql", "database_ready": False,
                "error": {"code": exc.code, "message": exc.message},
            })
        except Exception:
            return JSONResponse(status_code=503, content={
                "model_mode": runtime.model_mode, "database": "mysql", "database_ready": False,
                "error": {"code": "DB_UNAVAILABLE", "message": "数据库暂不可用。"},
            })

    @application.post("/api/query", response_model=QueryResponse, responses={
        500: {"model": QueryResponse}, 502: {"model": QueryResponse},
        503: {"model": QueryResponse}, 504: {"model": QueryResponse},
    })
    def query(body: QueryRequest):
        request_id = str(uuid4())
        state = create_state(body.question, application.state.settings.model_mode, request_id=request_id)
        try:
            result = application.state.graph.invoke(state, config={"recursion_limit": 8})
            response = to_response(result)
            http_status = result.get("http_status", 200)
        except Exception:
            # Avoid traceback, raw model/DB errors, secrets and user question in logs.
            logger.error("request_id=%s code=INTERNAL_ERROR", request_id)
            response = QueryResponse(
                request_id=request_id, status="failed", model_mode=application.state.settings.model_mode,
                answer="请求处理失败，请使用 request_id 联系维护者。", plan=None, data=[],
                trace=state.get("trace", []), query=None,
                error={"code": "INTERNAL_ERROR", "message": "请求处理失败。"},
            )
            http_status = 500
        logger.info("request_id=%s mode=%s status=%s", request_id, response.model_mode, response.status)
        return JSONResponse(status_code=http_status, content=response.model_dump(mode="json"))

    return application


app = create_app()
