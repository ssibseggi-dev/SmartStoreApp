"""실행이 끝난 뒤 알림 보내기. 알림이 실패해도 등록 작업은 실패로 치지 않고 run_log에만 남긴다."""
from __future__ import annotations

import logging

from services.automation.config import load_settings
from services.automation.review import pending_review
from services.notify import kakao
from services.notify.summary import error_summary, run_summary

log = logging.getLogger(__name__)


def _send(db, text: str, run_id: str | None) -> str:
    settings = load_settings(db)
    try:
        kakao.send_to_me(text, settings.kakao_link_url)
    except kakao.KakaoReconnectRequired as e:
        db.add_run_log(run_id or "-", "notify", "실패", summary="카카오톡", error=f"카카오톡 재연결 필요: {e}")
        log.warning("카카오톡 알림 실패(재연결 필요): %s", e)
        return "카카오톡 알림 실패: 재연결 필요 (설정 → 예약·알림)"
    except kakao.KakaoError as e:
        db.add_run_log(run_id or "-", "notify", "실패", summary="카카오톡", error=str(e))
        log.warning("카카오톡 알림 실패: %s", e)
        return f"카카오톡 알림 실패: {e}"
    db.add_run_log(run_id or "-", "notify", "성공", summary=f"카카오톡 {len(text)}자")
    return "카카오톡 알림을 보냈습니다."


def notify_run(db, report, source: str) -> str | None:
    """source: "schedule"(예약·CLI --notify) | "app". 알림을 보내지 않으면 None."""
    if report.dry_run:
        return None
    settings = load_settings(db)
    if not settings.kakao_enabled or (source == "app" and not settings.kakao_notify_app):
        return None
    return _send(db, run_summary(report, len(pending_review(db))), report.run_id)


def notify_error(db, error: str) -> str | None:
    if not load_settings(db).kakao_enabled:
        return None
    return _send(db, error_summary(error), None)
