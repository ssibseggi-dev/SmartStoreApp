import copy

import pytest

from api.errors import ApiError
from services.product_manager import (
    ProductSummary, build_update_body, bulk_change_status, fetch_all, filter_products, load_edit, save_edit,
    validate_edit,
)
from storage.db import Database

# 실제 API 응답(2026-11-01 조회)을 줄인 형태
DETAIL = {
    "originProduct": {
        "statusType": "SUSPENSION", "name": "테스트 상품", "salePrice": 19000, "stockQuantity": 15,
        "detailAttribute": {"optionInfo": {
            "optionCombinationGroupNames": {"optionGroupName1": "색상", "optionGroupName2": "사이즈"},
            "optionCombinations": [
                {"id": 1, "stockQuantity": 10, "price": 0, "usable": True, "optionName1": "블랙", "optionName2": "S"},
                {"id": 2, "stockQuantity": 5, "price": 1000, "usable": True, "optionName1": "블랙", "optionName2": "M"},
            ],
            "useStockManagement": True,
        }},
    },
    "smartstoreChannelProduct": {"naverShoppingRegistration": False, "channelProductDisplayStatusType": "SUSPENSION"},
}
SIMPLE = {"originProduct": {"statusType": "SUSPENSION", "name": "단일", "salePrice": 12000, "stockQuantity": 100,
                            "detailAttribute": {}},
          "smartstoreChannelProduct": {"channelProductDisplayStatusType": "SUSPENSION"}}


def search_item(no, name, status="SALE", stock=1):
    return {"originProductNo": no, "channelProducts": [{
        "channelProductNo": no + 1, "name": name, "statusType": status, "channelProductDisplayStatusType": "ON",
        "salePrice": 1000, "stockQuantity": stock, "wholeCategoryName": "a>b",
        "modifiedDate": "2026-11-01T15:14:07.377+09:00"}]}


class FakeClient:
    def __init__(self, detail=None, pages=None, fail_nos=()):
        self.detail, self.pages, self.fail_nos = detail, pages or [], set(fail_nos)
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(("GET", path))
        return copy.deepcopy(self.detail)

    def post(self, path, json=None, **kw):
        self.calls.append(("POST", path, json))
        return self.pages[json["page"] - 1]

    def put(self, path, json=None):
        self.calls.append(("PUT", path, json))
        no = int(path.split("/")[5])
        if no in self.fail_nos:
            raise ApiError(400, "BAD", "변경 불가")


class FakeServices:
    def __init__(self, client, db):
        self._client, self.db = client, db

    def client(self):
        return self._client


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def test_fetch_all_pages_and_filter(db):
    pages = [{"contents": [search_item(1, "블랙 티셔츠"), search_item(3, "원피스", "SUSPENSION")], "last": False},
             {"contents": [search_item(5, "화이트 티셔츠")], "last": True}]
    items = fetch_all(FakeServices(FakeClient(pages=pages), db))
    assert [p.origin_product_no for p in items] == [1, 3, 5]
    assert items[0].modified == "2026-11-01 15:14" and items[0].channel_product_no == 2
    assert [p.origin_product_no for p in filter_products(items, "티셔츠")] == [1, 5]
    assert [p.origin_product_no for p in filter_products(items, "", "SUSPENSION")] == [3]
    assert [p.origin_product_no for p in filter_products(items, "5")] == [5]


def test_load_edit_and_build_body(db):
    edit = load_edit(FakeServices(FakeClient(DETAIL), db), 77)
    assert edit.group_names == ["색상", "사이즈"] and edit.effective_stock() == 15
    assert [o.values for o in edit.options] == [["블랙", "S"], ["블랙", "M"]]
    edit.options[1].stock, edit.options[1].usable = 3, False
    edit.options[0].stock = 7
    edit.sale_price, edit.display_status = 20000, "ON"
    body = build_update_body(edit)
    op = body["originProduct"]
    assert op["salePrice"] == 20000 and op["stockQuantity"] == 7
    assert op["detailAttribute"]["optionInfo"]["optionCombinations"][1] == {
        "id": 2, "stockQuantity": 3, "price": 1000, "usable": False, "optionName1": "블랙", "optionName2": "M"}
    assert body["smartstoreChannelProduct"]["channelProductDisplayStatusType"] == "ON"
    assert edit.raw["originProduct"]["salePrice"] == 19000  # 원본은 그대로


def test_validate_edit(db):
    edit = load_edit(FakeServices(FakeClient(DETAIL), db), 77)
    assert validate_edit(edit) == []
    edit.options[0].price = 500
    assert any("0원인 옵션" in e for e in validate_edit(edit))
    simple = load_edit(FakeServices(FakeClient(SIMPLE), db), 1)
    simple.stock, simple.status = 0, "SALE"
    assert any("재고가 0" in e for e in validate_edit(simple))
    simple.sale_price = 12005
    assert any("10원 단위" in e for e in validate_edit(simple))


