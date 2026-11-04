"""등록 상품 조회·수정 — 목록 조회, 가격·재고·옵션·판매/전시 상태 변경."""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field

from api.errors import ApiError
from api.products import ProductApi
from services.connection import AppServices
from services.options import price_range
from services.validator import MAX_PRICE, MAX_STOCK

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "SALE": "판매중", "SUSPENSION": "판매중지", "OUTOFSTOCK": "품절", "WAIT": "판매대기",
    "UNADMISSION": "승인대기", "REJECTION": "승인거부", "PROHIBITION": "판매금지",
    "CLOSE": "판매종료", "DELETE": "삭제",
}
DISPLAY_LABELS = {"ON": "전시중", "SUSPENSION": "전시중지"}
# 판매자가 직접 바꿀 수 있는 판매 상태
EDITABLE_STATUSES = ["SALE", "SUSPENSION"]

_PAGE_SIZE = 500
_MAX_ITEMS = 5000


@dataclass
class ProductSummary:
    origin_product_no: int
    channel_product_no: int | None
    name: str
    status: str
    display_status: str
    sale_price: int
    stock: int
    category: str
    modified: str

    @classmethod
    def from_search(cls, item: dict) -> "ProductSummary":
        ch = (item.get("channelProducts") or [{}])[0]
        return cls(
            origin_product_no=int(item["originProductNo"]),
            channel_product_no=ch.get("channelProductNo"),
            name=ch.get("name", ""),
            status=ch.get("statusType", ""),
            display_status=ch.get("channelProductDisplayStatusType", ""),
            sale_price=int(ch.get("salePrice") or 0),
            stock=int(ch.get("stockQuantity") or 0),
            category=ch.get("wholeCategoryName", ""),
            modified=(ch.get("modifiedDate") or "")[:16].replace("T", " "),
        )


def fetch_all(services: AppServices) -> list[ProductSummary]:
    """스토어의 상품을 모두 불러온다(최근 등록순). 상품 수가 많지 않은 스토어 기준으로 전체를 받아 화면에서 거른다."""
    api = ProductApi(services.client())
    result: list[ProductSummary] = []
    page = 1
    while len(result) < _MAX_ITEMS:
        data = api.search(page=page, size=_PAGE_SIZE)
        result += [ProductSummary.from_search(item) for item in data.get("contents", [])]
        if data.get("last", True) or not data.get("contents"):
            break
        page += 1
    return result


def filter_products(items: list[ProductSummary], keyword: str = "", status: str = "") -> list[ProductSummary]:
    words = keyword.lower().split()
    return [p for p in items
            if (not status or p.status == status)
            and all(w in p.name.lower() or w in str(p.origin_product_no) for w in words)]


@dataclass
class EditableOption:
    id: int | None
    values: list[str]
    price: int
    stock: int
    usable: bool


@dataclass
class ProductEdit:
    """수정 화면용 상품 정보. raw는 원상품 조회 응답 그대로(수정 시 이것을 고쳐서 전송)."""
    origin_product_no: int
    name: str
    sale_price: int
    stock: int
    status: str
    display_status: str
    group_names: list[str] = field(default_factory=list)
    options: list[EditableOption] = field(default_factory=list)
    naver_shopping: bool | None = None   # None이면 바꾸지 않음
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def has_options(self) -> bool:
        return bool(self.options)

    def effective_stock(self) -> int:
        return sum(o.stock for o in self.options if o.usable) if self.options else self.stock


def load_edit(services: AppServices, origin_product_no: int) -> ProductEdit:
    raw = ProductApi(services.client()).get_origin_product(origin_product_no)
    op = raw["originProduct"]
    info = (op.get("detailAttribute") or {}).get("optionInfo") or {}
    names = info.get("optionCombinationGroupNames") or {}
    group_names = [names[k] for k in sorted(names) if names[k]]
    options = [
        EditableOption(
            id=c.get("id"),
            values=[c.get(f"optionName{i}", "") for i in range(1, len(group_names) + 1)],
            price=int(c.get("price") or 0),
            stock=int(c.get("stockQuantity") or 0),
            usable=bool(c.get("usable", True)),
        )
        for c in info.get("optionCombinations") or []
    ]
    return ProductEdit(
        origin_product_no=origin_product_no,
        name=op.get("name", ""),
        sale_price=int(op.get("salePrice") or 0),
        stock=int(op.get("stockQuantity") or 0),
        status=op.get("statusType", ""),
        display_status=(raw.get("smartstoreChannelProduct") or {}).get("channelProductDisplayStatusType", ""),
        group_names=group_names,
        options=options,
        raw=raw,
    )


NAME_MAX = 100   # 네이버 상품명 최대 길이


def validate_edit(edit: ProductEdit, banned_words: list[str] | None = None) -> list[str]:
    errors = []
    name = edit.name.strip()
    if not name:
        errors.append("상품명을 입력하세요.")
    elif len(name) > NAME_MAX:
        errors.append(f"상품명은 {NAME_MAX}자 이내여야 합니다. (지금 {len(name)}자)")
    found = [w for w in (banned_words or []) if w and w.lower() in name.lower()]
    if found:
        errors.append(f"상품명에 금지어가 있습니다: {', '.join(found)}")
    if not (10 <= edit.sale_price <= MAX_PRICE) or edit.sale_price % 10:
        errors.append("판매가는 10원 단위, 10원 ~ 999,999,990원이어야 합니다.")
    if not (0 <= edit.effective_stock() <= MAX_STOCK) or any(o.stock < 0 for o in edit.options):
        errors.append("재고수량은 0 ~ 99,999,999 사이여야 합니다.")
    if edit.options:
        usable = [o for o in edit.options if o.usable]
        if not usable:
            errors.append("사용하는 옵션이 1개 이상 있어야 합니다.")
        if not any(o.price == 0 for o in usable):
            errors.append("추가금액이 0원인 옵션이 1개 이상 있어야 합니다.")
        lo, hi = price_range(edit.sale_price)
        if any(o.price % 10 or not (lo <= o.price <= hi) for o in edit.options):
            errors.append(f"옵션 추가금액은 10원 단위, {lo:,}원 ~ {hi:,}원 범위여야 합니다. (판매가 기준)")
    original_status = edit.raw.get("originProduct", {}).get("statusType")
    if edit.status == "SALE" and original_status != "SALE" and edit.effective_stock() == 0:
        errors.append("재고가 0이면 판매중으로 바꿀 수 없습니다.")
    return errors


