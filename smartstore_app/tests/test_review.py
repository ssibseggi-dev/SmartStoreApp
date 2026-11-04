"""5단계: 검수 대기 목록과 일괄 판매 시작 (API는 가짜 클라이언트)."""
import copy
import os
import sqlite3

import pytest

from api.errors import ApiError
from services.automation import config, runner
from services.automation.review import pending_review, publish
from storage.db import Database
from tests.test_automation_register import FakeClient, FakeServices, add_rows, env, photos, row, sheet  # noqa: F401


class StoreClient(FakeClient):
    """등록한 상품을 기억했다가 조회·수정·상태 변경에 응답하는 대역."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.products: dict[int, dict] = {}
        self.updates: list[dict] = []
        self.status_calls: list[tuple[int, str]] = []
        self.missing: set[int] = set()
        self.deleted: list[int] = []

    def post(self, path, json=None, files=None):
        result = super().post(path, json=json, files=files)
        if path == "/external/v2/products":
            body = copy.deepcopy(json)
            body["originProduct"]["statusType"] = "SALE"
            self.products[result["originProductNo"]] = body
        return result

    def get(self, path, params=None):
        no = int(path.rsplit("/", 1)[1])
        if no in self.missing:
            raise ApiError(404, "NOT_FOUND", "존재하지 않는 상품입니다.")
        return copy.deepcopy(self.products[no])

    def delete(self, path):
        no = int(path.rsplit("/", 1)[1])
        if no in self.missing or no not in self.products:
            raise ApiError(404, "NOT_FOUND", "존재하지 않는 상품입니다.")
        self.deleted.append(no)
        del self.products[no]

    def put(self, path, json=None):
        no = int(path.split("/")[5])
        if path.endswith("/change-status"):
            self.status_calls.append((no, json["statusType"]))
            self.products[no]["originProduct"]["statusType"] = json["statusType"]
        elif path.startswith("/external/v2/products/origin-products/"):
            self.updates.append(json)
            self.products[no] = copy.deepcopy(json)
        super().put(path, json)


def register(env, codes, client=None, **row_kw):
    db, root, excel = env
    for code in codes:
        photos(root, code)
    add_rows(excel, [row(code, **row_kw) for code in codes])
    client = client or StoreClient()
    runner.run_waiting(FakeServices(db, client), dry_run=False)
    return client


def test_pending_review_lists_registered_items(env):
    db, root, excel = env
    register(env, ["A001", "A002"])
    items = pending_review(db)
    assert [i.code for i in items] == ["A001", "A002"] or [i.code for i in items] == ["A002", "A001"]
    item = next(i for i in items if i.code == "A001")
    assert item.name == "상품 A001" and item.sale_price == 13900 and item.ai_draft is False
    assert item.image and item.image.parent.name == "보정"           # 썸네일은 보정본
    assert item.detail_page and item.detail_page.exists()           # 등록한 상세페이지
    assert item.registered_at


def test_publish_turns_on_sale_display_and_shopping(env):
    db, root, excel = env
    client = register(env, ["A001", "A002"])
    no = db.get_automation_item("A001")["origin_product_no"]
    results, excel_written = publish(FakeServices(db, client), ["A001"])
    assert [(r.code, r.ok) for r in results] == [("A001", True)] and excel_written
    channel = client.updates[-1]["smartstoreChannelProduct"]
    assert channel["channelProductDisplayStatusType"] == "ON" and channel["naverShoppingRegistration"] is True
    assert client.status_calls[-1] == (no, "SALE")
    assert sheet(excel)["A001"][0] == "판매중" and sheet(excel)["A002"][0] == "등록완료(판매중지)"
    assert db.get_automation_item("A001")["status"] == "판매중"
    assert [i.code for i in pending_review(db)] == ["A002"]
    assert ("publish", "성공", "A001") in [(r["action"], r["result"], r["code"]) for r in db.list_run_log()]


def test_publish_without_naver_shopping(env):
    db, root, excel = env
    client = register(env, ["A001"])
    s = config.load_settings(db)
    s.publish_naver_shopping = False
    config.save_settings(db, s)
    publish(FakeServices(db, client), ["A001"])
    assert client.updates[-1]["smartstoreChannelProduct"]["naverShoppingRegistration"] is False


def test_publish_failures_are_reported_and_others_continue(env):
    db, root, excel = env
    client = register(env, ["A001", "A002", "A003"])
    client.missing.add(db.get_automation_item("A001")["origin_product_no"])          # 스토어에서 지워진 상품
    zero = db.get_automation_item("A002")["origin_product_no"]
    client.products[zero]["originProduct"]["stockQuantity"] = 0                       # 재고 0
    results, _ = publish(FakeServices(db, client), ["A001", "A002", "A003", "ZZZ"])
    by = {r.code: r for r in results}
    assert not by["A001"].ok and "상품이 없습니다" in by["A001"].message
    assert not by["A002"].ok and "재고가 0" in by["A002"].message
    assert by["A003"].ok
    assert not by["ZZZ"].ok and "검수 대기" in by["ZZZ"].message
    assert sheet(excel)["A001"][0] == "등록완료(판매중지)"                                # 실패는 상태 유지
    assert {i.code for i in pending_review(db)} == {"A001", "A002"}


def test_publish_with_locked_excel_syncs_later(env, monkeypatch):
    db, root, excel = env
    client = register(env, ["A001"])
    from services.automation import review
    real = review.write_results
    monkeypatch.setattr(review, "write_results", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")))
    results, excel_written = publish(FakeServices(db, client), ["A001"])
    assert results[0].ok and not excel_written and sheet(excel)["A001"][0] == "등록완료(판매중지)"
    monkeypatch.setattr(review, "write_results", real)
    report = runner.run_waiting(FakeServices(db, client), dry_run=False)               # 다음 실행 때 반영
    assert report.synced_previous == 1 and sheet(excel)["A001"][0] == "판매중"


def test_cli_publish(env, monkeypatch, capsys):
    import cli
    db, root, excel = env
    client = register(env, ["A001", "A002"])
    monkeypatch.setattr(cli, "AppServices", lambda d: FakeServices(d, client))
    assert cli.main(["publish"]) == 0
    out = capsys.readouterr().out
    assert "검수 대기(등록완료·판매중지) 2건" in out and "A001  상품 A001  13,900원" in out
    assert cli.main(["publish", "--codes", "A001"]) == 0
    assert "A001  [판매 시작]" in capsys.readouterr().out
    assert cli.main(["publish", "--all"]) == 0
    out = capsys.readouterr().out
    assert "A002  [판매 시작]" in out and "A001" not in out
    assert cli.main(["publish", "--codes", "NONE"]) == 1


def test_db_migration_adds_review_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE automation_items (code TEXT PRIMARY KEY, status TEXT NOT NULL, origin_product_no "
                 "INTEGER, message TEXT, excel_synced INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL "
                 "DEFAULT (datetime('now','localtime')))")
    conn.execute("INSERT INTO automation_items(code, status, origin_product_no) VALUES('OLD', '등록완료(판매중지)', 5)")
    conn.commit()
    conn.close()
    db = Database(path)
    row = db.get_automation_item("OLD")
    assert row["origin_product_no"] == 5 and row["ai_draft"] == 0 and row["name"] is None
    db.upsert_automation_item("OLD", "판매중", message="x")
    assert db.get_automation_item("OLD")["origin_product_no"] == 5                    # 기존 값 유지
    db.close()


def test_review_page_ui(env):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from ui.review_page import ReviewPage
    app = QApplication.instance() or QApplication([])
    db, root, excel = env
    register(env, ["A001", "A002"])
    page = ReviewPage(FakeServices(db, StoreClient()))
    assert page.table.rowCount() == 2 and not page.publish_btn.isEnabled()
    assert not page.table.item(0, 1).icon().isNull()                                   # 썸네일
    page.on_toggle_all()
    assert page.publish_btn.isEnabled() and page.publish_btn.text().endswith("(2)")
    page.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert page.publish_btn.text().endswith("(1)")
    page.shopping_check.setChecked(False)
    assert config.load_settings(db).publish_naver_shopping is False
    page.deleteLater()
    app.processEvents()


def test_edit_name_price_from_review(env, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import ui.review_page as review_page
    from services.product_manager import save_edit, validate_edit
    app = QApplication.instance() or QApplication([])
    db, root, excel = env
    client = StoreClient()
    register(env, ["A001"], client=client)
    services = FakeServices(db, client)
    monkeypatch.setattr(review_page, "run_async", lambda fn, done, fail: done(fn()))   # 스레드 없이

    opened = {}

    class FakeDialog:
        def __init__(self, services_, edit, parent=None, **kw):
            opened.update(kw, edit=edit)
            self.saved_actions = []

        def exec(self):                                  # 사람이 상품명·판매가를 고치고 저장한 것처럼
            e = opened["edit"]
            e.name, e.sale_price = "고친 상품명 반팔 티셔츠", 15900
            assert not validate_edit(e)
            assert "금지어" in " ".join(validate_edit(type(e)(**{**e.__dict__, "name": "최저가 티셔츠"}), ["최저가"]))
            self.saved_actions = save_edit(services, e)
            return True

    monkeypatch.setattr(review_page, "ProductEditDialog", FakeDialog)
    page = review_page.ReviewPage(services)
    status_calls = len(client.status_calls)                                  # 등록 때 판매중지 전환 1번
    assert not page.edit_btn.isEnabled()
    page.table.setCurrentCell(0, 3)
    assert page.edit_btn.isEnabled()
    page.on_edit()
    assert opened["lock_status"] is True                                    # 판매 상태는 '판매 시작' 버튼으로
    sent = client.updates[-1]["originProduct"]
    assert sent["name"] == "고친 상품명 반팔 티셔츠" and sent["salePrice"] == 15900
    assert sent["statusType"] == "SUSPENSION" and len(client.status_calls) == status_calls   # 상태는 그대로
    assert page.table.item(0, 3).text() == "고친 상품명 반팔 티셔츠" and page.table.item(0, 4).text() == "15,900원"
    assert "저장 완료" in page.info.text()
    page.deleteLater()
    app.processEvents()


def test_edit_dialog_name_field(env):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from services.product_manager import ProductEdit
    from ui.product_edit_dialog import ProductEditDialog
    app = QApplication.instance() or QApplication([])
    db, root, excel = env
    edit = ProductEdit(1, "원래 이름", 10000, 5, "SUSPENSION", "ON",
                       raw={"originProduct": {"statusType": "SUSPENSION"}})
    dialog = ProductEditDialog(FakeServices(db, StoreClient()), edit, lock_status=True, banned_words=["최저가"])
    assert dialog.name_edit.text() == "원래 이름" and not dialog.status_combo.isEnabled()
    dialog.name_edit.setText("  새 이름  ")
    assert dialog._collect().name == "새 이름" and dialog.name_count.text() == "4/100자"
    dialog.deleteLater()
    app.processEvents()


def test_delete_registered_products(env, monkeypatch):
    from services.automation.review import delete_registered
    db, root, excel = env
    client = register(env, ["A001", "A002", "A003"])
    services = FakeServices(db, client)
    no = {i.code: i.origin_product_no for i in pending_review(db)}
    client.missing.add(no["A002"])                                   # 스토어에서 이미 지운 상품
    results, excel_written = delete_registered(services, ["A001", "A002", "Z999"])
    by_code = {r.code: r for r in results}
    assert by_code["A001"].ok and client.deleted == [no["A001"]]
    assert by_code["A002"].ok and "이미 없어" in by_code["A002"].message
    assert not by_code["Z999"].ok
    assert [i.code for i in pending_review(db)] == ["A003"]               # 검수 목록에서 빠짐
    assert excel_written and sheet(excel)["A001"][0] == "삭제됨" and sheet(excel)["A003"][0] == "등록완료(판매중지)"
    assert db.get_automation_item("A001")["status"] == "삭제됨"

    monkeypatch.setattr("services.automation.review.write_results",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError("열림")))
    results, excel_written = delete_registered(services, ["A003"])
    assert results[0].ok and not excel_written and not db.get_automation_item("A003")["excel_synced"]


def test_review_page_delete_button(env, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox
    import ui.review_page as review_page
    app = QApplication.instance() or QApplication([])
    db, root, excel = env
    client = register(env, ["A001", "A002"])
    page = review_page.ReviewPage(FakeServices(db, client))
    assert not page.delete_btn.isEnabled()
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert page.delete_btn.isEnabled() and page.delete_btn.text().endswith("(1)")
    monkeypatch.setattr(review_page, "run_with_progress", lambda fn, prog, done, fail: done(fn(prog)))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: next(
        (b for b in self.buttons() if b.text() == "취소"), None))
    page.on_delete()                                                  # 취소 → 아무것도 안 지움
    assert not client.deleted
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: next(
        (b for b in self.buttons() if b.text().endswith("개 삭제")), None))
    page.on_delete()
    assert len(client.deleted) == 1 and page.table.rowCount() == 1
    page.deleteLater()
    app.processEvents()


def test_deleted_store_product_does_not_block_reregistering(env):
    from services.automation.runner import StoreCodeIndex
    from api.products import ProductApi

    class SearchClient:
        def post(self, path, json=None):
            return {"contents": [
                {"originProductNo": 1, "channelProducts": [{"sellerManagementCode": "L002", "statusType": "DELETE"}]},
                {"originProductNo": 2, "channelProducts": [{"sellerManagementCode": "L001", "statusType": "SUSPENSION"}]},
            ], "last": True}

    index = StoreCodeIndex(ProductApi(SearchClient()))
    assert index.find("L002") is None and index.find("L001") == 2


def test_edit_targets_checked_row_not_cursor_row(env, monkeypatch):
    """체크는 A002인데 선택(커서) 줄이 A001이면 A002를 고쳐야 한다."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    import ui.review_page as review_page
    app = QApplication.instance() or QApplication([])
    db, root, excel = env
    client = register(env, ["A001", "A002"])
    page = review_page.ReviewPage(FakeServices(db, client))
    codes = [i.code for i in page._items]
    row_a1, row_a2 = codes.index("A001"), codes.index("A002")
    page.table.item(row_a2, 0).setCheckState(Qt.CheckState.Checked)
    page.table.setCurrentCell(row_a1, 3)                              # 커서는 A001
    assert page.edit_btn.text() == "A002 수정"
    loaded = []
    monkeypatch.setattr(review_page, "run_async", lambda fn, done, fail: loaded.append(fn()))
    page.on_edit()
    assert loaded[0].origin_product_no == page._items[row_a2].origin_product_no
    page.table.item(row_a1, 0).setCheckState(Qt.CheckState.Checked)   # 둘 다 체크 → 커서 줄
    page.table.setCurrentCell(row_a1, 3)
    assert page.edit_btn.text() == "A001 수정"
    page.deleteLater()
    app.processEvents()


