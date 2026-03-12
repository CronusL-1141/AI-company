"""AI Team OS — 统一错误处理.

为FastAPI注册全局异常处理器。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """错误响应模型."""

    success: bool = False
    error: str
    detail: str = ""


def register_error_handlers(app: FastAPI) -> None:
    """注册全局异常处理器."""

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        """ValueError → 404（资源不存在）."""
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(error="not_found", detail=str(exc)).model_dump(),
        )

    @app.exception_handler(Exception)
    async def general_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """通用异常 → 500."""
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(error="internal_error", detail=str(exc)).model_dump(),
        )