def build_update_body(edit: ProductEdit) -> dict:
    body = copy.deepcopy(edit.raw)
    op = body["originProduct"]
    if op.get("statusType") == "OUTOFSTOCK":
        # 품절은 네이버가 재고 0일 때 붙이는 상태라 수정 요청에는 보낼 수 없다(NotValidEnum).
        # 고른 상태(판매중/판매중지)를 보내고, 그대로 품절이면 판매중으로 보낸다 — 재고가 0이면 네이버가 다시 품절로 둔다.
        op["statusType"] = edit.status if edit.status in EDITABLE_STATUSES else "SALE"
    op["name"] = edit.name.strip()
    op["salePrice"] = edit.sale_price
    op["stockQuantity"] = edit.effective_stock()
    if edit.options:
        by_id = {o.id: o for o in edit.options}
        for c in op["detailAttribute"]["optionInfo"]["optionCombinations"]:
            o = by_id.get(c.get("id"))
            if o:
                c.update(price=o.price, stockQuantity=o.stock, usable=o.usable)
    channel = body.setdefault("smartstoreChannelProduct", {})
    channel["channelProductDisplayStatusType"] = edit.display_status
    if edit.naver_shopping is not None:
        channel["naverShoppingRegistration"] = edit.naver_shopping
    return body


def _content_changed(edit: ProductEdit) -> bool:
    body = build_update_body(edit)
    base = copy.deepcopy(edit.raw)
    base["originProduct"]["statusType"] = body["originProduct"].get("statusType")   # 품절 → 상태 바꿔 보낸 것만으로는 변경 아님
    return body != base


def save_edit(services: AppServices, edit: ProductEdit) -> list[str]:
    """변경 사항을 저장하고 수행한 작업 목록을 돌려준다. 재고를 먼저 반영한 뒤 판매 상태를 바꾼다."""
    api = ProductApi(services.client())
    original_status = edit.raw["originProduct"].get("statusType")
    done = []
    sent_status = original_status
    if _content_changed(edit):
        body = build_update_body(edit)
        api.update_origin_product(edit.origin_product_no, body)
        sent_status = body["originProduct"].get("statusType")
        done.append("상품명·가격·재고·옵션·전시 상태 저장")
        log.info("상품 수정: originProductNo=%s price=%s stock=%s display=%s", edit.origin_product_no,
                 edit.sale_price, edit.effective_stock(), edit.display_status)
    if edit.status != original_status and edit.status != sent_status:   # 품절 상품은 수정 요청에 이미 상태를 실어 보냄
        api.change_status(edit.origin_product_no, edit.status)
        done.append(f"판매 상태 → {STATUS_LABELS.get(edit.status, edit.status)}")
        log.info("판매 상태 변경: originProductNo=%s %s -> %s", edit.origin_product_no, original_status, edit.status)
    elif edit.status != original_status:
        done.append(f"판매 상태 → {STATUS_LABELS.get(edit.status, edit.status)}")
    if done:
        services.db.update_product(edit.origin_product_no, edit.sale_price, edit.effective_stock(), edit.status)
    return done


@dataclass
class BulkResult:
    origin_product_no: int
    ok: bool
    message: str = ""


def bulk_change_status(services: AppServices, origin_product_nos: list[int], status: str) -> list[BulkResult]:
    api = ProductApi(services.client())
    results = []
    for no in origin_product_nos:
        try:
            api.change_status(no, status)
            services.db.update_product(no, status=status)
            results.append(BulkResult(no, True))
            log.info("일괄 판매 상태 변경: originProductNo=%s -> %s", no, status)
        except ApiError as e:
            log.warning("일괄 판매 상태 변경 실패: originProductNo=%s %s", no, e)
            results.append(BulkResult(no, False, e.message))
    return results


def bulk_sold_out(services: AppServices, origin_product_nos: list[int]) -> list[BulkResult]:
    """품절 처리 — 재고를 0으로 만든다(옵션 상품은 모든 옵션 재고 0). 네이버가 상태를 '품절'로 바꾼다.

    API로 상태를 '품절'로 직접 바꿀 수는 없어서 재고로 처리한다. 다시 팔 때는 재고를 넣는다.
    """
    results = []
    for no in origin_product_nos:
        try:
            edit = load_edit(services, no)
            edit.stock = 0
            for option in edit.options:
                option.stock = 0
            if _content_changed(edit):
                ProductApi(services.client()).update_origin_product(no, build_update_body(edit))
            services.db.update_product(no, edit.sale_price, 0, "OUTOFSTOCK")
            results.append(BulkResult(no, True))
            log.info("품절 처리(재고 0): originProductNo=%s", no)
        except ApiError as e:
            log.warning("품절 처리 실패: originProductNo=%s %s", no, e)
            results.append(BulkResult(no, False, e.message))
    return results
