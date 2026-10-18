"""API error helpers matching docs/API_CONTRACT.md."""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def error_body(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    request_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "requestId": request_id,
        }
    }
    if extra:
        payload.update(extra)
    return payload


def error_response(
    status: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    request_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=error_body(code, message, retryable=retryable, request_id=request_id, extra=extra),
    )


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        request_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.request_id = request_id
        self.extra = extra

    def to_response(self) -> JSONResponse:
        return error_response(
            self.status,
            self.code,
            self.message,
            retryable=self.retryable,
            request_id=self.request_id,
            extra=self.extra,
        )
