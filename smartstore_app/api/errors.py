"""커머스API 예외 정의."""
from __future__ import annotations


class ApiError(Exception):
    """커머스API가 오류 응답을 돌려준 경우.

    오류 응답 본문 예: {"code": "...", "message": "...", "traceId": "...", "invalidInputs": [...]}
    """

    def __init__(self, status: int, code: str | None, message: str,
                 trace_id: str | None = None, invalid_inputs: list | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.trace_id = trace_id
        self.invalid_inputs = invalid_inputs or []
        super().__init__(self.__str__())

    @classmethod
    def from_response(cls, resp) -> "ApiError":
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        message = body.get("message") or body.get("error_description") or resp.text[:300] or resp.reason
        return cls(
            status=resp.status_code,
            code=body.get("code") or body.get("error"),
            message=message,
            trace_id=body.get("traceId"),
            invalid_inputs=body.get("invalidInputs"),
        )

    def __str__(self) -> str:
        parts = [f"[HTTP {self.status}]"]
        if self.code:
            parts.append(self.code)
        parts.append(self.message)
        if self.trace_id:
            parts.append(f"(traceId: {self.trace_id})")
        return " ".join(parts)


class AuthError(ApiError):
    """토큰 발급 실패 (ID/시크릿 오류, IP 미등록 등)."""


class NetworkError(Exception):
    """연결 실패·타임아웃 등 응답을 받지 못한 경우."""
