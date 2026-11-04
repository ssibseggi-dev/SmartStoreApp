"""UI 입력값(ProductDraft) → 커머스API 상품 등록 요청 본문 변환."""
from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field, fields

from services.options import OptionGroup, OptionRow, build_option_info, total_stock

# 택배사 코드 (표시명, 코드)
DELIVERY_COMPANIES = [
    ("CJ대한통운", "CJGLS"),
    ("한진택배", "HANJIN"),
    ("롯데택배", "HYUNDAI"),
    ("우체국택배", "EPOST"),
    ("로젠택배", "KGB"),
]

DELIVERY_FEE_TYPES = [
    ("무료", "FREE"),
    ("유료", "PAID"),
    ("조건부 무료", "CONDITIONAL_FREE"),
]

DELIVERY_FEE_PAY_TYPES = [
    ("선결제", "PREPAID"),
    ("착불", "COLLECT"),
    ("착불 또는 선결제", "COLLECT_OR_PREPAID"),
]

# 원산지 코드 (표시명, 코드). 세부 국가 코드는 공식 문서의 원산지 코드 표를 참고.
ORIGIN_AREAS = [
    ("기타 - 상세설명에 표시", "03"),
    ("국산", "00"),
]

NOTICE_DEFAULT = "상품상세 참조"


@dataclass
class ProductDraft:
    leaf_category_id: str = ""
    category_name: str = ""
    name: str = ""
    sale_price: int = 0
    stock_quantity: int = 100
    representative_image: str = ""                       # 로컬 파일 경로
    optional_images: list[str] = field(default_factory=list)
    detail_html: str = ""

    delivery_company: str = "CJGLS"
    delivery_fee_type: str = "FREE"
    base_fee: int = 3000
    free_condition_amount: int = 0
    delivery_fee_pay_type: str = "PREPAID"
    return_fee: int = 3000
    exchange_fee: int = 6000

    as_phone: str = ""
    as_guide: str = ""
    origin_area_code: str = "03"
    origin_content: str = "상세설명에 표시"
    minor_purchasable: bool = True

    notice_item_name: str = NOTICE_DEFAULT
    notice_model_name: str = NOTICE_DEFAULT
    notice_manufacturer: str = NOTICE_DEFAULT

    use_options: bool = False
    option_groups: list[OptionGroup] = field(default_factory=list)
    option_rows: list[OptionRow] = field(default_factory=list)

    seller_code: str = ""          # 판매자 상품코드 (자동화의 상품코드, 중복 등록 확인용)

    naver_shopping: bool = False
    # 테스트 모드: 전시중지로 등록하고, 등록 직후 판매중지로 전환
    test_mode: bool = True

    def effective_stock(self) -> int:
        """옵션 사용 시 상품 재고는 사용 중인 옵션 재고의 합계."""
        return total_stock(self.option_rows) if self.use_options else self.stock_quantity

    @classmethod
    def from_dict(cls, data: dict) -> "ProductDraft":
        known = {f.name for f in fields(cls)}
        d = cls(**{k: v for k, v in data.items() if k in known})
        d.option_groups = [OptionGroup(**g) if isinstance(g, dict) else g for g in d.option_groups]
        d.option_rows = [OptionRow(**r) if isinstance(r, dict) else r for r in d.option_rows]
        return d

    def to_dict(self) -> dict:
        return asdict(self)


def to_detail_html(text: str) -> str:
    """HTML 태그가 없는 일반 텍스트면 줄바꿈을 살려 HTML로 변환한다."""
    if re.search(r"<[a-zA-Z/][^>]*>", text):
        return text
    return "<br>".join(html.escape(line) for line in text.strip().splitlines())


def build_payload(d: ProductDraft, representative_url: str, optional_urls: list[str]) -> dict:
    delivery_fee: dict = {"deliveryFeeType": d.delivery_fee_type}
    if d.delivery_fee_type != "FREE":
        delivery_fee["baseFee"] = d.base_fee
        delivery_fee["deliveryFeePayType"] = d.delivery_fee_pay_type
    if d.delivery_fee_type == "CONDITIONAL_FREE":
        delivery_fee["freeConditionalAmount"] = d.free_condition_amount

    origin_area: dict = {"originAreaCode": d.origin_area_code}
    if d.origin_content:
        origin_area["content"] = d.origin_content

    payload = {
        "originProduct": {
            "statusType": "SALE",  # 등록 시에는 판매중만 허용 → 테스트 모드면 등록 직후 판매중지로 전환
            "saleType": "NEW",
            "leafCategoryId": d.leaf_category_id,
            "name": d.name.strip(),
            "detailContent": to_detail_html(d.detail_html),
            "images": {
                "representativeImage": {"url": representative_url},
                "optionalImages": [{"url": u} for u in optional_urls],
            },
            "salePrice": d.sale_price,
            "stockQuantity": d.effective_stock(),
            "deliveryInfo": {
                "deliveryType": "DELIVERY",
                "deliveryAttributeType": "NORMAL",
                "deliveryCompany": d.delivery_company,
                "deliveryFee": delivery_fee,
                "claimDeliveryInfo": {
                    "returnDeliveryFee": d.return_fee,
                    "exchangeDeliveryFee": d.exchange_fee,
                },
            },
            "detailAttribute": {
                "afterServiceInfo": {
                    "afterServiceTelephoneNumber": d.as_phone.strip(),
                    "afterServiceGuideContent": d.as_guide.strip() or NOTICE_DEFAULT,
                },
                "originAreaInfo": origin_area,
                "minorPurchasable": d.minor_purchasable,
                "productInfoProvidedNotice": {
                    "productInfoProvidedNoticeType": "ETC",
                    "etc": {
                        "returnCostReason": NOTICE_DEFAULT,
                        "noRefundReason": NOTICE_DEFAULT,
                        "qualityAssuranceStandard": NOTICE_DEFAULT,
                        "compensationProcedure": NOTICE_DEFAULT,
                        "troubleShootingContents": NOTICE_DEFAULT,
                        "itemName": d.notice_item_name or NOTICE_DEFAULT,
                        "modelName": d.notice_model_name or NOTICE_DEFAULT,
                        "manufacturer": d.notice_manufacturer or NOTICE_DEFAULT,
                        "customerServicePhoneNumber": d.as_phone.strip(),
                    },
                },
            },
        },
        "smartstoreChannelProduct": {
            "naverShoppingRegistration": d.naver_shopping and not d.test_mode,
            "channelProductDisplayStatusType": "SUSPENSION" if d.test_mode else "ON",
        },
    }
    if d.use_options:
        payload["originProduct"]["detailAttribute"]["optionInfo"] = build_option_info(d.option_groups, d.option_rows)
    if d.seller_code.strip():
        payload["originProduct"]["detailAttribute"]["sellerCodeInfo"] = {"sellerManagementCode": d.seller_code.strip()}
    return payload
