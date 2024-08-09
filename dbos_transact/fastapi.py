from typing import Any, Callable

from fastapi import FastAPI
from fastapi import Request as FastAPIRequest

from .context import DBOSContextEnsure, SetWorkflowUUID, assert_current_dbos_context
from .logger import dbos_logger


class Request:
    """
    A serializable subset of the FastAPI Request object
    """

    def __init__(self, req: FastAPIRequest):
        self.headers = req.headers
        self.path_params = req.path_params
        self.query_params = req.query_params
        self.url = req.url
        self.base_url = req.base_url
        self.client = req.client
        self.cookies = req.cookies


def setup_fastapi_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def dbos_fastapi_middleware(
        request: FastAPIRequest, call_next: Callable[..., Any]
    ) -> Any:
        with DBOSContextEnsure():
            ctx = assert_current_dbos_context()
            ctx.request = Request(request)
            workflow_id = request.headers.get("dbos-idempotency-key", "")
            with SetWorkflowUUID(workflow_id):
                response = await call_next(request)
        return response
