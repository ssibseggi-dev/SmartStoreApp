"""상품 API.

POST /external/v2/products                                          상품 등록
POST /external/v1/products/search                                   상품 목록 조회 (page, size ≤ 500)
GET  /external/v2/products/origin-products/{originProductNo}        원상품 조회
PUT  /external/v2/products/origin-products/{originProductNo}        원상품 수정 (조회 결과 전체를 수정해 전송)
PUT  /external/v1/products/origin-products/{originProductNo}/change-status  판매 상태 변경
DELETE /external/v2/products/origin-products/{originProductNo}      원상품 삭제 (되돌릴 수 없음)
"""
from __future__ import annotations

from dataclasses import dataclass

from .client import CommerceClient


@dataclass(frozen=True)
class RegisterResult:
    origin_product_no: int
    channel_product_no: int | None


class ProductApi:
    def __init__(self, client: CommerceClient):
        self._client = client

    def register(self, payload: dict) -> RegisterResult:
        data = self._client.post("/external/v2/products", json=payload) or {}
        return RegisterResult(
            origin_product_no=int(data["originProductNo"]),
            channel_product_no=data.get("smartstoreChannelProductNo"),
        )

    def search(self, page: int = 1, size: int = 100) -> dict:
        """응답: {contents: [{originProductNo, channelProducts: [...]}], page, size, totalElements, totalPages, last}"""
        return self._client.post("/external/v1/products/search", json={"page": page, "size": size}) or {}

    def update_origin_product(self, origin_product_no: int, body: dict) -> None:
        self._client.put(f"/external/v2/products/origin-products/{origin_product_no}", json=body)

    def get_origin_product(self, origin_product_no: int) -> dict:
        return self._client.get(f"/external/v2/products/origin-products/{origin_product_no}")

    def change_status(self, origin_product_no: int, status_type: str) -> None:
        self._client.put(f"/external/v1/products/origin-products/{origin_product_no}/change-status",
                         json={"statusType": status_type})

    def delete_origin_product(self, origin_product_no: int) -> None:
        self._client.delete(f"/external/v2/products/origin-products/{origin_product_no}")
