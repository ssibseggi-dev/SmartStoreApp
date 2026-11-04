"""검수 — '등록완료(판매중지)' 상품 목록과 일괄 판매 시작·삭제, 삭제 내역과 다시 등록.

판매 시작 = 판매중 + 전시중(+ 설정에 따라 네이버쇼핑 노출). 자동화는 판매중지·전시중지로 등록하므로 둘 다 바꾼다.
결과는 DB와 작업 엑셀(상태 칸)에 기록한다. 엑셀이 열려 있으면 DB에 두고 다음 실행 때 반영한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from api.errors import ApiError, NetworkError
from services.automation.config import load_settings
from api.products import ProductApi
from services.automation.input_sheet import (
    STATUS_DELETED, STATUS_ON_SALE, STATUS_REGISTERED, STATUS_WAITING, ResultUpdate, read_items, write_results,
)
from services.automation.run_lock import RunLock
from services.automation.runner import lock_path, new_run_id, run_waiting
from services.bulk_register import api_error_message
from services.images import product_images
from services.product_manager import load_edit, save_edit, validate_edit
from storage.db import app_data_dir

log = logging.getLogger(__name__)


@dataclass
class ReviewItem:
    code: str
    origin_product_no: int
    name: str
    sale_price: int | None
    ai_draft: bool
    message: str
    registered_at: str
    image: Path | None          # 대표 이미지(보정본) — 화면 썸네일
    detail_page: Path | None    # 등록에 쓴 상세페이지 HTML


@dataclass
class PublishResult:
    code: str
    ok: bool
    message: str


def registered_detail_page(code: str) -> Path | None:
    path = app_data_dir() / "상세페이지_등록본" / f"{code}.html"
    return path if path.is_file() else None


def pending_review(db) -> list[ReviewItem]:
    root = load_settings(db).image_root_path()
    items = []
    for r in db.automation_items_by_status(STATUS_REGISTERED):
        if not r["origin_product_no"]:
            continue
        images = product_images(root, r["code"])
        items.append(ReviewItem(
            code=r["code"], origin_product_no=int(r["origin_product_no"]), name=r["name"] or r["code"],
            sale_price=r["sale_price"], ai_draft=bool(r["ai_draft"]), message=r["message"] or "",
            registered_at=r["registered_at"] or r["updated_at"], image=images.representative,
            detail_page=registered_detail_page(r["code"]),
        ))
    return items


def publish(services, codes: list[str],
            on_progress: Callable[[str, int, int], None] | None = None) -> tuple[list[PublishResult], bool]:
    """검수가 끝난 상품을 판매 시작한다. (결과 목록, 엑셀 기록 성공 여부)를 돌려준다."""
    db = services.db
    with RunLock(lock_path(), "publish"):
        settings = load_settings(db)
        run_id = new_run_id()
        records = {r["code"]: r for r in db.automation_items_by_status(STATUS_REGISTERED)}
        results: list[PublishResult] = []
        for index, code in enumerate(codes, start=1):
            record = records.get(code)
            if record is None or not record["origin_product_no"]:
                results.append(PublishResult(code, False, "검수 대기(등록완료·판매중지) 상품이 아닙니다."))
            else:
                results.append(_publish_one(services, record, settings, run_id))
            if on_progress:
                on_progress(code, index, len(codes))

        updates = {r.code: ResultUpdate(STATUS_ON_SALE, records[r.code]["origin_product_no"], r.message)
                   for r in results if r.ok}
        excel_written = True
        try:
            write_results(settings.work_excel_path(), updates)
        except OSError as e:
            excel_written = False
            log.warning("판매 시작 결과를 엑셀에 쓰지 못함(DB 보관): %s", e)
        for r in results:
            if r.ok:
                db.upsert_automation_item(r.code, STATUS_ON_SALE, message=r.message, excel_synced=excel_written)
        return results, excel_written


def _publish_one(services, record, settings, run_id: str) -> PublishResult:
    code, origin_no = record["code"], int(record["origin_product_no"])
    try:
        edit = load_edit(services, origin_no)
        edit.status, edit.display_status = "SALE", "ON"
        edit.naver_shopping = settings.publish_naver_shopping
        errors = validate_edit(edit)
        if errors:
            raise ValueError("; ".join(errors))
        save_edit(services, edit)
    except (ApiError, NetworkError, ValueError) as e:
        message = api_error_message(e) if isinstance(e, ApiError) else str(e)
        if isinstance(e, ApiError) and e.status == 404:
            message = "스마트스토어에 상품이 없습니다(삭제됨?). " + message
        db_error = f"판매 시작 실패: {message}"
        services.db.add_run_log(run_id, "publish", "실패", code=code, error=db_error)
        log.warning("[%s] %s", code, db_error)
        return PublishResult(code, False, db_error)
    note = "판매 시작" + (" · 네이버쇼핑 노출" if settings.publish_naver_shopping else "")
    services.db.add_run_log(run_id, "publish", "성공", code=code, summary=f"원상품번호 {origin_no} · {note}")
    log.info("[%s] %s (원상품번호 %s)", code, note, origin_no)
    return PublishResult(code, True, note)


def delete_registered(services, codes: list[str],
                      on_progress: Callable[[str, int, int], None] | None = None) -> tuple[list[PublishResult], bool]:
    """검수 대기 상품을 스마트스토어에서 삭제한다 (되돌릴 수 없음). (결과 목록, 엑셀 기록 성공 여부).

    판매 중인 상품은 지우지 않는다(검수 대기 = 판매중지 상품만). 스토어에 이미 없으면 목록에서만 뺀다.
    작업 엑셀의 상태는 '삭제됨'으로 바꾼다 (행은 지우지 않음).
    """
    db = services.db
    with RunLock(lock_path(), "delete"):
        settings = load_settings(db)
        run_id = new_run_id()
        records = {r["code"]: r for r in db.automation_items_by_status(STATUS_REGISTERED)}
        api = ProductApi(services.client())
        results: list[PublishResult] = []
        for index, code in enumerate(codes, start=1):
            record = records.get(code)
            if record is None or not record["origin_product_no"]:
                results.append(PublishResult(code, False, "검수 대기(등록완료·판매중지) 상품이 아닙니다."))
            else:
                origin_no = int(record["origin_product_no"])
                try:
                    api.delete_origin_product(origin_no)
                    result = PublishResult(code, True, f"스마트스토어에서 삭제 (원상품번호 {origin_no})")
                except ApiError as e:
                    if e.status == 404:
                        result = PublishResult(code, True, f"스마트스토어에 이미 없어 목록에서만 뺐습니다 (원상품번호 {origin_no})")
                    else:
                        result = PublishResult(code, False, f"삭제 실패: {api_error_message(e)}")
                except NetworkError as e:
                    result = PublishResult(code, False, f"삭제 실패: {e}")
                db.add_run_log(run_id, "delete", "성공" if result.ok else "실패", code=code,
                               summary=result.message if result.ok else None, error=None if result.ok else result.message)
                log.info("[%s] %s", code, result.message)
                results.append(result)
            if on_progress:
                on_progress(code, index, len(codes))

        updates = {r.code: ResultUpdate(STATUS_DELETED, None, r.message) for r in results if r.ok}
        excel_written = True
        try:
            write_results(settings.work_excel_path(), updates)
        except OSError as e:
            excel_written = False
            log.warning("삭제 결과를 엑셀에 쓰지 못함(DB 보관): %s", e)
        for r in results:
            if r.ok:
                db.upsert_automation_item(r.code, STATUS_DELETED, message=r.message, excel_synced=excel_written)
        return results, excel_written


# --- 삭제 내역 · 다시 등록 ---
@dataclass
class DeletedItem:
    code: str
    old_product_no: int | None   # 삭제 전 네이버 상품번호 (다시 등록하면 새 번호가 붙음)
    name: str
    sale_price: int | None
    deleted_at: str
    image: Path | None
    in_excel: bool               # 작업 엑셀에 그 상품 줄이 있어야 다시 등록할 수 있다


def deleted_items(db) -> list[DeletedItem]:
    settings = load_settings(db)
    try:
        excel_codes = {i.code for i in read_items(settings.work_excel_path()) if i.code}
    except (OSError, ValueError):
        excel_codes = set()
    root = settings.image_root_path()
    rows = sorted(db.automation_items_by_status(STATUS_DELETED), key=lambda r: r["updated_at"] or "", reverse=True)
    return [DeletedItem(code=r["code"], old_product_no=r["origin_product_no"], name=r["name"] or r["code"],
                        sale_price=r["sale_price"], deleted_at=r["updated_at"] or "",
                        image=product_images(root, r["code"]).representative, in_excel=r["code"] in excel_codes)
            for r in rows]


def reregister(services, codes: list[str], on_progress: Callable[[str, int, int], None] | None = None):
    """삭제한 상품을 작업 엑셀의 줄과 사진 폴더 그대로 다시 등록한다 (판매중지, 새 상품번호). RunReport를 돌려준다.

    엑셀 상태를 '대기'로 돌린 뒤 그 상품코드만 등록한다. 검증·중복 확인·하루 한도는 평소 등록과 같다.
    엑셀이 열려 있으면 PermissionError (상태를 바꿀 수 없어 등록하지 않음).
    """
    db = services.db
    deleted = {d.code: d for d in deleted_items(db)}
    targets = [c for c in codes if c in deleted and deleted[c].in_excel]
    if not targets:
        raise ValueError("다시 등록할 수 있는 상품이 없습니다. 작업 엑셀에 그 상품 줄이 있어야 합니다.")
    excel = load_settings(db).work_excel_path()
    with RunLock(lock_path(), "reregister"):
        try:
            write_results(excel, {c: ResultUpdate(STATUS_WAITING, None, "다시 등록 대기", sets={"네이버상품번호": None})
                                  for c in targets})
        except PermissionError:
            raise PermissionError("작업 엑셀이 열려 있어 다시 등록할 수 없습니다. 엑셀을 닫고 다시 누르세요.") from None
        for c in targets:
            db.upsert_automation_item(c, STATUS_WAITING, message="다시 등록 대기", excel_synced=True)
    return run_waiting(services, dry_run=False, codes=targets, on_progress=on_progress)


def forget_deleted(db, codes: list[str]) -> int:
    """삭제 내역에서 치운다 (스토어·엑셀에는 영향 없음)."""
    deleted = {d.code for d in deleted_items(db)}
    for code in codes:
        if code in deleted:
            db.remove_automation_item(code)
    return len([c for c in codes if c in deleted])
