"""2단계: 실제 등록 흐름 (API는 가짜 클라이언트) — 등록·실패 기록·하루 한도·중복 방지·엑셀 잠금·이미지 다운로드."""
import sys

import pytest
from openpyxl import load_workbook

from api.categories import Category
from api.errors import ApiError
from services import templates
from services.automation import config, runner
from services.automation.config import AutomationSettings, CategoryMapping, save_mappings, save_settings
from services.automation.input_sheet import HEADERS, ResultUpdate, write_results, write_template
from services.images.download import download_images
from services.product_builder import ProductDraft
from storage.db import Database
from tests.test_product import write_png

PNG = None


def png_bytes(tmp_path):
    p = tmp_path / "_x.png"
    write_png(p)
    return p.read_bytes()


class FakeClient:
    """커머스API 대역. 등록·상태 변경·목록 조회 호출을 기록한다."""

    def __init__(self, store_codes=None, fail_names=()):
        self.store_codes = dict(store_codes or {})   # 판매자 상품코드 → 원상품번호
        self.fail_names = set(fail_names)
        self.registered: list[dict] = []
        self.status_changes: list[tuple] = []
        self.searches = 0
        self.next_no = 9000

    def post(self, path, json=None, files=None):
        if path.endswith("/product-images/upload"):
            return {"images": [{"url": f"https://shop-phinf/{i}"} for i in range(len(files))]}
        if path == "/external/v1/products/search":
            self.searches += 1
            contents = [{"originProductNo": no, "channelProducts": [{"sellerManagementCode": code}]}
                        for code, no in self.store_codes.items()]
            return {"contents": contents, "last": True}
        if path == "/external/v2/products":
            if json["originProduct"]["name"] in self.fail_names:
                raise ApiError(400, "BAD_REQUEST", "입력한 데이터가 유효하지 않습니다.",
                               invalid_inputs=[{"name": "originProduct.salePrice", "message": "범위 오류"}])
            self.next_no += 1
            self.registered.append(json)
            return {"originProductNo": self.next_no, "smartstoreChannelProductNo": self.next_no + 1}
        raise AssertionError(path)

    def put(self, path, json=None):
        self.status_changes.append((path, json))


class FakeServices:
    def __init__(self, db, client):
        self.db, self._client = db, client

    def client(self):
        return self._client


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTSTORE_DATA_DIR", str(tmp_path / "data"))
    db = Database()
    db.replace_categories([Category("50000803", "티셔츠", "패션의류>여성의류>티셔츠", True)])
    db.save_template("as", "기본", templates.extract("as", ProductDraft(as_phone="02-123-4567")))
    db.set_default_template("as", templates.list_templates(db, "as")[0].id)
    save_mappings(db, [CategoryMapping("티셔츠", "50000803", margin_rate=30)])
    excel = tmp_path / "상품입력.xlsx"
    save_settings(db, AutomationSettings(work_excel=str(excel), image_root=str(tmp_path / "img"), daily_limit=30))
    write_template(excel, ["티셔츠"])
    yield db, tmp_path, excel
    db.close()


def add_rows(excel, rows):
    wb = load_workbook(excel)
    ws = wb["상품"]
    for r in rows:
        ws.append([r.get(h, "") for h in HEADERS])
    wb.save(excel)


def photos(root, code):
    folder = root / "img" / code / "원본"
    folder.mkdir(parents=True, exist_ok=True)
    write_png(folder / "01.png")
    write_png(folder / "02.png")


def row(code, **kw):
    return {"상품코드": code, "입력방식": "촬영", "원본상품명": f"상품 {code}", "원가": 10000, "재고": 5,
            "분류": "티셔츠", "상태": "대기"} | kw


def sheet(excel):
    ws = load_workbook(excel)["상품"]
    col = {h: i for i, h in enumerate(HEADERS)}
    return {r[col["상품코드"]]: (r[col["상태"]], r[col["네이버상품번호"]], r[col["결과메시지"]])
            for r in ws.iter_rows(min_row=2, values_only=True) if r[0]}


