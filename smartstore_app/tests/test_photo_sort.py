"""7단계: QR 카드 출력과 촬영 사진 자동 분리."""
import hashlib
import os
import re
from datetime import datetime

import pytest
from PIL import Image, ImageDraw

from services.automation import config, runner
from services.images.photo_sort import capture_time, read_qr_code, sort_photos
from services.images.qr_cards import QR_PREFIX, make_pdf, parse_payload, render_card
from services.notify.summary import run_summary
from storage.db import Database
from tests.test_automation_register import FakeClient, FakeServices, add_rows, env, row, sheet  # noqa: F401

OLD = 1_700_000_000   # 파일 수정 시각을 과거로 (동기화 대기 판정 피하기)


def shot(path, when, color=(120, 140, 160), qr_code=None, qr_text=None, size=(1200, 900)):
    """폰 사진 흉내: EXIF 촬영 시각 + (선택) 바닥에 놓인 QR 카드."""
    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, size[1] * 0.7, size[0], size[1]), fill=(150, 120, 90))    # 책상
    if qr_code or qr_text:
        if qr_text:
            import qrcode
            card = qrcode.make(qr_text).convert("RGB").resize((420, 420))
        else:
            card = render_card(qr_code, "샘플 상품", (760, 540))
        card = card.rotate(4, expand=True, fillcolor=color)
        img.paste(card, ((size[0] - card.width) // 2, (size[1] - card.height) // 2))
    exif = Image.Exif()
    exif[306] = when.strftime("%Y:%m:%d %H:%M:%S")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=90, exif=exif)
    os.utime(path, (OLD, OLD))
    return path


def t(hh, mm):
    return datetime(2026, 11, 2, hh, mm)


def digest(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob("*") if p.is_file()}


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


# --- QR 카드 ---
def test_qr_payload_and_card_roundtrip(tmp_path):
    assert parse_payload(f"{QR_PREFIX}A001") == "A001"
    assert parse_payload("https://brand.example/tag") is None and parse_payload(QR_PREFIX) is None
    path = shot(tmp_path / "card.jpg", t(10, 0), qr_code="A001")
    assert read_qr_code(path) == "A001"
    tag = shot(tmp_path / "tag.jpg", t(10, 0), qr_text="https://brand.example/tag")
    assert read_qr_code(tag) is None                                           # 다른 QR은 무시
    assert read_qr_code(shot(tmp_path / "plain.jpg", t(10, 0))) is None


def test_small_qr_card_read_at_higher_resolution(tmp_path):
    import qrcode
    img = Image.new("L", (4000, 3000), 130)                     # 고해상도 폰 사진 속 작게 찍힌 카드
    img.paste(qrcode.make(f"{QR_PREFIX}A007", box_size=3, border=4).convert("L"), (1800, 1300))
    img.save(tmp_path / "far.png")
    assert read_qr_code(tmp_path / "far.png") == "A007"


def test_make_pdf_pages(tmp_path):
    out = make_pdf([(f"A{i:03}", f"상품 {i}") for i in range(1, 10)], tmp_path / "cards.pdf")
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and len(re.findall(rb"/Type\s*/Page\b", data)) == 2   # 9장 → A4 2장
    with pytest.raises(ValueError):
        make_pdf([], tmp_path / "none.pdf")


def test_capture_time_from_exif_and_fallback(tmp_path):
    assert capture_time(shot(tmp_path / "a.jpg", t(9, 41))) == t(9, 41)
    raw = tmp_path / "noexif.png"
    Image.new("RGB", (10, 10)).save(raw)
    os.utime(raw, (OLD, OLD))
    assert capture_time(raw) == datetime.fromtimestamp(OLD)


# --- 분리 ---
def test_sort_sequence_and_originals_untouched(tmp_path, db):
    inbox, root = tmp_path / "inbox", tmp_path / "상품이미지"
    shot(inbox / "IMG_0001.jpg", t(9, 0))                       # 카드 전 → 미분류
    shot(inbox / "IMG_0002.jpg", t(10, 0), qr_code="A001")
    shot(inbox / "IMG_0003.jpg", t(10, 1), (200, 50, 50))
    shot(inbox / "IMG_0004.jpg", t(10, 2), (50, 200, 50))
    shot(inbox / "sub" / "IMG_0005.jpg", t(10, 5), qr_code="A002")   # 하위 폴더도 읽음
    shot(inbox / "sub" / "IMG_0006.jpg", t(10, 6), (50, 50, 200))
    shot(inbox / "IMG_0007.jpg", t(14, 30))                     # 카드 뒤 3시간 넘음 → 미분류
    before = digest(inbox)

    report = sort_photos(db, inbox, root, max_gap_hours=3, known_codes={"A001", "A002"})
    assert {c: [p.name for p in f] for c, f in report.assigned.items()} == {
        "A001": ["IMG_0003.jpg", "IMG_0004.jpg"], "A002": ["IMG_0006.jpg"]}
    assert [c for c, _ in report.qr_cards] == ["A001", "A002"]
    assert sorted(p.name for p in report.unsorted) == ["IMG_0001.jpg", "IMG_0007.jpg"]
    assert sorted(p.name for p in (root / "A001" / "원본").iterdir()) == ["IMG_0003.jpg", "IMG_0004.jpg"]  # 카드 사진 없음
    assert digest(inbox) == before                              # 동기화 폴더 사진은 그대로
    assert report.warnings == []

    again = sort_photos(db, inbox, root)
    assert again.copied == 0 and not again.qr_cards and again.already == 7


def test_sort_continues_across_runs_and_out_of_order_sync(tmp_path, db):
    inbox, root = tmp_path / "inbox", tmp_path / "img"
    shot(inbox / "IMG_0100.jpg", t(11, 0), qr_code="A003")
    sort_photos(db, inbox, root)
    shot(inbox / "IMG_0101.jpg", t(11, 10))                     # 나중에 동기화된 사진
    shot(inbox / "IMG_0050.jpg", t(11, 20), qr_code="A004")
    shot(inbox / "IMG_0300.jpg", t(11, 15))                     # 파일 이름은 늦지만 A004 카드보다 먼저 찍음
    shot(inbox / "IMG_0010.jpg", t(11, 25))                     # 파일 이름은 빠르지만 A004 카드 뒤에 찍음
    report = sort_photos(db, inbox, root)
    assert {c: sorted(p.name for p in f) for c, f in report.assigned.items()} == {
        "A003": ["IMG_0101.jpg", "IMG_0300.jpg"], "A004": ["IMG_0010.jpg"]}


def test_sort_other_qr_unknown_code_deferred_and_collisions(tmp_path, db):
    inbox, root = tmp_path / "inbox", tmp_path / "img"
    shot(inbox / "c.jpg", t(12, 0), qr_code="ZZ99")
    shot(inbox / "tag.jpg", t(12, 1), qr_text="https://brand.example/tag")   # 옷 태그 QR = 상품 사진
    fresh = shot(inbox / "fresh.jpg", t(12, 2))
    os.utime(fresh, None)                                        # 방금 생긴 파일 → 다음에
    (root / "ZZ99" / "원본").mkdir(parents=True)
    Image.new("RGB", (5, 5)).save(root / "ZZ99" / "원본" / "tag.jpg")          # 같은 이름, 다른 파일
    report = sort_photos(db, inbox, root, known_codes={"A001"})
    assert [p.name for p in report.assigned["ZZ99"]] == ["tag_1.jpg"]
    assert report.deferred == 1 and any("ZZ99" in w and "엑셀에 없는" in w for w in report.warnings)
    os.utime(fresh, (OLD, OLD))
    assert [p.name for p in sort_photos(db, inbox, root).assigned["ZZ99"]] == ["fresh.jpg"]


def test_sort_ignores_output_inside_inbox_and_missing_inbox(tmp_path, db):
    inbox = tmp_path / "inbox"
    shot(inbox / "card.jpg", t(13, 0), qr_code="A005")
    shot(inbox / "p.jpg", t(13, 1))
    root = inbox / "상품이미지"                                   # 사진 폴더가 동기화 폴더 안
    sort_photos(db, inbox, root)
    report = sort_photos(db, inbox, root)
    assert report.copied == 0 and report.already == 2           # 복사본을 다시 읽지 않음
    with pytest.raises(FileNotFoundError):
        sort_photos(db, tmp_path / "없음", root)


def test_heic_photo_is_sorted(tmp_path, db):
    pillow_heif = pytest.importorskip("pillow_heif")
    inbox, root = tmp_path / "inbox", tmp_path / "img"
    shot(inbox / "IMG_1.jpg", t(15, 0), qr_code="A006")
    heic = inbox / "IMG_2.HEIC"
    try:
        pillow_heif.from_pillow(Image.new("RGB", (640, 480), (90, 30, 30))).save(heic)
    except Exception as e:
        pytest.skip(f"HEIC 인코더 없음: {e}")
    os.utime(heic, (OLD, OLD))
    report = sort_photos(db, inbox, root, max_gap_hours=48 * 365 * 10)   # HEIC은 EXIF 없이 수정 시각 사용
    assert "A006" in report.assigned or heic.name in [p.name for p in report.unsorted]


# --- 자동화 흐름 ---
def test_run_sorts_inbox_before_registering(env):
    db, root, excel = env
    inbox = root / "phone"
    shot(inbox / "IMG_1.jpg", t(10, 0), qr_code="A001")
    shot(inbox / "IMG_2.jpg", t(10, 1), (200, 60, 60))
    shot(inbox / "IMG_3.jpg", t(10, 2), (60, 200, 60))
    shot(inbox / "IMG_9.jpg", t(8, 0))                          # 미분류
    s = config.load_settings(db)
    s.photo_inbox = str(inbox)
    config.save_settings(db, s)
    add_rows(excel, [row("A001")])                               # 사진 폴더에는 아직 사진 없음
    client = FakeClient()
    report = runner.run_waiting(FakeServices(db, client), dry_run=False)
    assert sheet(excel)["A001"][0] == "등록완료(판매중지)" and "사진 2장" in sheet(excel)["A001"][2]
    text = runner.format_report(report)
    assert "촬영 사진 분리: 새 사진 4장" in text and "미분류 사진 1장" in text
    assert "미분류 사진 1장 확인 필요" in run_summary(report, 1)

    dry = runner.run_waiting(FakeServices(db, client), dry_run=True)
    assert dry.photo_sort is None                                # dry-run은 분리하지 않음


def test_run_continues_when_inbox_missing(env):
    db, root, excel = env
    s = config.load_settings(db)
    s.photo_inbox = str(root / "없는폴더")
    config.save_settings(db, s)
    report = runner.run_waiting(FakeServices(db, FakeClient()), dry_run=False)
    assert "촬영 사진 분리 실패" in runner.format_report(report)


def test_cli_sort_photos_and_qr_cards(env, capsys):
    import cli
    db, root, excel = env
    inbox = root / "phone"
    shot(inbox / "IMG_1.jpg", t(10, 0), qr_code="A001")
    shot(inbox / "IMG_2.jpg", t(10, 1))
    assert cli.main(["sort-photos"]) == 2                        # 폴더 미설정
    assert cli.main(["sort-photos", "--inbox", str(inbox)]) == 0
    out = capsys.readouterr().out
    assert "A001  1장" in out and "QR 카드 1장" in out

    add_rows(excel, [row("A001"), row("A002", 입력방식="도매몰"), row("A003", 상태="판매중")])
    assert cli.main(["qr-cards", "--out", str(root / "cards.pdf")]) == 0
    assert "QR 카드 1장" in capsys.readouterr().out and (root / "cards.pdf").exists()   # 대기·촬영만
    assert cli.main(["qr-cards", "--codes", "A002,A003", "--out", str(root / "c2.pdf")]) == 0


def test_photo_inbox_page_ui(env, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QFileDialog
    from PySide6.QtGui import QDesktopServices
    from ui.photo_inbox_page import PhotoInboxPage
    app = QApplication.instance() or QApplication([])
    db, root, excel = env
    add_rows(excel, [row("A001"), row("A002"), row("A003", 입력방식="도매몰")])
    page = PhotoInboxPage(FakeServices(db, FakeClient()))
    assert page.cards.rowCount() == 2
    page.cards.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
    out = root / "ui_cards.pdf"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: True))
    page.on_make_pdf()
    assert out.exists() and len(re.findall(rb"/Type\s*/Page\b", out.read_bytes())) == 1
    page.inbox_edit.setText(str(root / "phone"))
    page.gap_spin.setValue(5)
    s = config.load_settings(db)
    assert s.photo_inbox == str(root / "phone") and s.photo_max_gap_hours == 5
    page.deleteLater()
    app.processEvents()
