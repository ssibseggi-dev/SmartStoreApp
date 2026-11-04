"""상품 사진 넣기 — 원고 폴더 불러오기, 이름 규칙, 기존 사진 보관, 화면."""
import hashlib
import os

import pytest
from PIL import Image

from services.images import product_images
from services.images.intake import (
    PhotoPlan, existing_photos, plan_from_manuscript, save_photos, target_names, validate_code, validate_plan,
)


def jpg(path, color=(120, 80, 60), size=(400, 400)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def manuscript(tmp_path):
    """결과물 폴더와 같은 구성의 원고 폴더."""
    m = tmp_path / "결과물"
    for name in ("대표이미지_A_4컬러.jpg", "대표이미지_B_모델컷.jpg"):
        jpg(m / "01_대표이미지" / name)
    jpg(m / "02_썸네일" / "썸네일_광고용.jpg")
    jpg(m / "03_누끼" / "누끼_네이비.jpg")
    for i, name in enumerate(["모델컷", "네이비", "아이보리"], start=1):
        jpg(m / "04_추가이미지" / f"추가{i:02}_{name}.jpg")
    for i in range(1, 4):
        jpg(m / "05_상세페이지" / "분할" / f"상세_{i:02}.jpg", size=(300, 900))
    jpg(m / "05_상세페이지" / "상세페이지_전체.jpg")
    jpg(m / "05_상세페이지" / "_이전버전_v1" / "분할" / "상세_01.jpg")
    (m / "상품등록_원고.md").write_text("원고", encoding="utf-8")
    return m


def test_plan_from_manuscript(manuscript):
    plan = plan_from_manuscript(manuscript)
    assert plan.representative.name == "대표이미지_A_4컬러.jpg"                 # 추천 대표(A)
    assert [p.name for p in plan.optional] == ["추가01_모델컷.jpg", "추가02_네이비.jpg", "추가03_아이보리.jpg"]
    assert [p.name for p in plan.detail] == ["상세_01.jpg", "상세_02.jpg", "상세_03.jpg"]   # 분할만, 이전 버전 제외
    assert all("썸네일" not in p.name and "누끼" not in p.name for p in plan.optional)
    with pytest.raises(ValueError, match="사진을 찾지 못했습니다"):
        plan_from_manuscript(manuscript / "02_썸네일")


def test_target_names_follow_folder_rules(manuscript):
    names = [n for _, n in target_names(plan_from_manuscript(manuscript))]
    assert names == ["00_대표_대표이미지_A_4컬러.jpg", "01_모델컷.jpg", "02_네이비.jpg", "03_아이보리.jpg",
                     "90_detail_01.jpg", "90_detail_02.jpg", "90_detail_03.jpg"]


def test_save_photos_copies_and_feeds_registration(tmp_path, manuscript):
    root = tmp_path / "상품이미지"
    plan = plan_from_manuscript(manuscript)
    before = {p: sha(p) for p in [plan.representative, *plan.optional, *plan.detail]}
    result = save_photos(root, "L002", plan)
    assert result.folder == root / "L002" / "원본" and len(result.saved) == 7 and result.archived is None
    assert all(sha(p) == h for p, h in before.items())                          # 원래 파일은 그대로 (복사)
    images = product_images(root, "L002")                                       # 등록 흐름이 읽는 구성
    assert images.representative.name.startswith("00_대표")
    assert [p.name for p in images.optional][:1] == ["01_모델컷.jpg"] and len(images.detail) == 3


def test_save_replaces_but_keeps_old_photos(tmp_path, manuscript):
    root = tmp_path / "상품이미지"
    old = jpg(root / "L002" / "원본" / "옛사진.jpg")
    old_hash = sha(old)
    result = save_photos(root, "L002", PhotoPlan(plan_from_manuscript(manuscript).representative))
    assert result.archived and (result.archived / "옛사진.jpg").exists() and sha(result.archived / "옛사진.jpg") == old_hash
    assert [p.name for p in existing_photos(root, "L002")] == ["00_대표_대표이미지_A_4컬러.jpg"]   # 보관 폴더는 안 읽음
    loose = jpg(root / "L003" / "바로넣은.jpg")                                 # 원본/ 없이 넣었던 사진도 보관
    result = save_photos(root, "L003", PhotoPlan(plan_from_manuscript(manuscript).representative))
    assert not loose.exists() and (result.archived / "바로넣은.jpg").exists()


def test_validation(tmp_path, manuscript):
    assert validate_code("") and validate_code("L 002") and validate_code("../x") and not validate_code("L002")
    plan = plan_from_manuscript(manuscript)
    assert "대표 이미지를 1장" in validate_plan(PhotoPlan(None, plan.optional))[0]
    assert "최대 9장" in " ".join(validate_plan(PhotoPlan(plan.representative, plan.optional * 4)))
    txt = tmp_path / "메모.txt"
    txt.write_text("x", encoding="utf-8")
    assert "사진 파일이 아닙니다" in " ".join(validate_plan(PhotoPlan(plan.representative, [txt])))
    with pytest.raises(ValueError):
        save_photos(tmp_path / "상품이미지", "L 002", plan)


def test_photo_intake_dialog(tmp_path, manuscript, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from services.automation.config import AutomationSettings, save_settings
    from services.automation.input_sheet import write_template
    from storage.db import Database
    from ui.photo_intake_dialog import PhotoIntakeDialog
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    excel = tmp_path / "상품입력.xlsx"
    write_template(excel, ["니트"])
    save_settings(db, AutomationSettings(work_excel=str(excel), image_root=str(tmp_path / "상품이미지")))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))

    dialog = PhotoIntakeDialog(db, code="L002")
    assert dialog.code() == "L002"
    dialog.apply_plan(plan_from_manuscript(manuscript))
    assert dialog.rep_list.count() == 1 and dialog.opt_list.count() == 3 and dialog.detail_list.count() == 3
    dialog.opt_list.setCurrentRow(2)
    dialog.opt_list.move_selected(-2)                                            # 아이보리를 맨 앞으로
    assert dialog.plan().optional[0].name == "추가03_아이보리.jpg"
    assert dialog.rep_list.add_paths([manuscript / "01_대표이미지" / "대표이미지_B_모델컷.jpg"]) == []
    assert dialog.rep_list.count() == 1 and dialog.plan().representative.name == "대표이미지_B_모델컷.jpg"   # 대표는 1장만
    assert dialog.opt_list.add_paths([manuscript / "상품등록_원고.md"])               # 사진 아닌 파일은 거절
    dialog.on_save()
    saved = sorted(p.name for p in (tmp_path / "상품이미지" / "L002" / "원본").iterdir())
    assert saved[0] == "00_대표_대표이미지_B_모델컷.jpg" and saved[1] == "01_아이보리.jpg"
    reopened = PhotoIntakeDialog(db, code="L002")
    assert "저장된 사진 7장" in reopened.existing.text() and reopened.opt_list.count() == 3   # 다시 열면 불러옴
    dialog.deleteLater()
    db.close()
    app.processEvents()


