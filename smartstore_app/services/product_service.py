"""단일 상품 등록 흐름: 이미지 업로드 → 요청 본문 생성 → 등록 → (테스트 모드) 판매중지 전환 → 로컬 기록."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from api.errors import ApiError
from api.images import ImageApi
from api.products import ProductApi
from services.connection import AppServices
from services.product_builder import ProductDraft, build_payload

log = logging.getLogger(__name__)


@dataclass
class RegistrationOutcome:
    origin_product_no: int
    channel_product_no: int | None
    status: str
    warning: str | None = None


def register_product(services: AppServices, draft: ProductDraft,
                     uploaded: dict[str, str] | None = None) -> RegistrationOutcome:
    """uploaded: 이미 올린 이미지 (로컬 경로 → 네이버 이미지 주소). 있으면 다시 올리지 않는다."""
    client = services.client()

    paths = [draft.representative_image, *draft.optional_images]
    known = uploaded or {}
    missing = [p for p in paths if p not in known]
    if missing:
        known = {**known, **dict(zip(missing, ImageApi(client).upload(missing)))}
        log.info("이미지 업로드 완료: %d개", len(missing))
    urls = [known[p] for p in paths]
    payload = build_payload(draft, urls[0], urls[1:])

    result = ProductApi(client).register(payload)
    log.info("상품 등록 완료: originProductNo=%s channelProductNo=%s",
             result.origin_product_no, result.channel_product_no)

    # 이후 단계가 실패해도 등록 사실은 남도록 바로 기록
    status, warning = "SALE", None
    save = lambda: services.db.add_product(result.origin_product_no, result.channel_product_no,
                                           draft.name.strip(), draft.sale_price, draft.effective_stock(), status)
    save()

    if draft.test_mode:
        try:
            ProductApi(client).change_status(result.origin_product_no, "SUSPENSION")
            status = "SUSPENSION"
            save()
            log.info("판매중지 전환 완료: originProductNo=%s", result.origin_product_no)
        except ApiError as e:
            log.warning("판매중지 전환 실패: %s", e)
            warning = (f"등록은 되었지만 판매중지 전환에 실패했습니다. 상품은 전시중지 상태입니다.\n"
                       f"스마트스토어센터에서 상태를 확인하세요.\n{e}")

    return RegistrationOutcome(result.origin_product_no, result.channel_product_no, status, warning)