def test_save_edit_order_and_noop(db):
    db.add_product(1, 2, "단일", 12000, 100, "SUSPENSION")
    client = FakeClient(SIMPLE)
    services = FakeServices(client, db)
    edit = load_edit(services, 1)
    assert save_edit(services, edit) == []
    assert [c[0] for c in client.calls] == ["GET"]

    edit.stock, edit.sale_price, edit.status = 50, 13000, "SALE"
    done = save_edit(services, edit)
    assert len(done) == 2
    puts = [c for c in client.calls if c[0] == "PUT"]
    assert puts[0][1] == "/external/v2/products/origin-products/1"      # 재고 먼저
    assert puts[1] == ("PUT", "/external/v1/products/origin-products/1/change-status", {"statusType": "SALE"})
    row = db.list_products()[0]
    assert (row["sale_price"], row["stock_quantity"], row["status"]) == (13000, 50, "SALE")


def test_status_only_change_skips_full_update(db):
    client = FakeClient(SIMPLE)
    services = FakeServices(client, db)
    edit = load_edit(services, 1)
    edit.status = "SALE"
    save_edit(services, edit)
    assert [c[1] for c in client.calls if c[0] == "PUT"] == ["/external/v1/products/origin-products/1/change-status"]


def test_bulk_change_status(db):
    db.add_product(10, None, "a", 1000, 1, "SALE")
    client = FakeClient(fail_nos={20})
    results = bulk_change_status(FakeServices(client, db), [10, 20], "SUSPENSION")
    assert [(r.origin_product_no, r.ok) for r in results] == [(10, True), (20, False)]
    assert results[1].message == "변경 불가"
    assert db.list_products()[0]["status"] == "SUSPENSION"


def test_summary_handles_missing_fields():
    p = ProductSummary.from_search({"originProductNo": 9, "channelProducts": []})
    assert (p.name, p.sale_price, p.modified) == ("", 0, "")


def test_bulk_sold_out_sets_all_stock_to_zero(db):
    from services.product_manager import bulk_sold_out
    client = FakeClient(DETAIL, fail_nos={88})
    services = FakeServices(client, db)
    results = bulk_sold_out(services, [77, 88])
    assert [r.ok for r in results] == [True, False] and results[1].message == "변경 불가"
    put = [c for c in client.calls if c[0] == "PUT"][0]
    op = put[2]["originProduct"]
    assert op["stockQuantity"] == 0
    assert all(c["stockQuantity"] == 0 for c in op["detailAttribute"]["optionInfo"]["optionCombinations"])
    assert op["statusType"] == DETAIL["originProduct"]["statusType"]              # 상태는 직접 바꾸지 않음
    assert not any("change-status" in c[1] for c in client.calls)

    simple = FakeClient(SIMPLE)
    assert bulk_sold_out(FakeServices(simple, db), [1])[0].ok
    assert [c for c in simple.calls if c[0] == "PUT"][0][2]["originProduct"]["stockQuantity"] == 0
    zero = copy.deepcopy(SIMPLE)
    zero["originProduct"]["stockQuantity"] = 0                                     # 이미 품절: 보낼 것 없음
    already = FakeClient(zero)
    assert bulk_sold_out(FakeServices(already, db), [1])[0].ok and not [c for c in already.calls if c[0] == "PUT"]


def test_sold_out_product_update_sends_valid_status(db):
    """품절(OUTOFSTOCK) 상품을 수정할 때 statusType을 그대로 보내면 네이버가 NotValidEnum으로 거절한다."""
    sold = copy.deepcopy(DETAIL)
    sold["originProduct"]["statusType"] = "OUTOFSTOCK"
    client = FakeClient(sold)
    services = FakeServices(client, db)
    edit = load_edit(services, 77)
    assert edit.status == "OUTOFSTOCK"
    assert save_edit(services, edit) == []                                  # 바꾼 게 없으면 보내지 않음
    for o in edit.options:
        o.stock = 5
    edit.status = "SALE"
    body = build_update_body(edit)
    assert body["originProduct"]["statusType"] == "SALE"
    save_edit(services, edit)
    sent = [c for c in client.calls if c[0] == "PUT"][0][2]
    assert sent["originProduct"]["statusType"] in ("SALE", "SUSPENSION")
    edit2 = load_edit(services, 77)                                         # 품절 그대로 두고 가격만 바꿈
    edit2.sale_price = 20000
    assert build_update_body(edit2)["originProduct"]["statusType"] == "SALE"


def test_restock_sold_out_sends_status_once(db):
    sold = copy.deepcopy(DETAIL)
    sold["originProduct"]["statusType"] = "OUTOFSTOCK"
    client = FakeClient(sold)
    services = FakeServices(client, db)
    edit = load_edit(services, 77)
    for o in edit.options:
        o.stock = 5
    edit.status = "SALE"
    done = save_edit(services, edit)
    puts = [c for c in client.calls if c[0] == "PUT"]
    assert len(puts) == 1 and puts[0][2]["originProduct"]["statusType"] == "SALE"   # 상태 변경 요청을 따로 안 보냄
    assert any("판매중" in d for d in done)
