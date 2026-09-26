"""AI Team OS — Unified error handling.

Registers global exception handlers for FastAPI.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from aiteam.api.exceptions import NotFoundError

logger = logging.getLogger(__name__)


class ErrorResponse(BaseModel):
    """Error response model."""

    success: bool = False
    error: str
    detail: str = ""


def register_error_handlers(app: FastAPI) -> None:
    """Register global exception handlers."""

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
        """NotFoundError -> 404 (resource not found)."""
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(error="not_found", detail=str(exc)).model_dump(),
        )

    @app.exception_handler(UnicodeError)
    async def unicode_error_handler(request: Request, exc: UnicodeError) -> JSONResponse:
        """UnicodeError -> 400 with a fixed text.

        A UnicodeError is a ValueError, but its message is codec internals (the
        offending character and its position in some string the caller never saw),
        not a statement about the request. Checked before ValueError: handlers are
        looked up along the exception's MRO.
        """
        logger.warning("Unicode error on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error="bad_request", detail="文本含无法编码的字符，详情见服务端日志",
            ).model_dump(),
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        """ValueError -> 400 (bad request parameters)."""
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(error="bad_request", detail=str(exc)).model_dump(),
        )

    @app.exception_handler(Exception)
    async def general_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Generic exception -> 500."""
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(error="internal_error", detail="服务器内部错误").model_dump(),
        )
