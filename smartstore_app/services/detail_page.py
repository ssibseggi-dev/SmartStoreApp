"""상세페이지 조립 — 분류별 Jinja2 템플릿 + 상품 정보 → 상세설명 HTML.

템플릿 찾는 순서: {작업 엑셀 폴더}/상세템플릿/{이름}.html → 앱 내장 resources/templates/{이름}.html
구성: 상단 공지 이미지 → 설명 → 옵션 → 사진 → 사이즈표(실측) → 소재·세탁 → 배송·교환 안내

등록용 HTML은 네이버 이미지 업로드 API로 올린 주소를 쓰고, 미리보기 HTML은 PC의 사진 파일을 직접 가리킨다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from services.automation.config import AutomationSettings
from services.automation.input_sheet import InputItem
from services.automation.parsing import MeasurementsParsed
from services.images import ImageSet
from services.paths import resource_path
from services.product_builder import DELIVERY_COMPANIES, ProductDraft
from storage.db import app_data_dir

USER_TEMPLATE_DIR = "상세템플릿"
DEFAULT_TEMPLATE = "기본"
IMAGE_ONLY_TEMPLATE = "이미지만"   # 완성된 상세 이미지(파일명에 detail)만 차례로 넣는 내장 템플릿
_COMPANY_NAMES = {code: name for name, code in DELIVERY_COMPANIES}


def user_template_dir(settings: AutomationSettings) -> Path:
    return settings.work_excel_path().parent / USER_TEMPLATE_DIR


def builtin_template_dir() -> Path:
    return resource_path("templates")


def _environment(settings: AutomationSettings) -> Environment:
    loader = ChoiceLoader([FileSystemLoader(str(user_template_dir(settings)), encoding="utf-8"),
                           FileSystemLoader(str(builtin_template_dir()), encoding="utf-8")])
    return Environment(loader=loader, autoescape=True, undefined=StrictUndefined,
                       trim_blocks=True, lstrip_blocks=True)


def template_exists(settings: AutomationSettings, name: str) -> bool:
    try:
        _environment(settings).get_template(f"{name or DEFAULT_TEMPLATE}.html")
        return True
    except TemplateNotFound:
        return False


def won(value: int) -> str:
    return f"{value:,}원"


def delivery_rows(d: ProductDraft) -> list[tuple[str, str]]:
    if d.delivery_fee_type == "FREE":
        fee = "무료배송"
    elif d.delivery_fee_type == "CONDITIONAL_FREE":
        fee = f"{won(d.base_fee)} ({won(d.free_condition_amount)} 이상 구매 시 무료)"
    else:
        fee = won(d.base_fee)
    if d.delivery_fee_type != "FREE" and d.delivery_fee_pay_type == "COLLECT":
        fee += " · 착불"
    return [("택배사", _COMPANY_NAMES.get(d.delivery_company, d.delivery_company)), ("배송비", fee)]


def exchange_rows(d: ProductDraft) -> list[tuple[str, str]]:
    rows = [("반품 배송비", f"편도 {won(d.return_fee)}"), ("교환 배송비", f"왕복 {won(d.exchange_fee)}")]
    if d.as_phone:
        rows.append(("A/S 문의", d.as_phone + (f" ({d.as_guide})" if d.as_guide else "")))
    return rows


def size_table(measurements: MeasurementsParsed | None) -> dict:
    if not measurements or not measurements.sizes:
        return {"columns": [], "rows": []}
    columns = measurements.columns()
    rows = []
    for size, items in measurements.sizes.items():
        values = dict(items)
        rows.append((size, [values.get(col, "-") for col in columns]))
    return {"columns": columns, "rows": rows}


def split_headline(name: str) -> tuple[str, str]:
    """상품명을 앞(강조색)·뒤 두 부분으로 나눈다. 단어 수 기준 앞쪽이 절반(홀수면 하나 더)."""
    words = name.split()
    if len(words) < 2:
        return name, ""
    cut = (len(words) + 1) // 2
    return " ".join(words[:cut]), " ".join(words[cut:])


def build_context(draft: ProductDraft, item: InputItem, images: ImageSet | None,
                  measurements: MeasurementsParsed | None, image_src: Callable[[Path], str],
                  notice_image: Path | None = None, notice_text: tuple[str, str] = ("", "")) -> dict:
    photos, detail_photos = [], []
    if images and images.found:
        photos = [image_src(p) for p in [images.representative, *images.optional]]
        detail_photos = [image_src(p) for p in images.detail]
    lines = [line.strip() for line in item.get("설명(최종)").splitlines() if line.strip()]
    head, tail = split_headline(draft.name)
    return {
        "name": draft.name,
        "name_head": head,
        "name_tail": tail,
        "description_lead": lines[0] if lines else "",
        "description_rest": lines[1:],
        "category": draft.category_name.split(">")[-1] if draft.category_name else "",
        "notice_image": image_src(notice_image) if notice_image else "",
        "notice_title": notice_text[0].strip(),
        "notice_subtitle": notice_text[1].strip(),
        "description_lines": lines,
        "options": [(g.name, g.values) for g in draft.option_groups] if draft.use_options else [],
        "photos": photos,
        "detail_photos": detail_photos,
        "size_table": size_table(measurements),
        "material": item.get("소재"),
        "washing": item.get("세탁"),
        "delivery": delivery_rows(draft),
        "exchange": exchange_rows(draft),
    }


def render(settings: AutomationSettings, template_name: str, context: dict) -> str:
    template = _environment(settings).get_template(f"{template_name or DEFAULT_TEMPLATE}.html")
    return template.render(p=context).strip()


def notice_image_path(settings: AutomationSettings) -> Path | None:
    path = Path(settings.notice_image) if settings.notice_image else None
    return path if path and path.is_file() else None


def notice_text(settings: AutomationSettings) -> tuple[str, str]:
    return settings.notice_title, settings.notice_subtitle


def page_images(images: ImageSet | None, notice: Path | None) -> list[Path]:
    """상세페이지에 들어가는 사진 (업로드 대상)."""
    result = [notice] if notice else []
    if images and images.found:
        result += [images.representative, *images.optional, *images.detail]
    return result


def preview_dir() -> Path:
    path = app_data_dir() / "상세페이지_미리보기"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_preview(code: str, html: str, title: str) -> Path:
    """브라우저로 열어 볼 수 있는 완전한 HTML 파일로 저장한다."""
    path = preview_dir() / f"{code}.html"
    path.write_text("<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                    f"<title>{title} - 상세페이지 미리보기</title></head>"
                    "<body style='margin:0;background:#eee;'><div style='background:#fff;max-width:900px;"
                    f"margin:0 auto;padding:20px 0;'>{html}</div></body></html>", encoding="utf-8")
    return path


def local_src(path: Path) -> str:
    return path.resolve().as_uri()


def save_registered(code: str, html: str, title: str) -> Path:
    """실제 등록에 쓴 상세페이지(네이버 이미지 주소)를 기록용으로 저장한다."""
    folder = app_data_dir() / "상세페이지_등록본"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{code}.html"
    path.write_text(f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>{title}</title></head>"
                    f"<body>{html}</body></html>", encoding="utf-8")
    return path
