"""예외를 사용자에게 보여줄 한국어 메시지로 변환."""
from __future__ import annotations

from api.errors import ApiError, AuthError, NetworkError
from services.connection import NotConfiguredError


def describe_error(exc: Exception) -> str:
    if isinstance(exc, NotConfiguredError):
        return str(exc)
    if isinstance(exc, NetworkError):
        return f"네트워크 오류: {exc}\n인터넷 연결을 확인하세요."
    if isinstance(exc, ApiError):
        hint = ""
        if exc.status == 403 or (exc.code and "IP" in exc.code.upper()):
            hint = ("\n\n이 PC의 공인 IP가 커머스API센터 애플리케이션에 '호출 IP'로 등록되어 있는지 확인하세요.")
        elif isinstance(exc, AuthError) or exc.status in (400, 401):
            hint = "\n\n애플리케이션 ID/시크릿이 정확한지, PC 시계가 정확한지 확인하세요."
        elif exc.status == 429:
            hint = "\n\n호출 한도를 초과했습니다. 잠시 후 다시 시도하세요."
        details = ""
        if exc.invalid_inputs:
            lines = []
            for item in exc.invalid_inputs:
                if isinstance(item, dict):
                    lines.append(f"• {item.get('name', '')}: {item.get('message', '')} ({item.get('type', '')})")
                else:
                    lines.append(f"• {item}")
            details = "\n\n입력값 오류:\n" + "\n".join(lines)
        return f"{exc}{details}{hint}"
    if isinstance(exc, ValueError):
        return str(exc)
    return f"알 수 없는 오류: {exc!r}"
