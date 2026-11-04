import os

import pytest
from openpyxl import load_workbook

from api.categories import Category
from services import templates
from services.automation import config
from services.automation.config import AutomationSettings, CategoryMapping, save_mappings, save_settings
from services.automation.input_sheet import HEADERS, STATUSES, read_items, write_template
from services.automation.naming import find_banned, render_name, unknown_tokens
from services.automation.parsing import parse_measurements, parse_options, parse_stock
from services.automation.pipeline import build_context, prepare
from services.automation.pricing import calculate_price
from services.automation.run_lock import AlreadyRunningError, RunLock
from services.images import product_images
from services.product_builder import ProductDraft
from storage.db import Database
from tests.test_product import write_png


# --- 가격 계산 ---
@pytest.mark.parametrize("cost, margin, fee, unit, ending, expected", [
    (10000, 30, 5.5, 100, 900, 13900),     # 13,757 → 13,800 → 13,900
    (10000, 30, 5.5, 100, 0, 13800),       # 끝자리 규칙 없음
    (9000, 30, 5.5, 100, 900, 12900),      # 12,381 → 12,400 → 12,900
    (10600, 30, 5.5, 100, 900, 14900),     # 14,582 → 14,600 → 14,900
    (11300, 30, 5.5, 100, 900, 15900),     # 15,545 → 15,600 → 15,900
    (11000, 30, 5.5, 100, 900, 15900),     # 15,132 → 15,200 → 15,900
    (6900, 30, 0, 100, 900, 9900),         # 8,970 → 9,000 → 9,900
    (13950, 0, 0, 10, 900, 14900),         # 끝자리 900을 이미 넘으면 다음 천 원대
    (100, 0, 0, 10, 0, 100),
    (10000, 30, 5.5, 1000, 0, 14000),
])
def test_calculate_price(cost, margin, fee, unit, ending, expected):
    assert calculate_price(cost, margin, fee, unit, ending).price == expected


def test_calculate_price_exact_ending_and_steps():
    r = calculate_price(13900, 0, 0, 100, 900)
    assert r.price == 13900  # 이미 끝자리가 맞으면 그대로
    r = calculate_price(10000, 30, 5.5, 100, 900)
    assert len(r.steps) == 3 and "13,757" in r.steps[0] and r.explain().endswith("13,900원")


@pytest.mark.parametrize("kwargs", [dict(cost=0), dict(fee_rate=100), dict(round_unit=15), dict(price_ending=905)])
def test_calculate_price_invalid(kwargs):
    args = dict(cost=1000, margin_rate=30, fee_rate=5, round_unit=100, price_ending=900) | kwargs
    with pytest.raises(ValueError):
        calculate_price(**args)


# --- 옵션·재고·실측 파싱 ---
def test_parse_options():
    r = parse_options("색상:베이지, 블랙 / 사이즈 : S,M,L")
    assert r.errors == [] and [(g.name, g.values) for g in r.groups] == [("색상", ["베이지", "블랙"]),
                                                                          ("사이즈", ["S", "M", "L"])]
    assert parse_options("").groups == []
    assert parse_options("색상 베이지").errors
    assert parse_options("색상:").errors
    assert any("중복" in e for e in parse_options("색상:a / 색상:b").errors)
    assert any("최대" in e for e in parse_options("a:1 / b:1 / c:1 / d:1").errors)


def test_parse_stock_variants():
    groups = parse_options("색상:베이지,블랙 / 사이즈:S,M").groups
    same = parse_stock("5", groups)
    assert [r.stock for r in same.rows] == [5, 5, 5, 5] and not same.warnings

    per = parse_stock("베이지-S:3, 블랙 - M : 2", groups)
    assert per.errors == []
    assert {"-".join(r.values): r.stock for r in per.rows} == {"베이지-S": 3, "베이지-M": 0, "블랙-S": 0, "블랙-M": 2}
    assert per.warnings and "2개" in per.warnings[0]

    assert any("옵션에 없습니다" in e for e in parse_stock("레드-S:1", groups).errors)
    assert any("숫자" in e for e in parse_stock("베이지-S:많음", groups).errors)
    assert parse_stock("", groups).errors

    assert parse_stock("1,200", []).quantity == 1200
    assert parse_stock("베이지:3", []).errors


