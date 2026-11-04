import pytest
from openpyxl import load_workbook

from api.categories import Category
from api.errors import ApiError
from services import templates
from services.bulk_register import register_rows
from services.excel_import import (
    COLUMNS, OPTION_SHEET, PRODUCT_SHEET, read_workbook, write_results, write_template,
)
from services.product_builder import ProductDraft
from storage.db import Database
from tests.test_product import write_png

HEADERS = [c[0] for c in COLUMNS]


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.replace_categories([Category("50000803", "티셔츠", "패션의류>여성의류>티셔츠", True),
                          Category("50000167", "여성의류", "패션의류>여성의류", False)])
    d.save_template("as", "본사", templates.extract("as", ProductDraft(as_phone="02-123-4567")))
    d.set_default_template("as", templates.list_templates(d, "as")[0].id)
    d.save_template("delivery", "유료", templates.extract("delivery", ProductDraft(delivery_fee_type="PAID",
                                                                                 base_fee=2500)))
    yield d
    d.close()


def make_file(tmp_path, db, rows, option_rows=()):
    """양식을 만든 뒤 예시 행을 지우고 rows(dict 목록)를 채운다."""
    path = tmp_path / "upload.xlsx"
    write_template(path, db)
    wb = load_workbook(path)
    ws = wb[PRODUCT_SHEET]
    ws.delete_rows(2)
    for r in rows:
        ws.append([r.get(h) for h in HEADERS])
    opt = wb[OPTION_SHEET]
    opt.delete_rows(2)
    for r in option_rows:
        opt.append(r)
    wb.save(path)
    return path


def base_row(**kw):
    row = {"상품키": "A1", "카테고리ID": "50000803", "상품명": "티셔츠", "판매가": 19900, "재고수량": 30,
           "대표이미지": "img/rep.png", "상세설명": "설명"}
    row.update(kw)
    return row


def test_template_has_sheets_and_guide(tmp_path, db):
    path = tmp_path / "t.xlsx"
    write_template(path, db)
    wb = load_workbook(path)
    assert wb.sheetnames == ["상품", "옵션상세", "안내"]
    assert [c.value for c in wb["상품"][1]] == HEADERS
    guide = "\n".join(str(c.value) for row in wb["안내"].iter_rows() for c in row if c.value)
    assert "★ 본사" in guide and "유료" in guide


def test_read_simple_row_with_defaults_and_relative_image(tmp_path, db):
    (tmp_path / "img").mkdir()
    write_png(tmp_path / "img" / "rep.png")
    path = make_file(tmp_path, db, [base_row(판매가="19,900", 배송템플릿="유료")])
    [row] = read_workbook(path, db)
    assert row.ok, row.errors
    d = row.draft
    assert (d.sale_price, d.stock_quantity, d.as_phone) == (19900, 30, "02-123-4567")
    assert (d.delivery_fee_type, d.base_fee) == ("PAID", 2500)
    assert d.category_name == "패션의류>여성의류>티셔츠" and d.test_mode is True
    assert d.representative_image == str((tmp_path / "img" / "rep.png").resolve())


def test_read_errors(tmp_path, db):
    write_png(tmp_path / "rep.png")
    rows = [base_row(카테고리ID="50000167", 대표이미지="rep.png"),
            base_row(판매가="abc", 대표이미지="rep.png", 반품템플릿="없는템플릿"),
            base_row(대표이미지="missing.png", 테스트등록="N")]
    r1, r2, r3 = read_workbook(make_file(tmp_path, db, rows), db)
    assert any("최하위" in e for e in r1.errors)
    assert any("숫자" in e for e in r2.errors) and any("없는템플릿" in e for e in r2.errors)
    assert any("찾을 수 없습니다" in e for e in r3.errors) and r3.draft.test_mode is False
    assert (r1.row_no, r2.row_no, r3.row_no) == (2, 3, 4)


def test_read_options_with_details(tmp_path, db):
    write_png(tmp_path / "rep.png")
    row = base_row(대표이미지="rep.png", 옵션명1="색상", 옵션값1="블랙, 화이트", 옵션명2="사이즈", 옵션값2="S,M",
                   옵션재고=5)
    details = [["A1", "블랙", "M", None, 1000, 2, "BK-M", "Y"], ["A1", "화이트", "S", None, None, None, None, "N"],
               ["A1", "레드", "S", None, 0, 1, None, None]]
    [r] = read_workbook(make_file(tmp_path, db, [row], details), db)
    rows = {tuple(o.values): o for o in r.draft.option_rows}
    assert len(rows) == 4 and r.draft.use_options
    assert (rows["블랙", "M"].price, rows["블랙", "M"].stock, rows["블랙", "M"].seller_code) == (1000, 2, "BK-M")
    assert rows["화이트", "S"].usable is False and rows["화이트", "S"].stock == 5
    assert r.errors == ["옵션상세 4행: 조합 '레드 / S'이(가) 옵션값에 없습니다."]