def test_deleted_history_reregister_and_forget(env):
    from openpyxl import load_workbook
    from services.automation.review import delete_registered, deleted_items, forget_deleted, reregister
    db, root, excel = env
    client = register(env, ["A001", "A002", "A003"])
    services = FakeServices(db, client)
    old = {i.code: i.origin_product_no for i in pending_review(db)}
    delete_registered(services, ["A001", "A002"])
    # A002는 작업 엑셀에서 줄을 지움 → 다시 등록 불가
    wb = load_workbook(excel)
    ws = wb["상품"]
    for r in range(ws.max_row, 1, -1):
        if ws.cell(r, 1).value == "A002":
            ws.delete_rows(r)
    wb.save(excel)
    items = {d.code: d for d in deleted_items(db)}
    assert set(items) == {"A001", "A002"} and items["A001"].in_excel and not items["A002"].in_excel
    assert items["A001"].old_product_no == old["A001"] and items["A001"].image is not None

    report = reregister(services, ["A001", "A002"])
    assert report.registered_count == 1 and [p.code for p in report.prepared] == ["A001"]
    new_no = db.get_automation_item("A001")["origin_product_no"]
    assert new_no != old["A001"] and sheet(excel)["A001"][:2] == ("등록완료(판매중지)", new_no)
    assert {i.code for i in pending_review(db)} == {"A001", "A003"}          # 검수 대기로 돌아옴
    assert [d.code for d in deleted_items(db)] == ["A002"]

    with pytest.raises(ValueError, match="다시 등록할 수 있는 상품이 없습니다"):
        reregister(services, ["A002"])
    assert forget_deleted(db, ["A002", "A003"]) == 1 and deleted_items(db) == []
    assert db.get_automation_item("A003")["status"] == "등록완료(판매중지)"     # 삭제 내역 아닌 상품은 그대로


