import struct
import zlib

from api.errors import ApiError
from services.product_builder import ProductDraft, build_payload, to_detail_html
from services.product_service import register_product
from services.validator import validate
from storage.db import Database


def write_png(path):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    raw = zlib.compress(b"\x00\xff\x00\x00")
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", raw) + chunk(b"IEND", b""))
    return str(path)


def make_draft(tmp_path, **kw):
    d = ProductDraft(leaf_category_id="50000807", name="테스트 원피스", sale_price=19900, stock_quantity=10,
                     representative_image=write_png(tmp_path / "rep.png"), detail_html="설명",
                     as_phone="010-0000-0000")
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def test_validate_ok_and_errors(tmp_path):
    assert validate(make_draft(tmp_path)) == []
    errors = validate(make_draft(tmp_path, sale_price=19905, leaf_category_id="", as_phone=" ",
                                 delivery_fee_type="PAID", base_fee=0))
    assert any("10원 단위" in e for e in errors)
    assert any("카테고리" in e for e in errors)
    assert any("A/S" in e for e in errors)
    assert any("기본 배송비" in e for e in errors)
    bad = tmp_path / "a.txt"
    bad.write_text("x")
    assert any("JPG" in e for e in validate(make_draft(tmp_path, optional_images=[str(bad)])))


def test_payload_test_mode(tmp_path):
    p = build_payload(make_draft(tmp_path, naver_shopping=True), "http://rep", ["http://o1"])
    op = p["originProduct"]
    assert op["statusType"] == "SALE" and op["leafCategoryId"] == "50000807"
    assert op["images"]["representativeImage"]["url"] == "http://rep"
    assert op["images"]["optionalImages"] == [{"url": "http://o1"}]
    assert op["deliveryInfo"]["deliveryFee"] == {"deliveryFeeType": "FREE"}
    assert op["detailAttribute"]["productInfoProvidedNotice"]["productInfoProvidedNoticeType"] == "ETC"
    assert p["smartstoreChannelProduct"] == {"naverShoppingRegistration": False,
                                             "channelProductDisplayStatusType": "SUSPENSION"}


def test_payload_conditional_fee(tmp_path):
    d = make_draft(tmp_path, test_mode=False, delivery_fee_type="CONDITIONAL_FREE",
                   base_fee=3000, free_condition_amount=50000)
    fee = build_payload(d, "u", [])["originProduct"]["deliveryInfo"]["deliveryFee"]
    assert fee == {"deliveryFeeType": "CONDITIONAL_FREE", "baseFee": 3000,
                   "deliveryFeePayType": "PREPAID", "freeConditionalAmount": 50000}


def test_detail_html():
    assert to_detail_html("a<b\n둘째") == "a&lt;b<br>둘째"
    assert to_detail_html("<p>x</p>") == "<p>x</p>"


class FakeClient:
    def __init__(self, fail_status=False):
        self.calls = []
        self.fail_status = fail_status

    def post(self, path, json=None, files=None):
        self.calls.append(("POST", path, files or json))
        if path.endswith("/upload"):
            return {"images": [{"url": f"http://img/{i}"} for i in range(len(files))]}
        return {"originProductNo": 111, "smartstoreChannelProductNo": 222}

    def put(self, path, json=None):
        self.calls.append(("PUT", path, json))
        if self.fail_status:
            raise ApiError(400, "BAD", "fail")


class FakeServices:
    def __init__(self, client, db):
        self._client = client
        self.db = db

    def client(self):
        return self._client


def test_register_flow(tmp_path):
    client, db = FakeClient(), Database(tmp_path / "t.db")
    out = register_product(FakeServices(client, db), make_draft(tmp_path))
    assert (out.origin_product_no, out.status, out.warning) == (111, "SUSPENSION", None)
    assert [c[:2] for c in client.calls] == [
        ("POST", "/external/v1/product-images/upload"),
        ("POST", "/external/v2/products"),
        ("PUT", "/external/v1/products/origin-products/111/change-status"),
    ]
    assert client.calls[2][2] == {"statusType": "SUSPENSION"}
    assert db.list_products()[0]["status"] == "SUSPENSION"


def test_register_status_change_failure_warns(tmp_path):
    client, db = FakeClient(fail_status=True), Database(tmp_path / "t.db")
    out = register_product(FakeServices(client, db), make_draft(tmp_path))
    assert out.status == "SALE" and "전시중지" in out.warning
