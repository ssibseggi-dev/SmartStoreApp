"""대기 상품 처리 실행 — 잠금, 검증, 등록(판매중지 검수 모드), 중복 방지, 하루 한도, 엑셀 결과 기록, run_log.

흐름: 잠금 → 엑셀에 못 쓴 지난 결과 반영 → 촬영 사진 분리 → (빈 칸이 있으면) AI 초안 → '대기' 상품 가공·검증 → (등록 모드) 상품마다
      한도 확인 → 스마트스토어 판매자 상품코드 중복 확인 → 이미지 다운로드 → 등록 → 기록 → 엑셀에 결과 쓰기
상품 하나가 실패해도 다음 상품으로 넘어간다. 엑셀이 열려 있으면 결과를 DB에 두고 다음 실행 때 반영한다.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from api.errors import ApiError, NetworkError
from api.images import ImageApi
from api.products import ProductApi
from services.automation.config import load_settings
from services.automation.input_sheet import (
    STATUS_FAILED, STATUS_ON_SALE, STATUS_PROCESSING, STATUS_REGISTERED, STATUS_WAITING, ResultUpdate, read_items,
    write_results,
)
from services.automation.pipeline import PreparedItem, build_context, item_image_options, prepare
from services.automation.run_lock import RunLock
from services import ai_draft, detail_page
from services.bulk_register import api_error_message
from services.images import product_images
from services.images.download import download_images
from services.images.photo_sort import sort_photos
from services.images.processing import process_product

# 대표 이미지 배경 제거 함수 (None이면 설정의 rembg 모델 사용). 테스트에서 대역으로 바꾼다.
_bg_remover = None
from services.product_service import register_product
from services.validator import validate
from storage.db import app_data_dir

log = logging.getLogger(__name__)

_DONE_STATUSES = (STATUS_REGISTERED, STATUS_ON_SALE)


@dataclass
class ItemResult:
    status: str                 # 엑셀에 쓸 상태
    naver_no: int | None = None
    message: str = ""
    registered: bool = False


@dataclass
class RunReport:
    run_id: str
    excel: Path
    dry_run: bool
    prepared: list[PreparedItem] = field(default_factory=list)
    results: dict[str, ItemResult] = field(default_factory=dict)   # 등록 모드: 상품코드별 결과
    total_rows: int = 0
    daily_limit: int = 0
    registered_today: int = 0
    excel_written: bool = True
    excel_error: str = ""
    synced_previous: int = 0     # 지난 실행에서 엑셀에 못 쓴 결과를 이번에 반영한 건수
    photo_sort: object = None    # 촬영 사진 분리 결과 (SortReport)
    photo_sort_error: str = ""
    pending_sync: int = 0        # 엑셀이 열려 있어 DB에만 있는 결과 건수
    ai_note: str = ""            # AI 초안을 건너뛴 이유 (키 없음 등)

    @property
    def ok_items(self) -> list[PreparedItem]:
        return [p for p in self.prepared if p.ok]

    @property
    def failed_items(self) -> list[PreparedItem]:
        return [p for p in self.prepared if not p.ok]

    @property
    def remaining_today(self) -> int:
        return max(0, self.daily_limit - self.registered_today)

    def count(self, status: str) -> int:
        return sum(1 for r in self.results.values() if r.status == status)

    @property
    def registered_count(self) -> int:
        return sum(1 for r in self.results.values() if r.registered)

    @property
    def ai_registered(self) -> int:
        """AI 초안으로 등록돼 검수가 필요한 상품 수."""
        return sum(1 for p in self.prepared if p.ai_draft and self.results.get(p.code, ItemResult("")).registered)


def _status_mark(report: RunReport, p: PreparedItem) -> str:
    if report.dry_run:
        return "통과" if p.ok else "실패"
    r = report.results.get(p.code)
    return r.status if r else ("통과" if p.ok else "실패")


def format_report(report: RunReport) -> str:
    """CLI·화면 공용 결과 요약 텍스트."""
    mode = "검증·미리보기 (dry-run, API 호출 없음)" if report.dry_run else "등록"
    lines = [f"[{mode}] {report.excel}",
             f"실행 ID {report.run_id} · 엑셀 {report.total_rows}행 중 '대기' {len(report.prepared)}건"]
    if report.synced_previous:
        lines.append(f"지난 실행에서 엑셀에 못 쓴 결과 {report.synced_previous}건을 반영했습니다.")
    if report.photo_sort:
        lines.append(f"촬영 사진 분리: {report.photo_sort.summary()}")
        if report.photo_sort.unsorted:
            lines.append(f"    [주의] 미분류 사진 {len(report.photo_sort.unsorted)}장 — '{report.photo_sort.unsorted[0].parent}' 확인")
        lines += [f"    [주의] {w}" for w in report.photo_sort.warnings]
    if report.photo_sort_error:
        lines.append(f"촬영 사진 분리 실패: {report.photo_sort_error}")
    if report.ai_note:
        lines.append(report.ai_note)
    lines.append("")
    for p in report.prepared:
        name = p.draft.name if p.draft else p.item.get("상품명(최종)") or p.item.get("원본상품명")
        lines.append(f"■ {p.code or '(코드 없음)'}  [{_status_mark(report, p)}]  {name}")
        if p.price:
            lines.append(f"    판매가 {p.price.price:,}원  ({p.price.explain()})")
        if p.draft:
            d = p.draft
            option = f"옵션 {len(d.option_rows)}개 조합" if d.use_options else "옵션 없음"
            if p.pending_urls:
                image = f"이미지URL {len(p.pending_urls)}개 (등록 시 다운로드)"
            else:
                image = f"사진 {1 + len(d.optional_images)}장" + (f" + 상세 {len(p.images.detail)}장" if p.images.detail else "")
            lines.append(f"    상품명({p.name_source}) · {d.category_name or d.leaf_category_id} · {option} · "
                         f"재고 {d.effective_stock():,} · {image} · {'판매중지(검수)' if d.test_mode else '바로 판매'}")
        if p.ai_draft:
            filled = "·".join(p.ai_fills) if p.ai_fills else "상품명(최종)·설명(최종)"
            lines.append(f"    [{ai_draft.AI_MARK}] {filled}을(를) AI 초안으로 채웠습니다.")
        lines += [f"    [오류] {e}" for e in p.errors]
        lines += [f"    [주의] {w}" for w in p.warnings]
        if p.preview:
            lines.append(f"    상세페이지 미리보기: {p.preview}")
        r = report.results.get(p.code)
        if r and not (r.status == STATUS_FAILED and p.errors):  # 검증 오류는 위에 이미 표시
            lines.append(f"    → {r.message}" + (f" (네이버 상품번호 {r.naver_no})" if r.naver_no else ""))
    lines.append("")
    if report.dry_run:
        ok, failed = len(report.ok_items), len(report.failed_items)
        lines.append(f"결과: 통과 {ok}건 / 실패 {failed}건")
        if ok:
            can = min(ok, report.remaining_today)
            lines.append(f"실제 실행 시: 오늘 등록 가능 {report.remaining_today}건 (하루 최대 {report.daily_limit}건, "
                         f"오늘 {report.registered_today}건 등록됨) → {can}건 등록, 나머지는 '대기' 유지")
    else:
        waiting = report.count(STATUS_WAITING)
        lines.append(f"결과: 등록 {report.registered_count}건 / 실패 {report.count(STATUS_FAILED)}건"
                     + (f" / 하루 한도로 대기 유지 {waiting}건" if waiting else ""))
        if report.excel_written:
            lines.append("작업 엑셀에 결과를 기록했습니다.")
        else:
            lines.append(f"작업 엑셀에 쓰지 못했습니다({report.excel_error}). 결과 {report.pending_sync}건은 "
                         f"DB에 보관했고 다음 실행 때 엑셀에 반영합니다. 엑셀을 닫아 주세요.")
    return "\n".join(lines)


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def lock_path() -> Path:
    return app_data_dir() / "run.lock"


class StoreCodeIndex:
    """스마트스토어에 등록된 판매자 상품코드 → 원상품번호. 처음 필요할 때 상품 목록을 한 번 불러온다."""

    def __init__(self, api: ProductApi):
        self._api = api
        self._codes: dict[str, int] | None = None

    def _load(self) -> dict[str, int]:
        codes: dict[str, int] = {}
        page = 1
        while True:
            data = self._api.search(page=page, size=500)
            for item in data.get("contents", []):
                for ch in item.get("channelProducts") or []:
                    code = (ch.get("sellerManagementCode") or "").strip()
                    if ch.get("statusType") == "DELETE":   # 삭제한 상품은 같은 코드로 다시 등록할 수 있게 건너뜀
                        continue
                    if code:
                        codes.setdefault(code, int(item["originProductNo"]))
            if data.get("last", True) or not data.get("contents"):
                return codes
            page += 1

    def find(self, code: str) -> int | None:
        if self._codes is None:
            self._codes = self._load()
        return self._codes.get(code)

    def add(self, code: str, origin_no: int) -> None:
        if self._codes is not None:
            self._codes[code] = origin_no


def _sync_previous(db, excel: Path, report: RunReport) -> None:
    """지난 실행에서 엑셀이 열려 있어 쓰지 못한 결과를 반영한다."""
    # '처리중'은 결과가 아니라 중단된 흔적 → 엑셀에 쓰지 않고 이번 실행에서 다시 처리
    pending = [r for r in db.unsynced_automation_items() if r["status"] != STATUS_PROCESSING]
    if not pending:
        return
    updates = {r["code"]: ResultUpdate(r["status"], r["origin_product_no"], r["message"] or "", fills=_saved_fills(db, r["code"]))
               for r in pending}
    try:
        write_results(excel, updates)
    except OSError as e:
        log.warning("지난 결과 엑셀 반영 실패(파일 열림?): %s", e)
        return
    for code, u in updates.items():
        db.upsert_automation_item(code, u.status, u.naver_no, u.message, excel_synced=True)
    report.synced_previous = len(updates)


def _saved_fills(db, code: str) -> dict[str, str]:
    """AI 초안으로 채웠던 값 (엑셀에 아직 못 썼을 수 있음). 엑셀 칸이 비어 있을 때만 쓰인다."""
    row = db.get_ai_draft(code)
    if not row:
        return {}
    return {k: v for k, v in (("상품명(최종)", row["filled_name"]), ("설명(최종)", row["filled_desc"])) if v}


def _photos_for_ai(settings, item):
    """AI에 보낼 보정 사진을 준비한다 (이미지URL만 있으면 먼저 내려받음). 등록 때 다시 받거나 보정하지 않는다."""
    root = settings.image_root_path()
    urls = [u.strip() for u in item.get("이미지URL").split("|") if u.strip().lower().startswith(("http://", "https://"))]
    if not product_images(root, item.code).found and urls:
        download_images(root, item.code, urls)
    process_product(root, item.code, item_image_options(settings, item), remover=_bg_remover)
    return product_images(root, item.code)


@dataclass
class DraftResult:
    code: str
    draft: "ai_draft.AiDraft | None" = None
    fills: dict[str, str] = field(default_factory=dict)
    error: str = ""
    cached: bool = False


def make_drafts(db, codes: list[str] | None = None, force: bool = False,
                on_progress: Callable[[str, int, int], None] | None = None) -> tuple[list[DraftResult], str]:
    """AI 초안만 만든다 (등록 없음). 빈 칸만 작업 엑셀에 채운다. (결과, 엑셀 쓰기 실패 사유).

    codes가 없으면 '대기' 상품 중 상품명(최종)·설명(최종)이 빈 상품. force면 보관된 초안을 버리고 새로 만든다.
    """
    if not ai_draft.has_api_key():
        raise ValueError("Claude API 키가 없습니다. 자동화 설정에서 키를 저장하세요.")
    with RunLock(lock_path(), "draft"):
        settings = load_settings(db)
        excel = settings.work_excel_path()
        items = read_items(excel)
        if codes:
            missing = [c for c in codes if c not in {i.code for i in items}]
            if missing:
                raise ValueError(f"작업 엑셀에 없는 상품코드: {', '.join(missing)}")
            targets = [i for i in items if i.code in codes]
        else:
            targets = [i for i in items if i.status == STATUS_WAITING and i.code and ai_draft.needs_draft(i)]
        results = []
        for index, item in enumerate(targets, start=1):
            r = DraftResult(item.code, cached=not force and ai_draft.load(db, item.code) is not None)
            try:
                r.draft = ai_draft.ensure(db, item, None if r.cached else _photos_for_ai(settings, item), settings,
                                          force=force)
                r.fills = ai_draft.fill_item(db, item, r.draft, settings.banned_words)
            except (ai_draft.AiDraftError, OSError, ValueError) as e:
                r.error = str(e)
            results.append(r)
            if on_progress:
                on_progress(item.code, index, len(targets))
        updates = {r.code: ResultUpdate(None, fills=r.fills) for r in results if r.fills}
        try:
            write_results(excel, updates)
        except OSError as e:
            return results, "파일이 열려 있음" if isinstance(e, PermissionError) else str(e)
        return results, ""


def format_drafts(results: list[DraftResult], banned_words: list[str], excel_error: str = "") -> str:
    """CLI·화면 공용 AI 초안 결과 텍스트."""
    lines = []
    for r in results:
        lines.append(f"■ {r.code}" + ("  (보관된 초안 — 다시 만들려면 --force)" if r.cached else ""))
        if r.error:
            lines += [f"    [오류] {r.error}", ""]
            continue
        d = r.draft
        usable = d.usable_names(banned_words)
        for i, name in enumerate(d.names, start=1):
            mark = "" if name in usable else "  ← 금지어·50자 초과로 제외"
            lines.append(f"    상품명 후보 {i}: {name} ({len(name)}자){mark}")
        lines.append("    특징:")
        lines += [f"      - {f}" for f in d.features]
        lines.append(f"    검색 태그: {', '.join(d.tags) or '-'}")
        lines.append(f"    색상: {', '.join(d.colors) or '-'}")
        if r.fills:
            lines.append(f"    → 엑셀 빈 칸 채움: {', '.join(r.fills)}  [{ai_draft.AI_MARK}]")
        else:
            lines.append("    → 엑셀에 이미 값이 있어 채우지 않았습니다 (사람이 입력한 값 유지).")
        lines.append("")
    if excel_error:
        lines.append(f"작업 엑셀에 쓰지 못했습니다({excel_error}). 엑셀을 닫으면 다음 실행 때 빈 칸에 채웁니다.")
    return "\n".join(lines).rstrip()


def _ai_step(services, settings, item, report: RunReport, dry_run: bool) -> tuple[dict[str, str], list[str]]:
    """상품명(최종)·설명(최종)이 비었으면 AI 초안으로 채운다. (채운 칸, 주의 문구). 실패해도 등록은 계속한다.

    dry-run은 API를 부르지 않고 이전에 만든 초안만 쓴다. 하루 한도에 걸린 상품도 새로 만들지 않는다(비용).
    """
    db = services.db
    if not settings.ai_enabled or not item.code or not ai_draft.needs_draft(item):
        return {}, []
    draft = ai_draft.load(db, item.code)
    if draft is None:
        if not ai_draft.has_api_key():
            report.ai_note = "Claude API 키가 없어 AI 초안을 건너뛰었습니다. (자동화 설정에서 키를 저장하면 사용)"
            return {}, []
        if dry_run:
            return {}, ["AI 초안은 실제 실행 때 만듭니다. (dry-run은 Claude API를 부르지 않음)"]
        if report.registered_today + report.registered_count >= report.daily_limit:
            return {}, []
        try:
            draft = ai_draft.ensure(db, item, _photos_for_ai(settings, item), settings)
        except (ai_draft.AiDraftError, OSError, ValueError) as e:
            return {}, [f"AI 초안 실패({e}) — 상품명은 분류 규칙으로 만듭니다."]
    fills = ai_draft.fill_item(db, item, draft, settings.banned_words)
    warnings = []
    if not item.get("상품명(최종)") and draft.names:
        warnings.append("AI 상품명 후보가 모두 금지어·50자 제한에 걸려 분류 규칙으로 만듭니다.")
    return fills, warnings


def _apply_pending_status(db, items) -> None:
    """엑셀에 아직 반영되지 않은 DB 상태를 우선한다 (이미 등록한 상품을 다시 등록하지 않도록).

    '처리중'으로 남은 상품은 지난 실행이 중간에 멈춘 것이므로 다시 처리한다.
    이때 스마트스토어 판매자 상품코드 확인으로 이미 등록됐는지 가려낸다.
    """
    for item in items:
        record = db.get_automation_item(item.code) if item.code else None
        if record and not record["excel_synced"]:
            item.values["상태"] = STATUS_WAITING if record["status"] == STATUS_PROCESSING else record["status"]


def _sort_inbox(db, settings, items, report: RunReport) -> None:
    """동기화 폴더의 촬영 사진을 상품 폴더로 분리한다. 실패해도 등록은 계속한다."""
    if not settings.photo_inbox:
        return
    try:
        report.photo_sort = sort_photos(db, Path(settings.photo_inbox), settings.image_root_path(),
                                        settings.photo_max_gap_hours, {i.code for i in items if i.code})
    except (OSError, ValueError) as e:
        report.photo_sort_error = str(e)
        log.warning("촬영 사진 분리 실패: %s", e)


def _review_fields(p: PreparedItem) -> dict:
    """검수 화면에 보여줄 정보 (등록 성공 시 기록)."""
    return {"name": p.draft.name, "sale_price": p.draft.sale_price, "ai_draft": getattr(p, "ai_draft", False),
            "registered": True}


def _summary(p: PreparedItem) -> str:
    d = p.draft
    return (f"{d.name} / {d.sale_price:,}원 / 옵션 {len(d.option_rows) if d.use_options else 0}개 / "
            f"사진 {1 + len(d.optional_images) if d.representative_image else len(p.pending_urls)}장")


def _register_one(services, p: PreparedItem, report: RunReport, store: StoreCodeIndex, settings) -> ItemResult:
    db, code, d = services.db, p.code, p.draft
    previous = db.get_automation_item(code)

    existing = store.find(code)
    if existing:
        if previous and previous["status"] == STATUS_PROCESSING:
            # 지난 실행이 등록 직후 중단된 경우: 이미 등록된 상품을 기록만 한다
            return ItemResult(STATUS_REGISTERED, existing, "지난 실행에서 등록된 상품을 확인해 기록했습니다.", True)
        return ItemResult(STATUS_FAILED, existing,
                          f"스마트스토어에 같은 판매자 상품코드({code})의 상품이 이미 있습니다.")

    root = settings.image_root_path()
    if p.pending_urls:
        dl = download_images(root, code, p.pending_urls)
        if dl.errors:
            p.warnings += dl.errors
        if not dl.saved and not dl.skipped:
            return ItemResult(STATUS_FAILED, message="이미지 다운로드 실패: " + "; ".join(dl.errors or ["받은 사진 없음"]))
        p.pending_urls = []

    # 사진 보정 (원본/ → 보정/, 이미 처리한 사진은 건너뜀) 후 보정본으로 등록
    processed = process_product(root, code, item_image_options(settings, p.item), remover=_bg_remover)
    p.images = product_images(root, code)
    if not p.images.found or not p.images.edited:
        return ItemResult(STATUS_FAILED, message="사진 보정 실패: " + "; ".join(processed.errors or ["보정본 없음"]))
    p.warnings += processed.errors
    d.representative_image = str(p.images.representative)
    d.optional_images = [str(x) for x in p.images.optional]
    errors = validate(d)
    if errors:
        return ItemResult(STATUS_FAILED, message="; ".join(errors))

    # 상세페이지: 사진을 한 번만 올리고, 올린 주소로 HTML을 만든다 (상품 이미지도 같은 주소 재사용)
    notice = detail_page.notice_image_path(settings)
    to_upload = [str(x) for x in detail_page.page_images(p.images, notice)]
    uploaded = dict(zip(to_upload, ImageApi(services.client()).upload(to_upload)))
    context = detail_page.build_context(d, p.item, p.images, p.measurements, lambda path: uploaded[str(path)], notice,
                                        detail_page.notice_text(settings))
    d.detail_html = detail_page.render(settings, p.detail_template, context)
    detail_page.save_registered(code, d.detail_html, d.name)

    db.upsert_automation_item(code, STATUS_PROCESSING, message="등록 중", excel_synced=False)
    outcome = register_product(services, d, uploaded=uploaded)
    store.add(code, outcome.origin_product_no)
    status = STATUS_REGISTERED if d.test_mode else STATUS_ON_SALE
    parts = [f"판매가 {d.sale_price:,}원",
             f"옵션 {len(d.option_rows)}개" if d.use_options else "옵션 없음",
             f"사진 {1 + len(d.optional_images)}장" + (" (대표 배경 제거)" if processed.background_removed else "")]
    if p.ai_draft:
        parts.insert(0, ai_draft.AI_MARK)
    if p.name_source in ("규칙", "AI"):
        parts.append(f"상품명 {'AI' if p.name_source == 'AI' else '자동'}: {d.name}")
    if outcome.warning:
        parts.append("확인 필요: 판매중지 전환 실패(전시중지 상태)")
    parts += p.warnings
    return ItemResult(status, outcome.origin_product_no, " · ".join(parts), True)


def run_waiting(services, excel: Path | None = None, dry_run: bool = True, codes: list[str] | None = None,
                on_progress: Callable[[str, int, int], None] | None = None) -> RunReport:
    """services: db와 client()를 가진 객체(AppServices). dry-run은 API를 부르지 않는다."""
    db = services.db
    with RunLock(lock_path(), "run"):
        settings = load_settings(db)
        excel = Path(excel) if excel else settings.work_excel_path()
        run_id = new_run_id()
        report = RunReport(run_id, excel, dry_run, daily_limit=settings.daily_limit,
                           registered_today=db.count_registered_today())
        db.add_run_log(run_id, "run_start", "시작", summary=f"{'dry-run ' if dry_run else ''}{excel}")
        try:
            if not dry_run:
                _sync_previous(db, excel, report)
            items = read_items(excel)
            if not dry_run:
                _apply_pending_status(db, items)
                _sort_inbox(db, settings, items, report)
            report.total_rows = len(items)
            targets = [i for i in items if i.status == STATUS_WAITING and (not codes or i.code in codes)]
            ctx = build_context(db, items)
            store = None if dry_run else StoreCodeIndex(ProductApi(services.client()))
            for index, item in enumerate(targets, start=1):
                fills, ai_warnings = _ai_step(services, settings, item, report, dry_run)
                p = prepare(item, ctx)
                p.ai_fills, p.ai_draft = fills, ai_draft.is_ai_origin(db, item)
                if "상품명(최종)" in fills:
                    p.name_source = "AI"
                p.warnings += ai_warnings
                report.prepared.append(p)
                if p.price:
                    log.info("[%s] 판매가 계산: %s", item.code, p.price.explain())
                if dry_run:
                    if p.ok:
                        _write_preview(p, settings)
                    db.add_run_log(run_id, "dry_run", "통과" if p.ok else "실패", code=item.code,
                                   summary=_summary(p) if p.draft else None, error="\n".join(p.errors) or None)
                else:
                    report.results[item.code] = r = _process_one(services, p, report, store, settings, run_id)
                    if item.code:
                        db.upsert_automation_item(item.code, r.status, r.naver_no, r.message, excel_synced=False,
                                                  **_review_fields(p) if r.registered else {})
                if on_progress:
                    on_progress(item.code, index, len(targets))
        except Exception as e:
            db.add_run_log(run_id, "run_end", "오류", error=str(e))
            if not dry_run:
                _write_results(db, excel, report)
            raise
        if not dry_run:
            _write_results(db, excel, report)
            summary = (f"등록 {report.registered_count} / 실패 {report.count(STATUS_FAILED)} / "
                       f"대기 유지 {report.count(STATUS_WAITING)}")
        else:
            summary = f"대상 {len(report.prepared)} / 통과 {len(report.ok_items)} / 실패 {len(report.failed_items)}"
        db.add_run_log(run_id, "run_end", "완료", summary=summary)
        return report


def _process_one(services, p: PreparedItem, report: RunReport, store: StoreCodeIndex, settings,
                 run_id: str) -> ItemResult:
    db = services.db
    if not p.ok:
        result = ItemResult(STATUS_FAILED, message="; ".join(p.errors))
        db.add_run_log(run_id, "register", "실패", code=p.code, error=result.message)
        return result
    if report.registered_today + report.registered_count >= report.daily_limit:
        return ItemResult(STATUS_WAITING, message=f"하루 최대 등록 수({report.daily_limit}건)에 도달해 다음 실행 때 처리합니다.")
    try:
        result = _register_one(services, p, report, store, settings)
    except (ApiError, NetworkError, OSError, ValueError) as e:
        result = ItemResult(STATUS_FAILED, message=api_error_message(e))
    db.add_run_log(run_id, "register", "성공" if result.registered else "실패", code=p.code,
                   summary=_summary(p), error=None if result.registered else result.message)
    log.info("[%s] %s %s", p.code, result.status, result.naver_no or result.message)
    return result


def _write_results(db, excel: Path, report: RunReport) -> None:
    prepared = {p.code: p for p in report.prepared}
    updates = {code: ResultUpdate(r.status, r.naver_no, r.message,
                                  prepared[code].item.row_no if code in prepared else None,
                                  fills=prepared[code].ai_fills if code in prepared else {})
               for code, r in report.results.items() if code}
    try:
        write_results(excel, updates)
    except OSError as e:
        report.excel_written = False
        report.excel_error = "파일이 열려 있음" if isinstance(e, PermissionError) else str(e)
        report.pending_sync = len(updates)
        log.warning("작업 엑셀 쓰기 실패 — DB에 보관: %s", e)
        return
    for code, r in report.results.items():
        if code:
            db.upsert_automation_item(code, r.status, r.naver_no, r.message, excel_synced=True)


def process_images(db, codes: list[str] | None = None, all_folders: bool = False,
                   on_progress: Callable[[str, int, int], None] | None = None) -> list:
    """사진만 보정한다(등록·API 호출 없음). codes가 없으면 작업 엑셀의 '대기' 상품, all_folders면 사진 폴더 전체."""
    with RunLock(lock_path(), "process-images"):
        settings = load_settings(db)
        root = settings.image_root_path()
        try:   # 엑셀의 상품별 '사진보정' 칸을 따른다 (엑셀에 없는 폴더는 기본 보정)
            items = {i.code: i for i in read_items(settings.work_excel_path()) if i.code}
        except (OSError, ValueError):
            items = {}
        if codes:
            targets = codes
        elif all_folders:
            targets = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
        else:
            targets = [code for code, i in items.items() if i.status == STATUS_WAITING]
        results = []
        for index, code in enumerate(targets, start=1):
            results.append(process_product(root, code, item_image_options(settings, items.get(code)),
                                           remover=_bg_remover))
            if on_progress:
                on_progress(code, index, len(targets))
        return results


def _write_preview(p: PreparedItem, settings) -> None:
    """상세페이지 미리보기 HTML (PC의 사진 파일을 직접 가리킴). 만들지 못해도 실행은 계속한다."""
    try:
        context = detail_page.build_context(p.draft, p.item, p.images, p.measurements, detail_page.local_src,
                                            detail_page.notice_image_path(settings), detail_page.notice_text(settings))
        p.preview = detail_page.write_preview(p.code, detail_page.render(settings, p.detail_template, context),
                                              p.draft.name)
    except Exception as e:  # 템플릿 문법 오류 등
        p.warnings.append(f"상세페이지 미리보기를 만들지 못했습니다: {e}")


def preview_pages(db, codes: list[str] | None = None, process: bool = True,
                  on_progress: Callable[[str, int, int], None] | None = None) -> list[PreparedItem]:
    """대기 상품(또는 지정 상품)의 상세페이지 미리보기를 만든다. process=True면 사진을 먼저 보정한다(API 호출 없음)."""
    with RunLock(lock_path(), "preview"):
        settings = load_settings(db)
        items = read_items(settings.work_excel_path())
        targets = [i for i in items if i.code in codes] if codes else             [i for i in items if i.status == STATUS_WAITING]
        ctx = build_context(db, items)
        root = settings.image_root_path()
        results = []
        for index, item in enumerate(targets, start=1):
            if process and item.code:
                processed = process_product(root, item.code, item_image_options(settings, item), remover=_bg_remover)
            p = prepare(item, ctx)
            if process and item.code:
                p.warnings += processed.errors
            if p.draft:
                _write_preview(p, settings)
            results.append(p)
            if on_progress:
                on_progress(item.code, index, len(targets))
        return results
