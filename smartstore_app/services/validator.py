"""상품 입력값 사전 검증 — API 호출 전에 걸러낼 수 있는 오류를 한국어로 알려준다."""
from __future__ import annotations

from pathlib import Path

from api.images import SUPPORTED_MIME, guess_mime
from services.options import validate_options
from services.product_builder import ProductDraft

MAX_NAME_LEN = 100
MAX_OPTIONAL_IMAGES = 9
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PRICE = 999_999_990
MAX_STOCK = 99_999_999


def _check_image(path: str, label: str) -> list[str]:
    p = Path(path)
    if not p.is_file():
        return [f"{label} 파일을 찾을 수 없습니다: {path}"]
    errors = []
    if guess_mime(p) not in SUPPORTED_MIME:
        errors.append(f"{label}는 JPG, PNG, GIF, BMP만 가능합니다: {p.name}")
    if p.stat().st_size > MAX_IMAGE_BYTES:
        errors.append(f"{label} 용량이 10MB를 넘습니다: {p.name}")
    return errors


def validate(d: ProductDraft, check_images: bool = True) -> list[str]:
    """check_images=False: 이미지를 등록 시점에 내려받는 경우(도매몰 URL) 이미지 검사를 건너뛴다."""
    errors: list[str] = []

    if not d.leaf_category_id:
        errors.append("카테고리를 선택하세요.")
    name = d.name.strip()
    if not name:
        errors.append("상품명을 입력하세요.")
    elif len(name) > MAX_NAME_LEN:
        errors.append(f"상품명은 {MAX_NAME_LEN}자 이하로 입력하세요. (현재 {len(name)}자)")

    if not (10 <= d.sale_price <= MAX_PRICE):
        errors.append("판매가는 10원 ~ 999,999,990원 사이여야 합니다.")
    elif d.sale_price % 10:
        errors.append("판매가는 10원 단위로 입력하세요.")
    if d.use_options:
        errors += validate_options(d.option_groups, d.option_rows, d.sale_price)
    if not (0 <= d.effective_stock() <= MAX_STOCK):
        errors.append("재고수량은 0 ~ 99,999,999 사이여야 합니다.")

    if check_images:
        if not d.representative_image:
            errors.append("대표 이미지를 선택하세요.")
        else:
            errors += _check_image(d.representative_image, "대표 이미지")
        if len(d.optional_images) > MAX_OPTIONAL_IMAGES:
            errors.append(f"추가 이미지는 최대 {MAX_OPTIONAL_IMAGES}개입니다.")
        for path in d.optional_images:
            errors += _check_image(path, "추가 이미지")

    if not d.detail_html.strip():
        errors.append("상세설명을 입력하세요.")

    if d.delivery_fee_type != "FREE" and d.base_fee <= 0:
        errors.append("유료/조건부 무료 배송은 기본 배송비를 입력하세요.")
    if d.delivery_fee_type == "CONDITIONAL_FREE" and d.free_condition_amount <= 0:
        errors.append("조건부 무료 배송은 무료 조건 금액을 입력하세요.")

    if not d.as_phone.strip():
        errors.append("A/S 전화번호를 입력하세요.")
    if not d.origin_area_code.strip():
        errors.append("원산지를 선택하세요.")

    return errors