def test_parse_measurements():
    r = parse_measurements("S:어깨40,가슴50,총장60.5cm / M:어깨42, 가슴52")
    assert r.errors == []
    assert r.sizes == {"S": [("어깨", "40"), ("가슴", "50"), ("총장", "60.5")], "M": [("어깨", "42"), ("가슴", "52")]}
    assert r.columns() == ["어깨", "가슴", "총장"]
    assert parse_measurements("S 어깨40").errors
    assert parse_measurements("S:어깨").errors


# --- 상품명 규칙·금지어 ---
def test_naming():
    rule = "{브랜드} {핵심키워드} {원본상품명}"
    assert render_name(rule, {"브랜드": "데일리", "핵심키워드": "", "원본상품명": " 반팔  티 "}) == "데일리 반팔 티"
    assert unknown_tokens("{브랜드} {색상}") == ["색상"]
    with pytest.raises(ValueError, match="색상"):
        render_name("{색상}", {})
    assert find_banned("정품 무료배송 최저가", ["최저가", "1위", "정품"]) == ["최저가", "정품"]
    assert find_banned("Best Item", ["best"]) == ["best"]


# --- 사진 폴더 규칙 ---
def test_product_images(tmp_path):
    original = tmp_path / "A001" / "원본"
    original.mkdir(parents=True)
    for name in ["b.png", "a.png", "x_detail.png", "c.txt"] + [f"z{i:02}.png" for i in range(10)]:
        (original / name).write_bytes(b"x")
    s = product_images(tmp_path, "A001")
    assert s.representative.name == "a.png" and len(s.optional) == 9 and len(s.skipped) == 2
    assert [p.name for p in s.detail] == ["x_detail.png"]
    (tmp_path / "A001" / "보정").mkdir()
    (tmp_path / "A001" / "보정" / "only.png").write_bytes(b"x")
    assert product_images(tmp_path, "A001").representative.name == "only.png"
    assert not product_images(tmp_path, "NONE").found


# --- 잠금 ---
def test_run_lock(tmp_path):
    path = tmp_path / "run.lock"
    with RunLock(path):
        assert path.exists()
        with pytest.raises(AlreadyRunningError):
            RunLock(path).acquire()
    assert not path.exists()
    path.write_text("99999999 run")  # 죽은 프로세스가 남긴 잠금
    with RunLock(path):
        assert path.read_text().split()[0] == str(os.getpid())


# --- 설정 ---
def test_settings_and_mappings(tmp_path):
    db = Database(tmp_path / "t.db")
    assert config.load_settings(db) == AutomationSettings()
    save_settings(db, AutomationSettings(fee_rate=3.3, banned_words=["a"], daily_limit=5))
    assert config.load_settings(db).fee_rate == 3.3
    save_mappings(db, [CategoryMapping("티셔츠", "1", margin_rate=40)])
    assert config.load_mappings(db)["티셔츠"].margin_rate == 40
    with pytest.raises(ValueError, match="중복"):
        save_mappings(db, [CategoryMapping("a"), CategoryMapping("a")])
    with pytest.raises(ValueError, match="비어"):
        save_mappings(db, [CategoryMapping(" ")])
    assert config.parse_banned_words("최저가, 정품\n1위,,최저가") == ["최저가", "정품", "1위"]
    db.close()


