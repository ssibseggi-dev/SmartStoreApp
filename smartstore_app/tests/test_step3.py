import os

import pytest

from services import drafts, templates
from services.options import (
    OptionGroup, OptionRow, build_option_info, generate_rows, parse_values, price_range, validate_options,
)
from services.product_builder import ProductDraft, build_payload
from storage.db import Database
from tests.test_product import make_draft


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def test_parse_values():
    assert parse_values(" 블랙, 화이트,,블랙 ,") == ["블랙", "화이트"]


def test_generate_rows_keeps_existing_values():
    groups = [OptionGroup("색상", ["블랙", "화이트"]), OptionGroup("사이즈", ["S", "M"])]
    rows = generate_rows(groups, default_stock=5)
    assert [r.values for r in rows] == [["블랙", "S"], ["블랙", "M"], ["화이트", "S"], ["화이트", "M"]]
    rows[1].price, rows[1].stock = 1000, 7
    groups[1].values.append("L")
    regenerated = generate_rows(groups, rows, default_stock=3)
    assert len(regenerated) == 6
    kept = next(r for r in regenerated if r.values == ["블랙", "M"])
    assert (kept.price, kept.stock) == (1000, 7)
    assert next(r for r in regenerated if r.values == ["블랙", "L"]).stock == 3


def test_price_range():
    assert price_range(1500) == (0, 1500)
    assert price_range(5000) == (-2500, 5000)
    assert price_range(20000) == (-10000, 10000)


def test_validate_options():
    groups = [OptionGroup("색상", ["블랙", "화이트"])]
    rows = generate_rows(groups, default_stock=1)
    assert validate_options(groups, rows, 20000) == []
    rows[0].price = rows[1].price = 1000
    assert any("0원인 옵션" in e for e in validate_options(groups, rows, 20000))
    rows[0].price = 15000
    assert any("10원 단위" in e for e in validate_options(groups, rows, 20000))
    groups[0].values.append("네이비")
    assert any("조합 생성" in e for e in validate_options(groups, rows, 20000))
    assert validate_options([OptionGroup("색상", [])], [], 20000)


def test_payload_with_options(tmp_path):
    groups = [OptionGroup("색상", ["블랙", "화이트"]), OptionGroup("사이즈", ["S"])]
    rows = generate_rows(groups, default_stock=4)
    rows[1].usable = False
    rows[0].seller_code = "BK-S"
    d = make_draft(tmp_path, use_options=True, option_groups=groups, option_rows=rows)
    op = build_payload(d, "u", [])["originProduct"]
    assert op["stockQuantity"] == 4
    info = op["detailAttribute"]["optionInfo"]
    assert info["optionCombinationGroupNames"] == {"optionGroupName1": "색상", "optionGroupName2": "사이즈"}
    assert info["optionCombinations"][0] == {"optionName1": "블랙", "optionName2": "S", "stockQuantity": 4,
                                             "price": 0, "usable": True, "sellerManagerCode": "BK-S"}
    assert info["optionCombinations"][1]["usable"] is False
    assert "optionInfo" not in build_payload(make_draft(tmp_path), "u", [])["originProduct"]["detailAttribute"]
    assert build_option_info(groups, rows)["useStockManagement"] is True


def test_templates(db):
    d = ProductDraft(delivery_fee_type="PAID", base_fee=2500, as_phone="02-111-2222")
    db.save_template("delivery", "유료 2500", templates.extract("delivery", d))
    db.save_template("as", "본사", templates.extract("as", d))
    db.save_template("delivery", "유료 2500", templates.extract("delivery", ProductDraft(base_fee=3500)))
    [t] = templates.list_templates(db, "delivery")
    assert t.data["base_fee"] == 3500 and "as_phone" not in t.data

    assert templates.apply_defaults(db, ProductDraft()).base_fee == 3000
    db.set_default_template("delivery", t.id)
    assert templates.apply_defaults(db, ProductDraft()).base_fee == 3500
    db.set_default_template("delivery", None)
    assert templates.default_template(db, "delivery") is None
    db.delete_template(t.id)
    assert templates.list_templates(db, "delivery") == []


def test_drafts_roundtrip(db, tmp_path):
    groups = [OptionGroup("색상", ["블랙"])]
    d = make_draft(tmp_path, use_options=True, option_groups=groups, option_rows=generate_rows(groups))
    draft_id = drafts.save(db, None, d)
    loaded = drafts.load(db, draft_id)
    assert loaded == d and isinstance(loaded.option_rows[0], OptionRow)
    d.name = "수정됨"
    assert drafts.save(db, draft_id, d) == draft_id
    [s] = drafts.list_summaries(db)
    assert (s.name, s.option_count) == ("수정됨", 1)
    os.remove(d.representative_image)
    assert drafts.missing_images(d) == [d.representative_image]
    assert ProductDraft.from_dict({"name": "x", "unknown_field": 1}).name == "x"


def test_product_page_roundtrip(db, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from services.connection import AppServices
    from ui.product_page import ProductPage
    app = QApplication.instance() or QApplication([])

    groups = [OptionGroup("색상", ["블랙", "화이트"]), OptionGroup("사이즈", ["S", "M"])]
    rows = generate_rows(groups, default_stock=2)
    rows[3].price, rows[2].usable, rows[0].seller_code = 500, False, "A1"
    d = make_draft(tmp_path, use_options=True, option_groups=groups, option_rows=rows, delivery_company="LOGEN",
                   delivery_fee_type="CONDITIONAL_FREE", free_condition_amount=50000, optional_images=["x.png"])
    page = ProductPage(AppServices(db))
    page.load_draft(d)
    out = page.to_draft()
    d.stock_quantity = out.stock_quantity  # 옵션 사용 시 화면 재고는 옵션 합계로 표시
    assert out == d
    assert out.effective_stock() == 6
    page.deleteLater()
    app.processEvents()
