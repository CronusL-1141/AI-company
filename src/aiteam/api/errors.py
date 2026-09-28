"""AI Team OS — Unified error handling.

Registers global exception handlers for FastAPI.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from aiteam.api.exceptions import NotFoundError
from aiteam.types import INVISIBLE_TEXT_ERROR

logger = logging.getLogger(__name__)


class ErrorResponse(BaseModel):
    """Error response model."""

    success: bool = False
    error: str
    detail: str = ""


def refusal_body(message: str, category: str, pattern: str, field: str) -> dict:
    """The memo-style refusal: 200, success false, the finding and a safety block."""
    return {
        "success": False,
        "error": message,
        "safety": {"category": category, "pattern": pattern, "field": field},
    }


def register_error_handlers(app: FastAPI) -> None:
    """Register global exception handlers."""

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
        """NotFoundError -> 404 (resource not found)."""
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(error="not_found", detail=str(exc)).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Invisible characters in long text -> the memo-style refusal; anything else -> 422.

        A body whose only problem is an invisible character is well formed; the
        author just has to rewrite that text. It gets the same answer task_memo_add
        has always given: 200, success false, the finding and a safety block. A body
        with any other validation error keeps FastAPI's 422.
        """
        errors = exc.errors()
        if errors and all(error.get("type") == INVISIBLE_TEXT_ERROR for error in errors):
            first = errors[0]
            context = first.get("ctx") or {}
            field = ".".join(str(part) for part in first.get("loc", ())[1:])
            return JSONResponse(
                status_code=200,
                content=refusal_body(context.get("message", ""), context.get("category", INVISIBLE_TEXT_ERROR),
                                     context.get("pattern", ""), field),
            )
        return await request_validation_exception_handler(request, exc)

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