# --- 작업 엑셀 + 가공 파이프라인 ---
@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    db.replace_categories([Category("50000803", "티셔츠", "패션의류>여성의류>티셔츠", True),
                           Category("50000167", "여성의류", "패션의류>여성의류", False)])
    db.save_template("as", "본사", templates.extract("as", ProductDraft(as_phone="02-123-4567")))
    db.save_template("delivery", "유료", templates.extract("delivery", ProductDraft(delivery_fee_type="PAID",
                                                                                 base_fee=2500)))
    save_mappings(db, [
        CategoryMapping("티셔츠", "50000803", as_template="본사", delivery_template="유료", margin_rate=30,
                        name_rule="{브랜드} {핵심키워드} {원본상품명}", brand="데일리", keyword="여름"),
        CategoryMapping("상위분류", "50000167", as_template="본사"),
        CategoryMapping("템플릿없음", "50000803", as_template="없는템플릿"),
    ])
    excel = tmp_path / "상품입력.xlsx"
    save_settings(db, AutomationSettings(banned_words=["최저가"], work_excel=str(excel)))
    write_template(excel, ["티셔츠", "상위분류", "템플릿없음"])
    yield db, tmp_path, excel
    db.close()


def add_rows(excel, rows):
    wb = load_workbook(excel)
    ws = wb["상품"]
    for r in rows:
        ws.append([r.get(h, "") for h in HEADERS])
    wb.save(excel)


def photos(root, code, names=("01.png", "02.png")):
    folder = root / "상품이미지" / code / "원본"
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        write_png(folder / n)


def row(**kw):
    base = {"상품코드": "A001", "입력방식": "촬영", "원본상품명": "코튼 티", "원가": 10000, "재고": 10,
            "분류": "티셔츠", "상태": "대기"}
    return base | kw


def test_template_structure(tmp_path):
    path = tmp_path / "t.xlsx"
    write_template(path, ["티셔츠", "바지"])
    wb = load_workbook(path)
    assert wb.sheetnames == ["상품", "목록", "안내"] and wb["목록"].sheet_state == "hidden"
    assert [c.value for c in wb["상품"][1]] == HEADERS
    assert [c.value for c in wb["목록"]["A"]][1:] == ["티셔츠", "바지"]
    formulas = {dv.formula1 for dv in wb["상품"].data_validations.dataValidation}
    assert '"' + ",".join(STATUSES) + '"' in formulas and "='목록'!$A$2:$A$200" in formulas


def test_read_items_skips_blank_rows(env):
    db, root, excel = env
    add_rows(excel, [row(), {}, row(상품코드="A002", 원가="12,000")])
    items = read_items(excel)
    assert [(i.row_no, i.code) for i in items] == [(2, "A001"), (4, "A002")]
    assert items[1].get("원가") == "12,000"


def test_prepare_success_with_options(env):
    db, root, excel = env
    photos(root, "A001", ("01.png", "02.png", "03_detail.png"))
    add_rows(excel, [row(옵션="색상:베이지,블랙", 재고="베이지:3, 블랙:4", 키워드="", 실측="S:어깨40")])
    items = read_items(excel)
    p = prepare(items[0], build_context(db, items))
    assert p.ok, p.errors
    d = p.draft
    assert d.name == "데일리 여름 코튼 티" and p.name_source == "규칙"
    assert d.sale_price == 13900 and d.effective_stock() == 7
    assert (d.delivery_fee_type, d.base_fee, d.as_phone) == ("PAID", 2500, "02-123-4567")
    assert d.category_name == "패션의류>여성의류>티셔츠" and d.test_mode is True
    assert d.representative_image.endswith("01.png") and len(d.optional_images) == 1
    assert len(p.images.detail) == 1


def test_prepare_keeps_human_name_and_url_images(env):
    db, root, excel = env
    add_rows(excel, [row(입력방식="도매몰", 이미지URL="https://a/1.jpg | https://a/2.jpg",
                         **{"상품명(최종)": "내가 정한 이름"})])
    items = read_items(excel)
    p = prepare(items[0], build_context(db, items))
    assert p.ok, p.errors
    assert p.draft.name == "내가 정한 이름" and p.name_source == "입력"
    assert p.pending_urls == ["https://a/1.jpg", "https://a/2.jpg"] and p.draft.representative_image == ""


def test_prepare_live_publish_setting(env):
    db, root, excel = env
    s = config.load_settings(db)
    s.live_publish = True
    save_settings(db, s)
    photos(root, "A001")
    add_rows(excel, [row()])
    items = read_items(excel)
    assert prepare(items[0], build_context(db, items)).draft.test_mode is False


