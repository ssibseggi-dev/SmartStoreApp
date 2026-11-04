"""작업 엑셀 1행 → 등록용 ProductDraft 가공·검증.

분류 매핑 적용 → 판매가 계산 → 옵션·재고 해석 → 상품명 결정 → 템플릿·사진 적용 → 검증
"""
from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from services import templates
from services.automation.config import AutomationSettings, CategoryMapping, load_mappings, load_settings
from services.automation.input_sheet import (
    METHODS, PHOTO_AS_IS, PHOTO_MODES, STATUS_ON_SALE, STATUS_REGISTERED, InputItem,
)
from services.automation.naming import find_banned, render_name
from services.automation.parsing import MeasurementsParsed, parse_measurements, parse_options, parse_stock
from dataclasses import replace

from services.automation.pricing import PriceResult, calculate_price
from services.detail_page import DEFAULT_TEMPLATE, IMAGE_ONLY_TEMPLATE, template_exists, user_template_dir
from services.images import ImageSet, product_images
from services.product_builder import ProductDraft
from services.validator import validate
from storage.db import Database

REQUIRED = ["상품코드", "입력방식", "원본상품명", "원가", "재고", "분류"]


@dataclass
class Context:
    db: Database
    settings: AutomationSettings
    mappings: dict[str, CategoryMapping]
    templates: dict[str, dict[str, dict]]   # 종류 → 이름 → 템플릿 데이터
    code_counts: Counter
    has_categories: bool

    @property
    def image_root(self) -> Path:
        return self.settings.image_root_path()


def build_context(db: Database, items: list[InputItem]) -> Context:
    return Context(
        db=db,
        settings=load_settings(db),
        mappings=load_mappings(db),
        templates={kind: {t.name: t.data for t in templates.list_templates(db, kind)}
                   for kind in templates.TEMPLATE_KINDS},
        code_counts=Counter(i.code for i in items if i.code),
        has_categories=db.category_count() > 0,
    )


@dataclass
class PreparedItem:
    item: InputItem
    draft: ProductDraft | None = None
    price: PriceResult | None = None
    name_source: str = ""                 # "입력" | "규칙" | "AI"
    images: ImageSet | None = None
    pending_urls: list[str] = field(default_factory=list)
    measurements: MeasurementsParsed | None = None
    detail_template: str = ""            # 분류에 지정한 상세페이지 템플릿 이름
    preview: Path | None = None           # dry-run 상세페이지 미리보기 파일
    ai_draft: bool = False                # 상품명(최종)·설명(최종)이 AI가 채운 값 그대로 (검수 필요)
    ai_fills: dict[str, str] = field(default_factory=dict)   # 이번 실행에서 AI 초안으로 채운 빈 칸
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def code(self) -> str:
        return self.item.code

    @property
    def ok(self) -> bool:
        return not self.errors


def _parse_won(text: str) -> int | None:
    s = text.replace(",", "").replace("원", "").strip()
    return int(float(s)) if re.fullmatch(r"\d+(\.0+)?", s) else None


def item_image_options(settings: AutomationSettings, item: InputItem | None):
    """상품별 사진 보정 옵션. 사진보정 '그대로'면 밝기·화이트밸런스 보정과 배경 제거를 하지 않는다(크기·용량만 맞춤)."""
    options = settings.image_options()
    if item is not None and item.get("사진보정") == PHOTO_AS_IS:
        options = replace(options, enhance=False, remove_background=False)
    return options


def basic_detail_html(item: InputItem, name: str) -> str:
    """설명(최종)이 비었을 때의 임시 상세설명. 4단계 상세페이지 템플릿이 대체한다."""
    parts = [f"<p>{html.escape(item.get('설명(최종)') or name)}</p>"]
    for label in ("소재", "세탁"):
        if item.get(label):
            parts.append(f"<p><b>{label}</b> {html.escape(item.get(label))}</p>")
    return "\n".join(parts)


