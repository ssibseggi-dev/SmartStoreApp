"""카테고리 조회 API.

GET /external/v1/categories?last=true            전체(또는 리프만) 카테고리 목록
GET /external/v1/categories/{categoryId}          단일 카테고리
GET /external/v1/categories/{categoryId}/sub-categories  하위 카테고리
응답 항목: id, name, wholeCategoryName("대>중>소>세"), last(리프 여부)
"""
from __future__ import annotations

from dataclasses import dataclass

from .client import CommerceClient


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    whole_name: str
    last: bool

    @classmethod
    def from_api(cls, d: dict) -> "Category":
        return cls(
            id=str(d["id"]),
            name=d.get("name", ""),
            whole_name=d.get("wholeCategoryName") or d.get("name", ""),
            last=bool(d.get("last", False)),
        )


class CategoryApi:
    def __init__(self, client: CommerceClient):
        self._client = client

    def list_all(self, last_only: bool = False) -> list[Category]:
        params = {"last": "true"} if last_only else None
        data = self._client.get("/external/v1/categories", params=params) or []
        return [Category.from_api(d) for d in data]

    def get(self, category_id: str) -> Category:
        return Category.from_api(self._client.get(f"/external/v1/categories/{category_id}"))

    def sub_categories(self, category_id: str) -> list[Category]:
        data = self._client.get(f"/external/v1/categories/{category_id}/sub-categories") or []
        return [Category.from_api(d) for d in data]