@pytest.mark.parametrize("override, expected", [
    ({"원본상품명": ""}, "필수값"),
    ({"입력방식": "직구"}, "입력방식"),
    ({"원가": "만원"}, "원가는 숫자"),
    ({"분류": "없는분류"}, "분류 매핑에 없습니다"),
    ({"분류": "상위분류"}, "최하위 카테고리가 아닙니다"),
    ({"분류": "템플릿없음"}, "템플릿 '없는템플릿'"),
    ({"상품명(최종)": "최저가 티셔츠"}, "금지어"),
    ({"옵션": "색상 베이지"}, "옵션 형식"),
    ({"재고": "많음"}, "숫자"),
    ({"실측": "S 어깨40"}, "실측 형식"),
    ({"이미지URL": "ftp://a"}, "http"),
    ({"상품코드": "NOPIC"}, "사진이 없습니다"),
])
def test_prepare_validation_errors(env, override, expected):
    db, root, excel = env
    photos(root, "A001")
    add_rows(excel, [row(**override)])
    items = read_items(excel)
    p = prepare(items[0], build_context(db, items))
    assert not p.ok
    assert any(expected in e for e in p.errors), p.errors


def test_prepare_duplicate_codes(env):
    db, root, excel = env
    photos(root, "A001")
    photos(root, "A002")
    db.upsert_automation_item("A002", "등록완료(판매중지)", origin_product_no=123)
    add_rows(excel, [row(), row(원본상품명="또"), row(상품코드="A002")])
    items = read_items(excel)
    ctx = build_context(db, items)
    results = [prepare(i, ctx) for i in items]
    assert all("2번" in " ".join(r.errors) for r in results[:2])
    assert any("이미 등록된 상품코드" in e and "123" in e for e in results[2].errors)


# --- CLI ---
def test_cli_dry_run_end_to_end(env, monkeypatch, capsys):
    import cli
    db, root, excel = env
    monkeypatch.setenv("SMARTSTORE_DATA_DIR", str(root / "data"))
    data_db = Database()  # CLI가 여는 DB(SMARTSTORE_DATA_DIR)에 설정 복사
    for kind in ("as", "delivery"):
        for t in templates.list_templates(db, kind):
            data_db.save_template(kind, t.name, t.data)
    data_db.replace_categories([db.get_category("50000803")])
    save_mappings(data_db, list(config.load_mappings(db).values()))
    save_settings(data_db, config.load_settings(db))
    data_db.close()

    photos(root, "A001")
    add_rows(excel, [row(), row(상품코드="A002", 원가="abc"), row(상품코드="A003", 상태="판매중")])
    code = cli.main(["run", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 1
    assert "A001  [통과]  데일리 여름 코튼 티" in out and "판매가 13,900원" in out
    assert "A002  [실패]" in out and "A003" not in out
    assert "통과 1건 / 실패 1건" in out
    assert load_workbook(excel)["상품"]["P2"].value == "대기"  # dry-run은 엑셀을 바꾸지 않음

    check = Database()
    actions = [(r["action"], r["result"], r["code"]) for r in check.list_run_log()]
    check.close()
    assert ("dry_run", "통과", "A001") in actions and ("dry_run", "실패", "A002") in actions
    assert not (root / "data" / "run.lock").exists()

    # 인증 정보가 없으면(테스트는 keyring 대역) 실제 등록은 시작하지 않고 안내만
    assert cli.main(["run"]) == 2 and "애플리케이션 ID와 시크릿" in capsys.readouterr().out
    assert cli.main(["publish", "--codes", "A001"]) == 1   # 검수 대기 상품이 아니므로 실패(API 호출 없음)


def test_cli_init_template_does_not_overwrite(env, monkeypatch, capsys):
    import cli
    db, root, excel = env
    monkeypatch.setenv("SMARTSTORE_DATA_DIR", str(root / "data"))
    out = root / "new.xlsx"
    assert cli.main(["init-template", "--out", str(out)]) == 0 and out.exists()
    out.write_bytes(b"user data")
    assert cli.main(["init-template", "--out", str(out)]) == 2
    assert out.read_bytes() == b"user data"