def test_missing_required_column(tmp_path, db):
    path = make_file(tmp_path, db, [base_row()])
    wb = load_workbook(path)
    wb[PRODUCT_SHEET].delete_cols(3)  # 상품명 열 삭제
    wb.save(path)
    with pytest.raises(ValueError, match="상품명"):
        read_workbook(path, db)


class FakeClient:
    def __init__(self, fail_names=()):
        self.fail_names, self.n = set(fail_names), 0

    def post(self, path, json=None, files=None):
        if path.endswith("/upload"):
            return {"images": [{"url": f"u{i}"} for i in range(len(files))]}
        if json["originProduct"]["name"] in self.fail_names:
            raise ApiError(400, "BAD_REQUEST", "입력한 데이터가 유효하지 않습니다.",
                           invalid_inputs=[{"name": "originProduct.salePrice", "message": "범위 오류"}])
        self.n += 1
        return {"originProductNo": 1000 + self.n, "smartstoreChannelProductNo": 2000 + self.n}

    def put(self, path, json=None):
        pass


class FakeServices:
    def __init__(self, client, db):
        self._client, self.db = client, db

    def client(self):
        return self._client


def test_register_rows_continues_after_failure_and_writes_results(tmp_path, db):
    write_png(tmp_path / "rep.png")
    rows = read_workbook(make_file(tmp_path, db, [
        base_row(상품명="A", 대표이미지="rep.png"), base_row(상품명="B", 대표이미지="rep.png"),
        base_row(상품명="C", 대표이미지="없음.png"), base_row(상품명="D", 대표이미지="rep.png")]), db)
    progress = []
    results = register_rows(FakeServices(FakeClient(fail_names={"B"}), db), rows,
                            on_progress=lambda r, i, n: progress.append((r.row_no, i, n)))
    assert [(r.row_no, r.ok) for r in results] == [(2, True), (3, False), (5, True)]  # 4행(C)은 검증 오류로 제외
    assert "originProduct.salePrice: 범위 오류" in results[1].message
    assert progress == [(2, 1, 3), (3, 2, 3), (5, 3, 3)]
    assert results[0].origin_product_no == 1001 and results[0].sale_status == "SUSPENSION"

    out = tmp_path / "result.xlsx"
    write_results(out, rows, {r.row_no: r.as_dict() for r in results})
    data = list(load_workbook(out).active.iter_rows(min_row=2, values_only=True))
    assert [(d[0], d[3], d[4]) for d in data] == [(2, "성공", 1001), (3, "실패", None), (4, "검증 오류", None),
                                                 (5, "성공", 1002)]
    assert data[0][5] == "판매중지"


def test_register_rows_cancel(tmp_path, db):
    write_png(tmp_path / "rep.png")
    rows = read_workbook(make_file(tmp_path, db, [base_row(상품명=n, 대표이미지="rep.png") for n in "ABC"]), db)
    done = []
    results = register_rows(FakeServices(FakeClient(), db), rows, on_progress=lambda r, i, n: done.append(i),
                            is_cancelled=lambda: len(done) >= 1)
    assert len(results) == 1


def test_excel_page_clear(tmp_path, db, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from services.bulk_register import RowResult
    from ui.excel_page import ExcelPage
    app = QApplication.instance() or QApplication([])

    write_png(tmp_path / "rep.png")
    path = make_file(tmp_path, db, [base_row(대표이미지="rep.png")])
    page = ExcelPage(FakeServices(FakeClient(), db))
    page._on_loaded(path, read_workbook(path, db))
    assert page.table.rowCount() == 1 and page.clear_btn.isEnabled()

    # 등록 성공 결과가 있으면 확인을 묻고, 취소하면 그대로 둔다
    page._results[2] = RowResult(2, True, 1001)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    page.on_clear()
    assert page.table.rowCount() == 1

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    page.on_clear()
    assert (page.table.rowCount(), page._rows, page._results, page._path) == (0, [], {}, None)
    assert not page.clear_btn.isEnabled() and not page.register_btn.isEnabled()
    assert path.exists()  # PC의 파일은 지우지 않음
    page.deleteLater()
    app.processEvents()
