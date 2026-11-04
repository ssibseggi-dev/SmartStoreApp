"""상품 사진 넣기 — 상품코드를 고르고 대표·추가·상세 사진을 골라(끌어다 놓기 가능) 사진 폴더 규칙대로 저장한다.

이미 사진이 있는 상품코드를 고르면 그 사진을 불러와 순서를 바꾸거나 빼고 더해서 다시 저장할 수 있다.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)

from services.automation.config import load_settings
from services.automation.input_sheet import read_items
from services.images import SOURCE_EXTENSIONS
from services.images.intake import (
    MAX_OPTIONAL, PhotoPlan, existing_photos, plan_from_existing, plan_from_manuscript, save_photos, validate_code,
    validate_plan,
)
from ui.theme import set_role

_THUMB = 56
_FILTER = "사진 (" + " ".join(f"*{e}" for e in sorted(SOURCE_EXTENSIONS)) + ")"


class PhotoList(QListWidget):
    """사진 목록. 탐색기에서 파일을 끌어다 놓을 수 있고, 목록 안에서 끌어서 순서를 바꾼다."""

    def __init__(self, limit: int | None = None, parent=None):
        super().__init__(parent)
        self.limit = limit
        self.setIconSize(QSize(_THUMB, _THUMB))
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setAcceptDrops(True)
        self.setMinimumHeight(_THUMB * 2 + 16)

    def paths(self) -> list[Path]:
        return [Path(self.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.count())]

    def set_paths(self, paths: list[Path]) -> None:
        self.clear()
        self.add_paths(paths)

    def add_paths(self, paths: list[Path]) -> list[Path]:
        """사진 파일만 추가한다. 한도를 넘으면 넘친 파일을 돌려준다."""
        rejected = []
        for path in paths:
            path = Path(path)
            if path.suffix.lower() not in SOURCE_EXTENSIONS or not path.is_file():
                rejected.append(path)
                continue
            if self.limit == 1:
                self.clear()
            elif self.limit is not None and self.count() >= self.limit:
                rejected.append(path)
                continue
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path))
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                thumb = pixmap.scaled(_THUMB, _THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
                icon = QIcon(thumb)
                icon.addPixmap(thumb, QIcon.Mode.Selected)
                item.setIcon(icon)
            self.addItem(item)
        return rejected

    def move_selected(self, step: int) -> None:
        row = self.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.count():
            return
        item = self.takeItem(row)
        self.insertItem(target, item)
        self.setCurrentRow(target)

    def remove_selected(self) -> None:
        for item in self.selectedItems():
            self.takeItem(self.row(item))

    # 탐색기에서 끌어다 놓기
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and event.source() is None:
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls() and event.source() is None:
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if event.mimeData().hasUrls() and event.source() is None:
            self.add_paths([Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()])
            event.acceptProposedAction()
        elif event.source() is self:
            super().dropEvent(event)   # 목록 안에서 순서 바꾸기
        else:
            event.ignore()


class PhotoIntakeDialog(QDialog):
    def __init__(self, db, parent=None, code: str = ""):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("상품 사진 넣기")
        self.resize(960, 760)
        self.root = load_settings(db).image_root_path()

        self.code_combo = QComboBox()
        self.code_combo.setEditable(True)
        self.code_combo.lineEdit().setPlaceholderText("예: L002 (작업 엑셀 상품코드를 고르거나 새로 입력)")
        try:
            items = read_items(load_settings(db).work_excel_path())
        except (OSError, ValueError):
            items = []
        for item in items:
            if item.code:
                self.code_combo.addItem(f"{item.code}  ·  {item.get('원본상품명')}  ({item.status})", item.code)
        self.code_combo.setCurrentIndex(-1)
        if code:
            index = self.code_combo.findData(code)
            if index >= 0:
                self.code_combo.setCurrentIndex(index)
            else:
                self.code_combo.setEditText(code)
        self.code_combo.currentTextChanged.connect(self._update_existing)
        manuscript_btn = set_role(QPushButton("원고 폴더 불러오기"), "primary")
        manuscript_btn.clicked.connect(self.on_load_manuscript)
        top = QHBoxLayout()
        top.addWidget(QLabel("상품코드"))
        top.addWidget(self.code_combo, 1)
        self.existing_btn = QPushButton("기존 사진 불러오기")
        self.existing_btn.clicked.connect(self.on_load_existing)
        top.addWidget(self.existing_btn)
        top.addWidget(manuscript_btn)
        self.existing = QLabel()
        self.existing.setObjectName("hint")
        self.existing.setWordWrap(True)

        self.rep_list = PhotoList(limit=1)
        self.rep_list.setMinimumHeight(_THUMB + 16)
        self.rep_list.setFixedHeight(_THUMB + 16)
        self.opt_list = PhotoList(limit=MAX_OPTIONAL)
        self.detail_list = PhotoList()
        grid = QGridLayout()
        grid.addWidget(self._box("대표 이미지 (1장 · 검색 목록에 보이는 사진)", self.rep_list), 0, 0, 1, 2)
        grid.addWidget(self._box(f"추가 이미지 (최대 {MAX_OPTIONAL}장 · 위에서부터 순서대로)", self.opt_list), 1, 0)
        grid.addWidget(self._box("상세페이지 이미지 (위에서부터 순서대로)", self.detail_list), 1, 1)
        grid.setRowStretch(1, 1)
        grid.setRowStretch(0, 0)

        hint = QLabel("탐색기에서 사진을 각 칸으로 끌어다 놓거나 '파일 추가'로 고르세요. 목록 안에서 끌거나 ▲▼로 순서를 바꿉니다. "
                      "원고 폴더(01_대표이미지 · 04_추가이미지 · 05_상세페이지\\분할)를 고르면 세 칸이 한 번에 채워집니다. "
                      "고른 원래 파일은 옮기거나 지우지 않고 복사합니다.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        set_role(self.buttons.button(QDialogButtonBox.StandardButton.Save), "primary").setText("사진 폴더에 저장")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("닫기")
        self.buttons.accepted.connect(self.on_save)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.existing)
        layout.addLayout(grid, 1)
        layout.addWidget(hint)
        layout.addWidget(self.buttons)
        self.saved = None
        self._loaded_code = ""   # 기존 사진을 자동으로 불러온 상품코드 (같은 코드로 다시 덮어쓰지 않게)
        self._update_existing()

    def _box(self, title: str, photo_list: PhotoList) -> QGroupBox:
        add_btn = QPushButton("파일 추가")
        up_btn, down_btn = QPushButton("▲"), QPushButton("▼")
        remove_btn = QPushButton("빼기")
        for b in (up_btn, down_btn):
            b.setFixedWidth(40)
        add_btn.clicked.connect(lambda: self._pick(photo_list))
        up_btn.clicked.connect(lambda: photo_list.move_selected(-1))
        down_btn.clicked.connect(lambda: photo_list.move_selected(1))
        remove_btn.clicked.connect(photo_list.remove_selected)
        box = QGroupBox(title)
        if photo_list.limit == 1:   # 대표 1장: 목록 옆에 버튼
            h = QHBoxLayout(box)
            h.addWidget(photo_list, 1)
            h.addWidget(add_btn)
            h.addWidget(remove_btn)
            return box
        row = QHBoxLayout()
        row.addWidget(add_btn)
        row.addWidget(up_btn)
        row.addWidget(down_btn)
        row.addWidget(remove_btn)
        row.addStretch()
        v = QVBoxLayout(box)
        v.addWidget(photo_list, 1)
        v.addLayout(row)
        return box

    def code(self) -> str:
        data = self.code_combo.currentData()
        text = self.code_combo.currentText().strip()
        if data and text == self.code_combo.itemText(self.code_combo.currentIndex()):
            return data
        return text.split()[0] if text else ""

    def plan(self) -> PhotoPlan:
        rep = self.rep_list.paths()
        return PhotoPlan(rep[0] if rep else None, self.opt_list.paths(), self.detail_list.paths())

    def _update_existing(self, _text: str = "") -> None:
        code = self.code()
        old = existing_photos(self.root, code) if code and not validate_code(code) else []
        self.existing_btn.setEnabled(bool(old))
        if old and code != self._loaded_code and not self.plan().count:
            self.apply_plan(plan_from_existing(self.root, code))   # 빈 상태에서 고르면 바로 불러와 수정
            self._loaded_code = code
        if old:
            self.existing.setText(f"{code}에 저장된 사진 {len(old)}장 — '기존 사진 불러오기'로 불러와 순서를 바꾸거나 빼고 더한 뒤 "
                                  f"저장하면 그 구성으로 바뀝니다. 바뀌기 전 사진은 원본 폴더 안 '_이전_날짜' 폴더에 남습니다.")
        else:
            self.existing.setText(f"저장 위치: {self.root}\\{code or '상품코드'}\\원본\\")

    def on_load_existing(self) -> None:
        code = self.code()
        if self.plan().count and QMessageBox.question(
                self, "기존 사진 불러오기", "지금 칸에 넣은 사진을 비우고 저장된 사진을 불러올까요?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.apply_plan(plan_from_existing(self.root, code))
        self._loaded_code = code

    def _pick(self, photo_list: PhotoList) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "사진 고르기", "", _FILTER)
        rejected = photo_list.add_paths([Path(f) for f in files])
        if rejected:
            QMessageBox.information(self, "사진 추가", f"{len(rejected)}장은 넣지 못했습니다 (사진 파일이 아니거나 장수 초과).")

    def on_load_manuscript(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "원고 폴더 고르기")
        if not folder:
            return
        try:
            plan = plan_from_manuscript(Path(folder))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "원고 폴더", str(e))
            return
        self.apply_plan(plan)

    def apply_plan(self, plan: PhotoPlan) -> None:
        self.rep_list.set_paths([plan.representative] if plan.representative else [])
        self.opt_list.set_paths(plan.optional)
        self.detail_list.set_paths(plan.detail)

    def on_save(self) -> None:
        code, plan = self.code(), self.plan()
        errors = ([validate_code(code)] if validate_code(code) else []) + validate_plan(plan)
        if errors:
            QMessageBox.warning(self, "상품 사진 넣기", "\n".join(f"• {e}" for e in errors))
            return
        if existing_photos(self.root, code) and QMessageBox.question(
                self, "상품 사진 넣기",
                f"{code}에 이미 있는 사진을 '_이전_날짜' 폴더로 옮기고 새 사진으로 바꿀까요?") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            self.saved = save_photos(self.root, code, plan)
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "상품 사진 넣기", f"저장하지 못했습니다.\n{e}")
            return
        box = QMessageBox(self)
        box.setWindowTitle("상품 사진 넣기")
        box.setText(f"{code} 사진 {len(self.saved.saved)}장을 저장했습니다.\n"
                    f"대표 1 · 추가 {len(plan.optional)} · 상세 {len(plan.detail)}"
                    + (f"\n기존 사진은 '{self.saved.archived.name}' 폴더로 옮겼습니다." if self.saved.archived else ""))
        open_btn = box.addButton("폴더 열기", QMessageBox.ButtonRole.ActionRole)
        box.addButton("확인", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.saved.folder)))
        self.accept()
