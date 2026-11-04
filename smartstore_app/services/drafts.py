"""임시저장 — 작성 중인 상품 입력값(ProductDraft)을 로컬 DB에 저장·복원한다."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from services.product_builder import ProductDraft
from storage.db import Database


@dataclass
class DraftSummary:
    id: int
    name: str
    category: str
    sale_price: int
    option_count: int
    updated_at: str


def save(db: Database, draft_id: int | None, draft: ProductDraft) -> int:
    name = draft.name.strip() or "(상품명 없음)"
    return db.save_draft(draft_id, name, draft.to_dict())


def load(db: Database, draft_id: int) -> ProductDraft | None:
    row = db.get_draft(draft_id)
    return ProductDraft.from_dict(json.loads(row["data"])) if row else None


def list_summaries(db: Database) -> list[DraftSummary]:
    result = []
    for r in db.list_drafts():
        data = json.loads(r["data"])
        result.append(DraftSummary(
            id=r["id"], name=r["name"], category=data.get("category_name", ""),
            sale_price=data.get("sale_price", 0),
            option_count=len(data.get("option_rows", [])) if data.get("use_options") else 0,
            updated_at=r["updated_at"],
        ))
    return result


def missing_images(draft: ProductDraft) -> list[str]:
    """임시저장 이후 옮겨지거나 삭제된 이미지 파일."""
    paths = [p for p in [draft.representative_image, *draft.optional_images] if p]
    return [p for p in paths if not Path(p).is_file()]