def test_register_success_and_failure_recorded(env):
    db, root, excel = env
    photos(root, "A001")
    photos(root, "A003")
    add_rows(excel, [row("A001", 옵션="색상:블랙,화이트"), row("A002", 원가="abc"), row("A003"), row("A004", 상태="판매중")])
    client = FakeClient()
    report = runner.run_waiting(FakeServices(db, client), dry_run=False)

    assert report.registered_count == 2 and report.count("실패") == 1
    result = sheet(excel)
    assert result["A001"][0] == "등록완료(판매중지)" and result["A001"][1] == 9001
    assert "판매가 13,900원" in result["A001"][2] and "옵션 2개" in result["A001"][2]
    assert result["A002"][0] == "실패" and "원가는 숫자" in result["A002"][2] and result["A002"][1] is None
    assert result["A004"] == ("판매중", None, None)   # 대기가 아닌 행은 그대로

    # 판매자 상품코드 전송, 판매중지 전환
    assert client.registered[0]["originProduct"]["detailAttribute"]["sellerCodeInfo"] == {"sellerManagementCode": "A001"}
    assert client.registered[0]["smartstoreChannelProduct"]["channelProductDisplayStatusType"] == "SUSPENSION"
    assert [j for _, j in client.status_changes] == [{"statusType": "SUSPENSION"}] * 2

    # 기록: run_log, automation_items, products
    log = [(r["action"], r["result"], r["code"]) for r in db.list_run_log(report.run_id)]
    assert ("register", "성공", "A001") in log and ("register", "실패", "A002") in log
    assert db.count_registered_today() == 2
    item = db.get_automation_item("A001")
    assert (item["status"], item["origin_product_no"], item["excel_synced"]) == ("등록완료(판매중지)", 9001, 1)
    assert len(db.list_products()) == 2

    # 다시 실행해도 다시 등록하지 않음
    again = runner.run_waiting(FakeServices(db, client), dry_run=False)
    assert again.prepared == [] and len(client.registered) == 2


def test_daily_limit_keeps_rest_waiting(env):
    db, root, excel = env
    s = config.load_settings(db)
    s.daily_limit = 2
    save_settings(db, s)
    for code in ("A001", "A002", "A003"):
        photos(root, code)
    add_rows(excel, [row("A001"), row("A002"), row("A003")])
    client = FakeClient()
    report = runner.run_waiting(FakeServices(db, client), dry_run=False)
    assert report.registered_count == 2
    result = sheet(excel)
    assert result["A003"][0] == "대기" and "하루 최대 등록 수(2건)" in result["A003"][2]

    # 같은 날 다시 실행: 한도가 이미 찼으므로 등록하지 않고 대기 유지
    report = runner.run_waiting(FakeServices(db, client), dry_run=False)
    assert report.registered_count == 0 and sheet(excel)["A003"][0] == "대기" and len(client.registered) == 2


def test_store_duplicate_seller_code(env):
    db, root, excel = env
    photos(root, "A001")
    photos(root, "A002")
    add_rows(excel, [row("A001"), row("A002")])
    db.upsert_automation_item("A002", "처리중", message="등록 중", excel_synced=False)  # 지난 실행이 등록 직후 중단
    client = FakeClient(store_codes={"A001": 111, "A002": 222})
    report = runner.run_waiting(FakeServices(db, client), dry_run=False)
    result = sheet(excel)
    assert result["A001"][0] == "실패" and "이미 있습니다" in result["A001"][2] and result["A001"][1] == 111
    assert result["A002"] [0] == "등록완료(판매중지)" and result["A002"][1] == 222 and "확인해 기록" in result["A002"][2]
    assert client.registered == [] and client.searches == 1   # 목록은 한 번만 조회


def test_api_failure_does_not_stop_others(env):
    db, root, excel = env
    photos(root, "A001")
    photos(root, "A002")
    add_rows(excel, [row("A001", 원본상품명="거절될 상품"), row("A002")])
    report = runner.run_waiting(FakeServices(db, FakeClient(fail_names={"거절될 상품"})), dry_run=False)
    result = sheet(excel)
    assert result["A001"][0] == "실패" and "originProduct.salePrice: 범위 오류" in result["A001"][2]
    assert result["A002"][0] == "등록완료(판매중지)"
    assert report.registered_count == 1


def test_live_publish_registers_on_sale(env):
    db, root, excel = env
    s = config.load_settings(db)
    s.live_publish = True
    save_settings(db, s)
    photos(root, "A001")
    add_rows(excel, [row("A001")])
    client = FakeClient()
    runner.run_waiting(FakeServices(db, client), dry_run=False)
    assert sheet(excel)["A001"][0] == "판매중"
    assert client.status_changes == []
    assert client.registered[0]["smartstoreChannelProduct"]["channelProductDisplayStatusType"] == "ON"