def test_deleted_page_ui(env, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox
    from services.automation.review import delete_registered
    import ui.deleted_page as deleted_page
    app = QApplication.instance() or QApplication([])
    db, root, excel = env
    client = register(env, ["A001", "A002"])
    services = FakeServices(db, client)
    delete_registered(services, ["A001"])
    page = deleted_page.DeletedPage(services)
    assert page.table.rowCount() == 1 and page.table.item(0, 7).text() == "가능"
    assert not page.restore_btn.isEnabled()
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert page.restore_btn.isEnabled() and page.restore_btn.text().endswith("(1)")
    monkeypatch.setattr(deleted_page, "run_with_progress", lambda fn, prog, done, fail: done(fn(prog)))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    page.on_restore()
    assert page.table.rowCount() == 0 and len(client.registered) == 3
    page.deleteLater()
    app.processEvents()


def test_edit_dialog_restock_sold_out_product(env):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QAbstractItemView
    from services.product_manager import EditableOption, ProductEdit
    from ui.product_edit_dialog import ProductEditDialog
    app = QApplication.instance() or QApplication([])
    db, root, excel = env
    opts = [EditableOption(1, ["카키", "FREE"], 0, 0, True), EditableOption(2, ["네이비", "FREE"], 0, 0, True)]
    edit = ProductEdit(5, "코듀로이 후드티", 22000, 0, "OUTOFSTOCK", "ON", group_names=["색상", "사이즈"], options=opts,
                       raw={"originProduct": {"statusType": "OUTOFSTOCK"}})
    dialog = ProductEditDialog(FakeServices(db, StoreClient()), edit)
    assert not dialog.stock_spin.isEnabled() and dialog.status_combo.currentData() == "OUTOFSTOCK"
    assert dialog.table.editTriggers() == QAbstractItemView.EditTrigger.AllEditTriggers
    assert dialog.table.item(0, 3).flags() & Qt.ItemFlag.ItemIsEditable                   # 재고 칸 입력 가능
    dialog.table.item(0, 3).setData(Qt.ItemDataRole.EditRole, 10)
    assert dialog.stock_spin.value() == 10 and dialog.status_combo.currentData() == "SALE"
    assert "판매중" in dialog.status.text()
    dialog.deleteLater()

    single = ProductEdit(6, "단일", 10000, 0, "OUTOFSTOCK", "ON", raw={"originProduct": {"statusType": "OUTOFSTOCK"}})
    d2 = ProductEditDialog(FakeServices(db, StoreClient()), single)
    d2.stock_spin.setValue(5)
    assert d2.status_combo.currentData() == "SALE"
    d2.deleteLater()
    app.processEvents()