def test_clear_notice_image_button(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from services.automation.config import AutomationSettings, load_settings, save_settings
    from services.connection import AppServices
    from storage.db import Database
    from ui.automation_page import AutomationPage
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    notice = jpg(tmp_path / "공지.jpg")
    save_settings(db, AutomationSettings(work_excel=str(tmp_path / "x.xlsx"), notice_image=str(notice)))
    page = AutomationPage(AppServices(db))
    assert page.notice_clear_btn.isEnabled()
    page.on_clear_notice()
    assert load_settings(db).notice_image == "" and not page.notice_clear_btn.isEnabled()
    assert notice.exists()                                                   # 파일은 지우지 않음
    page.deleteLater()
    db.close()
    app.processEvents()


def test_edit_existing_photos(tmp_path, manuscript):
    from services.images.intake import plan_from_existing
    root = tmp_path / "상품이미지"
    save_photos(root, "L002", plan_from_manuscript(manuscript))
    plan = plan_from_existing(root, "L002")
    assert plan.representative.name == "00_대표_대표이미지_A_4컬러.jpg"
    assert [p.name for p in plan.optional] == ["01_모델컷.jpg", "02_네이비.jpg", "03_아이보리.jpg"]
    assert [p.name for p in plan.detail] == ["90_detail_01.jpg", "90_detail_02.jpg", "90_detail_03.jpg"]
    rep_hash = sha(plan.representative)
    # 아이보리를 맨 앞으로, 네이비는 빼고, 새 사진 추가
    new = jpg(tmp_path / "새컷.jpg", (10, 20, 30))
    edited = PhotoPlan(plan.representative, [plan.optional[2], plan.optional[0], new], plan.detail[:2])
    result = save_photos(root, "L002", edited)
    names = [p.name for p in existing_photos(root, "L002")]
    assert names == ["00_대표_대표이미지_A_4컬러.jpg", "01_아이보리.jpg", "02_모델컷.jpg", "03_새컷.jpg",
                     "90_detail_01.jpg", "90_detail_02.jpg"]                  # 이름이 길어지지 않음
    assert sha(root / "L002" / "원본" / names[0]) == rep_hash
    assert (result.archived / "02_네이비.jpg").exists()                          # 뺀 사진도 보관 폴더에 남음


def test_dialog_loads_existing_photos(tmp_path, manuscript, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from services.automation.config import AutomationSettings, save_settings
    from storage.db import Database
    from ui.photo_intake_dialog import PhotoIntakeDialog
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    root = tmp_path / "상품이미지"
    save_settings(db, AutomationSettings(work_excel=str(tmp_path / "x.xlsx"), image_root=str(root)))
    save_photos(root, "L002", plan_from_manuscript(manuscript))
    dialog = PhotoIntakeDialog(db)
    assert not dialog.existing_btn.isEnabled() and dialog.plan().count == 0
    dialog.code_combo.setEditText("L002")                                       # 코드를 고르면 바로 불러옴
    assert dialog.existing_btn.isEnabled() and dialog.opt_list.count() == 3 and dialog.detail_list.count() == 3
    dialog.opt_list.setCurrentRow(0)
    dialog.opt_list.remove_selected()
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    dialog.on_save()
    assert [p.name for p in existing_photos(root, "L002")][1:3] == ["01_네이비.jpg", "02_아이보리.jpg"]
    dialog.deleteLater()
    db.close()
    app.processEvents()