def prepare(item: InputItem, ctx: Context) -> PreparedItem:
    p = PreparedItem(item)
    errors, warnings = p.errors, p.warnings

    # 판매가를 직접 정했으면 원가는 없어도 된다
    missing = [c for c in REQUIRED if not item.get(c) and not (c == "원가" and item.get("판매가"))]
    if missing:
        errors.append(f"필수값이 비어 있습니다: {', '.join(missing)}")
    if item.get("입력방식") and item.get("입력방식") not in METHODS:
        errors.append(f"입력방식은 {' / '.join(METHODS)} 중 하나여야 합니다: {item.get('입력방식')}")
    if item.get("사진보정") and item.get("사진보정") not in PHOTO_MODES:
        errors.append(f"사진보정은 {' / '.join(PHOTO_MODES)} 중 하나여야 합니다: {item.get('사진보정')}")

    # 상품코드 중복 (엑셀 안, 이전 등록 기록)
    if item.code:
        if ctx.code_counts[item.code] > 1:
            errors.append(f"엑셀에 상품코드 {item.code}가 {ctx.code_counts[item.code]}번 있습니다.")
        record = ctx.db.get_automation_item(item.code)
        if record and record["status"] in (STATUS_REGISTERED, STATUS_ON_SALE) and record["origin_product_no"]:
            errors.append(f"이미 등록된 상품코드입니다 (네이버 상품번호 {record['origin_product_no']}).")

    # 분류 매핑
    mapping = ctx.mappings.get(item.get("분류")) if item.get("분류") else None
    if item.get("분류") and mapping is None:
        errors.append(f"분류 '{item.get('분류')}'가 자동화 설정의 분류 매핑에 없습니다.")
    category_name = ""
    if mapping:
        if not mapping.category_id:
            errors.append(f"분류 '{mapping.name}'의 네이버 카테고리 ID가 설정되지 않았습니다.")
        elif ctx.has_categories:
            category = ctx.db.get_category(mapping.category_id)
            if category is None:
                errors.append(f"분류 '{mapping.name}'의 카테고리 ID {mapping.category_id}가 카테고리 목록에 없습니다.")
            elif not category.last:
                errors.append(f"분류 '{mapping.name}'의 카테고리 ID {mapping.category_id}는 최하위 카테고리가 아닙니다.")
            else:
                category_name = category.whole_name

    # 판매가: 직접 정한 값이 있으면 그대로, 없으면 원가 × 마진 계산
    cost = _parse_won(item.get("원가")) if item.get("원가") else None
    if item.get("원가") and cost is None:
        errors.append(f"원가는 숫자로 입력하세요: {item.get('원가')}")
    fixed = _parse_won(item.get("판매가")) if item.get("판매가") else None
    if item.get("판매가") and not fixed:
        errors.append(f"판매가는 0보다 큰 숫자로 입력하세요: {item.get('판매가')}")
    elif fixed:
        if fixed % 10:
            errors.append(f"판매가는 10원 단위여야 합니다: {fixed:,}원")
        p.price = PriceResult(fixed, [f"엑셀 판매가 {fixed:,}원 (직접 지정)"])
    elif cost is not None and mapping:
        try:
            p.price = calculate_price(cost, mapping.margin_rate, ctx.settings.fee_rate,
                                      ctx.settings.round_unit, ctx.settings.price_ending)
        except ValueError as e:
            errors.append(f"판매가 계산 실패: {e}")

    # 옵션·재고·실측
    options = parse_options(item.get("옵션"))
    errors += options.errors
    stock = parse_stock(item.get("재고"), options.groups) if item.get("재고") and not options.errors else None
    if stock:
        errors += stock.errors
        warnings += stock.warnings
    p.measurements = parse_measurements(item.get("실측"))
    errors += p.measurements.errors

    # 상품명: 사람이 적은 값이 있으면 그대로, 없으면 분류의 규칙으로 생성
    name = item.get("상품명(최종)")
    p.name_source = "입력" if name else "규칙"
    if not name and mapping:
        try:
            name = render_name(mapping.name_rule, {
                "브랜드": item.get("브랜드") or mapping.brand,
                "핵심키워드": item.get("키워드") or mapping.keyword,
                "원본상품명": item.get("원본상품명"),
                "분류": mapping.name,
            })
        except ValueError as e:
            errors.append(str(e))
    banned = find_banned(name or "", ctx.settings.banned_words)
    if banned:
        errors.append(f"상품명에 금지어가 있습니다: {', '.join(banned)}")

    # 사진: 사진 폴더 우선, 없으면 이미지URL을 등록 시 내려받음
    urls = [u.strip() for u in item.get("이미지URL").split("|") if u.strip()]
    bad_urls = [u for u in urls if not u.lower().startswith(("http://", "https://"))]
    if bad_urls:
        errors.append(f"이미지URL은 http:// 또는 https://로 시작해야 합니다: {bad_urls[0]}")
    if item.code:
        p.images = product_images(ctx.image_root, item.code)
        if not p.images.found:
            if urls:
                p.pending_urls = urls
            else:
                errors.append(f"사진이 없습니다. '{p.images.folder}'에 사진을 넣거나 이미지URL을 입력하세요.")
        elif p.images.skipped:
            warnings.append(f"사진이 10장을 넘어 {len(p.images.skipped)}장은 제외됩니다.")
    if mapping:
        p.detail_template = item.get("상세템플릿") or mapping.detail_template or DEFAULT_TEMPLATE
        where = "엑셀 상세템플릿 칸" if item.get("상세템플릿") else f"분류 '{mapping.name}'"
        if not template_exists(ctx.settings, p.detail_template):
            errors.append(f"{where}의 상세 템플릿 '{p.detail_template}'이(가) 없습니다. "
                          f"('{user_template_dir(ctx.settings)}' 폴더에 {p.detail_template}.html을 넣으세요)")
        elif p.detail_template == IMAGE_ONLY_TEMPLATE and p.images and not p.images.detail:
            errors.append(f"상세템플릿 '{IMAGE_ONLY_TEMPLATE}'은 상세 이미지가 필요합니다. "
                          f"파일명에 detail이 들어간 사진을 '{p.images.folder}'에 넣으세요.")
    if ctx.settings.notice_image and not Path(ctx.settings.notice_image).is_file():
        warnings.append(f"상단 공지 이미지 파일이 없어 빼고 만듭니다: {ctx.settings.notice_image}")
    if not item.get("설명(최종)"):
        warnings.append("설명(최종)이 비어 있어 상세페이지에 설명 문구가 들어가지 않습니다.")

    if errors:
        return p

    # ProductDraft 조립 (템플릿: 기본(★) → 분류에 지정한 템플릿)
    d = templates.apply_defaults(ctx.db, ProductDraft())
    for kind, template_name in mapping.template_names().items():
        if template_name:
            data = ctx.templates.get(kind, {}).get(template_name)
            if data is None:
                errors.append(f"분류 '{mapping.name}'의 {templates.TEMPLATE_KINDS[kind][0]} 템플릿 "
                              f"'{template_name}'을(를) 찾을 수 없습니다.")
            else:
                templates.apply(kind, d, data)
    d.seller_code = item.code
    d.leaf_category_id = mapping.category_id
    d.category_name = category_name
    d.name = name
    d.sale_price = p.price.price
    if options.groups:
        d.use_options, d.option_groups, d.option_rows = True, options.groups, stock.rows
    else:
        d.stock_quantity = stock.quantity
    if p.images and p.images.found:
        d.representative_image = str(p.images.representative)
        d.optional_images = [str(x) for x in p.images.optional]
    d.detail_html = basic_detail_html(item, name)
    d.test_mode = not ctx.settings.live_publish
    p.draft = d

    # 원본 사진(HEIC·대용량 포함)은 등록 직전에 보정하므로, 보정본이 있을 때만 이미지 형식·용량을 검사한다
    errors += validate(d, check_images=bool(p.images and p.images.edited) and not p.pending_urls)
    return p
