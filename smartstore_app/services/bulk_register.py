"""엑셀 행 일괄 등록 — 행마다 단일 등록 흐름을 순서대로 실행하고 결과를 모은다."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from api.errors import ApiError, NetworkError
from services.connection import AppServices
from services.excel_import import ExcelRow
from services.product_service import register_product

log = logging.getLogger(__name__)


@dataclass
class RowResult:
    row_no: int
    ok: bool
    origin_product_no: int | None = None
    sale_status: str = ""
    message: str = ""

    def as_dict(self) -> dict:
        return {"status": "성공" if self.ok else "실패", "origin_product_no": self.origin_product_no,
                "sale_status": {"SALE": "판매중", "SUSPENSION": "판매중지"}.get(self.sale_status, self.sale_status),
                "message": self.message}


def api_error_message(e: Exception) -> str:
    if isinstance(e, ApiError):
        details = [f"{i.get('name')}: {i.get('message')}" for i in e.invalid_inputs if isinstance(i, dict)]
        return e.message + (" — " + "; ".join(details) if details else "")
    return str(e)


def register_rows(services: AppServices, rows: list[ExcelRow],
                  on_progress: Callable[[RowResult, int, int], None] | None = None,
                  is_cancelled: Callable[[], bool] = lambda: False) -> list[RowResult]:
    """검증을 통과한 행만 등록한다. 한 행이 실패해도 다음 행을 계속 진행한다."""
    targets = [r for r in rows if r.ok]
    results: list[RowResult] = []
    for i, row in enumerate(targets, start=1):
        if is_cancelled():
            log.info("일괄 등록 중지: %d/%d 처리", i - 1, len(targets))
            break
        try:
            outcome = register_product(services, row.draft)
            result = RowResult(row.row_no, True, outcome.origin_product_no, outcome.status, outcome.warning or "")
        except NetworkError as e:
            result = RowResult(row.row_no, False, message=f"네트워크 오류: {e}")
        except (ApiError, ValueError, OSError) as e:
            result = RowResult(row.row_no, False, message=api_error_message(e))
        log.info("일괄 등록 %d/%d (엑셀 %d행): %s %s", i, len(targets), row.row_no,
                 "성공" if result.ok else "실패", result.origin_product_no or result.message)
        results.append(result)
        if on_progress:
            on_progress(result, i, len(targets))
    return results
