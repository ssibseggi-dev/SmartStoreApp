"""실행 결과 → 카카오톡 알림 문구 (텍스트 템플릿 200자 안에 들어가게 줄인다)."""
from __future__ import annotations

from datetime import datetime

from services.automation.input_sheet import STATUS_FAILED, STATUS_WAITING
from services.notify.kakao import TEXT_LIMIT, fit_text

_MAX_FAILURES = 3


def _short_reason(message: str, limit: int = 18) -> str:
    first = message.split(";")[0].split("—")[0].split(" (")[0].strip()
    for prefix in ("판매 시작 실패: ", "이미지 다운로드 실패: ", "사진 보정 실패: "):
        first = first.removeprefix(prefix)
    return first if len(first) <= limit else first[:limit - 1] + "…"


def run_summary(report, pending_review: int, now: datetime | None = None) -> str:
    """예: [스마트스토어] 11/02 10:00 자동 등록\n등록 8건 / 실패 1건 / 검수 대기 12건\n실패: A005 사진이 없습니다"""
    now = now or datetime.now()
    failed = [(code, r.message) for code, r in report.results.items() if r.status == STATUS_FAILED]
    waiting = sum(1 for r in report.results.values() if r.status == STATUS_WAITING)
    lines = [f"[스마트스토어] {now:%m/%d %H:%M} 자동 등록",
             f"등록 {report.registered_count}건 / 실패 {len(failed)}건 / 검수 대기 {pending_review}건"]
    if waiting:
        lines.append(f"하루 한도로 다음에 등록: {waiting}건")
    if not report.prepared:
        lines.append("처리할 '대기' 상품이 없었습니다.")
    if failed:
        shown = ", ".join(f"{code} {_short_reason(msg)}" for code, msg in failed[:_MAX_FAILURES])
        more = f" 외 {len(failed) - _MAX_FAILURES}건" if len(failed) > _MAX_FAILURES else ""
        lines.append(f"실패: {shown}{more}")
    ai = getattr(report, "ai_registered", 0)
    if ai:
        lines.append(f"AI 초안 {ai}건 검수 필요")
    sort = getattr(report, "photo_sort", None)
    if sort and sort.unsorted:
        lines.append(f"미분류 사진 {len(sort.unsorted)}장 확인 필요")
    if not report.excel_written:
        lines.append("작업 엑셀이 열려 있어 결과는 다음 실행 때 반영됩니다.")
    return fit_text("\n".join(lines), TEXT_LIMIT)


def error_summary(error: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    return fit_text(f"[스마트스토어] {now:%m/%d %H:%M} 자동 등록 실패\n{error}", TEXT_LIMIT)