def test_excel_locked_results_kept_in_db_then_synced(env, monkeypatch):
    db, root, excel = env
    photos(root, "A001")
    add_rows(excel, [row("A001"), row("A002", 원가="abc")])
    client = FakeClient()
    real_write = runner.write_results
    monkeypatch.setattr(runner, "write_results", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")))
    report = runner.run_waiting(FakeServices(db, client), dry_run=False)
    assert not report.excel_written and report.pending_sync == 2 and "다음 실행 때" in runner.format_report(report)
    assert sheet(excel)["A001"][0] == "대기"                      # 엑셀은 아직 그대로
    assert db.get_automation_item("A001")["excel_synced"] == 0

    # 엑셀이 계속 열려 있어도 다시 등록하지 않는다
    report = runner.run_waiting(FakeServices(db, client), dry_run=False)
    assert len(client.registered) == 1 and report.prepared == []

    # 엑셀을 닫은 뒤 실행하면 보관한 결과를 반영
    monkeypatch.setattr(runner, "write_results", real_write)
    report = runner.run_waiting(FakeServices(db, client), dry_run=False)
    assert report.synced_previous == 2
    result = sheet(excel)
    assert result["A001"][:2] == ("등록완료(판매중지)", 9001) and result["A002"][0] == "실패"
    assert db.unsynced_automation_items() == [] and len(client.registered) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 파일 잠금")
def test_write_results_real_file_lock(env):
    """엑셀처럼 파일을 독점으로 연 상태에서는 PermissionError(호출 쪽에서 DB 보관)."""
    import ctypes
    db, root, excel = env
    add_rows(excel, [row("A001")])
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(str(excel), 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, 공유 안 함
    try:
        with pytest.raises(PermissionError):
            write_results(excel, {"A001": ResultUpdate("실패", message="x")})
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    write_results(excel, {"A001": ResultUpdate("실패", message="x")})
    assert sheet(excel)["A001"][0] == "실패"
    assert not list(excel.parent.glob("~*.saving.xlsx"))


def test_write_results_only_touches_app_columns(env):
    db, root, excel = env
    add_rows(excel, [row("A001", **{"상품명(최종)": "사람이 쓴 이름"}), row("A002")])
    wb = load_workbook(excel)       # 사용자가 행 순서를 바꾼 상황
    ws = wb["상품"]
    a1 = [c.value for c in ws[2]]
    a2 = [c.value for c in ws[3]]
    for i, v in enumerate(a2, start=1):
        ws.cell(2, i).value = v
    for i, v in enumerate(a1, start=1):
        ws.cell(3, i).value = v
    wb.save(excel)
    write_results(excel, {"A001": ResultUpdate("등록완료(판매중지)", 123, "ok", row_no=2)})
    ws = load_workbook(excel)["상품"]
    col = {h: i + 1 for i, h in enumerate(HEADERS)}
    assert ws.cell(3, 1).value == "A001" and ws.cell(3, col["상태"]).value == "등록완료(판매중지)"
    assert ws.cell(3, col["상품명(최종)"]).value == "사람이 쓴 이름"
    assert ws.cell(2, col["상태"]).value == "대기"
    assert len(ws.data_validations.dataValidation) == 4   # 드롭다운 유지


# --- 이미지 다운로드 ---
class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        for i in range(0, len(self.data), size):
            yield self.data[i:i + size]


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.responses[url]


def test_download_images(tmp_path):
    png = png_bytes(tmp_path)
    session = FakeSession({"https://a/1": FakeResponse(png), "https://a/2": FakeResponse(b"<html>"),
                           "https://a/3": FakeResponse(b"", 404), "https://a/4": FakeResponse(b"\xff\xd8\xff" + b"0")})
    r = download_images(tmp_path / "img", "A001", ["https://a/1", "https://a/2", "https://a/3", "https://a/4"], session)
    assert [p.name for p in r.saved] == ["01.png", "04.jpg"]
    assert len(r.errors) == 2 and "JPG" in r.errors[0] and "404" in r.errors[1]
    again = download_images(tmp_path / "img", "A001", ["https://a/1"], session)   # 이미 있으면 받지 않음
    assert again.skipped and len(session.calls) == 4


def test_register_downloads_url_images(env, monkeypatch):
    db, root, excel = env
    png = png_bytes(root)
    session = FakeSession({"https://shop/a.png": FakeResponse(png), "https://shop/b.png": FakeResponse(png)})
    monkeypatch.setattr(runner, "download_images", lambda r, c, urls: download_images(r, c, urls, session))
    add_rows(excel, [row("A001", 입력방식="도매몰", 이미지URL="https://shop/a.png|https://shop/b.png"),
                     row("A002", 입력방식="도매몰", 이미지URL="https://shop/none.png")])
    session.responses["https://shop/none.png"] = FakeResponse(b"", 404)
    client = FakeClient()
    runner.run_waiting(FakeServices(db, client), dry_run=False)
    result = sheet(excel)
    assert result["A001"][0] == "등록완료(판매중지)" and "사진 2장" in result["A001"][2]
    assert (root / "img" / "A001" / "원본" / "01.png").exists()
    assert result["A002"][0] == "실패" and "이미지 다운로드 실패" in result["A002"][2]


def test_cli_run_registers_with_mock(env, monkeypatch, capsys):
    import cli
    db, root, excel = env
    photos(root, "A001")
    add_rows(excel, [row("A001")])
    client = FakeClient()
    monkeypatch.setattr(cli, "AppServices", lambda d: FakeServices(d, client))
    assert cli.main(["run"]) == 0
    out = capsys.readouterr().out
    assert "(1/1) A001 처리 중" in out and "등록 1건 / 실패 0건" in out and "작업 엑셀에 결과를 기록" in out
    assert sheet(excel)["A001"][0] == "등록완료(판매중지)"


def test_fixed_price_as_is_photos_and_image_only_template(env, monkeypatch):
    """원고로 완성된 상품: 판매가 직접 지정(원가 없음), 사진 그대로, 상세는 완성 이미지만."""
    db, root, excel = env
    s = config.load_settings(db)
    s.remove_background = True
    config.save_settings(db, s)
    folder = root / "img" / "L001" / "원본"
    folder.mkdir(parents=True)
    from PIL import Image
    Image.new("RGB", (1000, 1000), (30, 40, 90)).save(folder / "00_대표.jpg")
    Image.new("RGB", (1000, 1000), (200, 190, 170)).save(folder / "01_추가.jpg")
    Image.new("RGB", (860, 1600), (240, 240, 240)).save(folder / "90_detail_01.jpg")
    seen = []
    from services.images import processing
    real = processing.process_product
    monkeypatch.setattr(runner, "process_product", lambda root_, code, options, remover=None:
                        seen.append(options) or real(root_, code, options, remover=remover))
    add_rows(excel, [row("L001", 원가="", 판매가="18,000", 사진보정="그대로", 상세템플릿="이미지만",
                         옵션="색상:네이비,아이보리 / 사이즈:FREE", 재고=10)])
    client = FakeClient()
    report = runner.run_waiting(FakeServices(db, client), dry_run=False)
    assert report.registered_count == 1, runner.format_report(report)
    origin = client.registered[0]["originProduct"]
    assert origin["salePrice"] == 18000
    assert not seen[-1].enhance and not seen[-1].remove_background          # 완성 사진은 보정·배경 제거 안 함
    detail = origin["detailContent"]
    assert detail.count("<img") == 1 and "detail" not in detail.lower().split("src=")[0]
    assert "엑셀 판매가 18,000원" in runner.format_report(report)
    edited = Image.open(root / "img" / "L001" / "보정" / "00_대표.jpg").convert("RGB")
    assert max(abs(a - b) for a, b in zip(edited.getpixel((500, 500)), (30, 40, 90))) <= 3   # 색 그대로


def test_new_column_validation(env):
    db, root, excel = env
    photos(root, "L002")
    photos(root, "L003")
    add_rows(excel, [row("L002", 원가="", 판매가="abc"), row("L003", 사진보정="반쯤", 상세템플릿="이미지만"),
                     row("L004", 원가="")])
    report = runner.run_waiting(FakeServices(db, FakeClient()), dry_run=True)
    errors = {p.code: " ".join(p.errors) for p in report.prepared}
    assert "판매가는 0보다 큰 숫자" in errors["L002"]
    assert "사진보정은 자동 / 그대로" in errors["L003"] and "상세 이미지가 필요합니다" in errors["L003"]
    assert "필수값이 비어 있습니다: 원가" in errors["L004"]                  # 판매가 없으면 원가는 필수
