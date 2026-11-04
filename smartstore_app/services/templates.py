"""템플릿 — ProductDraft의 일부 필드 묶음을 이름 붙여 저장하고 불러온다."""
from __future__ import annotations

import json
from dataclasses import dataclass

from services.product_builder import ProductDraft
from storage.db import Database

# 종류: (표시명, ProductDraft 필드 목록)
TEMPLATE_KINDS: dict[str, tuple[str, list[str]]] = {
    "delivery": ("배송", ["delivery_company", "delivery_fee_type", "base_fee",
                          "free_condition_amount", "delivery_fee_pay_type"]),
    "claim": ("반품/교환", ["return_fee", "exchange_fee"]),
    "as": ("A/S", ["as_phone", "as_guide"]),
    "notice": ("고시정보·원산지", ["notice_item_name", "notice_model_name", "notice_manufacturer",
                               "origin_area_code", "origin_content", "minor_purchasable"]),
}


@dataclass
class Template:
    id: int
    kind: str
    name: str
    data: dict
    is_default: bool


def extract(kind: str, draft: ProductDraft) -> dict:
    return {f: getattr(draft, f) for f in TEMPLATE_KINDS[kind][1]}


def apply(kind: str, draft: ProductDraft, data: dict) -> ProductDraft:
    for f in TEMPLATE_KINDS[kind][1]:
        if f in data:
            setattr(draft, f, data[f])
    return draft


def list_templates(db: Database, kind: str) -> list[Template]:
    return [Template(r["id"], r["kind"], r["name"], json.loads(r["data"]), bool(r["is_default"]))
            for r in db.list_templates(kind)]


def default_template(db: Database, kind: str) -> Template | None:
    return next((t for t in list_templates(db, kind) if t.is_default), None)


def apply_defaults(db: Database, draft: ProductDraft) -> ProductDraft:
    """기본으로 지정된 템플릿들을 새 상품 입력값에 적용한다."""
    for kind in TEMPLATE_KINDS:
        t = default_template(db, kind)
        if t:
            apply(kind, draft, t.data)
    return draft
