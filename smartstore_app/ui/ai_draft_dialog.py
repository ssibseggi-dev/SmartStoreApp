"""저장된 AI 초안 보기 — 상품명 후보·특징·검색 태그·색상을 보고, 등록 전 상품이면 고른 후보를 작업 엑셀에 적는다."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from services import ai_draft
from services.automation.config import load_settings
from ui.theme import set_role


class AiDraftDialog(QDialog):
    def __init__(self, db, code: str | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("AI 초안 보기")
        self.resize(980, 640)
        self._saved: list[ai_draft.SavedDraft] = []

        self.codes = QListWidget()
        self.codes.setMinimumWidth(220)
        self.codes.setMaximumWidth(300)
        self.codes.setWordWrap(True)
        self.codes.setSpacing(2)
        self.codes.currentRowChanged.connect(self._show)

        self.title = QLabel()
        self.title.setObjectName("pageTitle")
        self.meta = QLabel()
        self.meta.setObjectName("hint")
        self.meta.setWordWrap(True)

        # 상품명 후보
        self.names = QListWidget()
        self.names.setMaximumHeight(110)
        self.names.currentRowChanged.connect(lambda _r: self._update_buttons())
        self.current_name = QLabel()
        self.current_name.setObjectName("hint")
        self.current_name.setWordWrap(True)
        self.apply_name_btn = set_role(QPushButton("선택 후보를 상품명(최종)에 적용"), "primary")
        self.apply_name_btn.clicked.connect(self.on_apply_name)
        copy_name_btn = QPushButton("복사")
        copy_name_btn.clicked.connect(lambda: self._copy(self.names.currentItem().text()
                                                         if self.names.currentItem() else ""))
        name_buttons = QHBoxLayout()
        name_buttons.addWidget(self.current_name, 1)
        name_buttons.addWidget(copy_name_btn)
        name_buttons.addWidget(self.apply_name_btn)
        names_box = QGroupBox("상품명 후보")
        v = QVBoxLayout(names_box)
        v.addWidget(self.names)
        v.addLayout(name_buttons)

        # 특징 (설명)
        self.features = QPlainTextEdit()
        self.features.setReadOnly(True)
        self.features.setMaximumHeight(130)
        self.apply_desc_btn = QPushButton("설명(최종)에 적용")
        self.apply_desc_btn.clicked.connect(self.on_apply_description)
        copy_desc_btn = QPushButton("복사")
        copy_desc_btn.clicked.connect(lambda: self._copy(self.features.toPlainText()))
        self.current_desc = QLabel()
        self.current_desc.setObjectName("hint")
        desc_buttons = QHBoxLayout()
        desc_buttons.addWidget(self.current_desc, 1)
        desc_buttons.addWidget(copy_desc_btn)
        desc_buttons.addWidget(self.apply_desc_btn)
        features_box = QGroupBox("특징 (상세페이지 설명)")
        v = QVBoxLayout(features_box)
        v.addWidget(self.features)
        v.addLayout(desc_buttons)

        # 태그·색상
        self.tags = QLabel()
        self.tags.setWordWrap(True)
        self.tags.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.colors = QLabel()
        copy_tags_btn = QPushButton("태그 복사")
        copy_tags_btn.clicked.connect(lambda: self._copy(", ".join(self._current().draft.tags)
                                                         if self._current() else ""))
        tag_row = QHBoxLayout()
        tag_row.addWidget(self.tags, 1)
        tag_row.addWidget(copy_tags_btn, 0, Qt.AlignmentFlag.AlignTop)
        extra_box = QGroupBox("검색 태그 · 색상")
        v = QVBoxLayout(extra_box)
        v.addLayout(tag_row)
        v.addWidget(self.colors)
        tag_hint = QLabel("검색 태그는 네이버에 자동으로 보내지 않습니다. 필요하면 복사해 스마트스토어 상품 수정에서 넣으세요.")
        tag_hint.setObjectName("hint")
        tag_hint.setWordWrap(True)
        v.addWidget(tag_hint)

        detail = QWidget()
        dv = QVBoxLayout(detail)
        dv.setContentsMargins(8, 0, 0, 0)
        dv.addWidget(self.title)
        dv.addWidget(self.meta)
        dv.addWidget(names_box)
        dv.addWidget(features_box)
        dv.addWidget(extra_box)
        dv.addStretch()

        splitter = QSplitter()
        splitter.addWidget(self.codes)
        splitter.addWidget(detail)
        splitter.setStretchFactor(1, 1)
        self.empty = QLabel("보관된 AI 초안이 없습니다. 자동화 설정 → 'AI 초안 (Claude)'에서 만들거나, "
                            "대기 상품 처리 때 빈 상품명·설명이 있으면 자동으로 만들어집니다.")
        self.empty.setObjectName("hint")
        self.empty.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.empty)
        layout.addWidget(splitter, 1)
        layout.addWidget(buttons)
        self.reload(code)

    def _excel(self):
        return load_settings(self.db).work_excel_path()

    def reload(self, code: str | None = None) -> None:
        self._saved = ai_draft.list_saved(self.db, self._excel())
        self.empty.setVisible(not self._saved)
        self.codes.blockSignals(True)
        self.codes.clear()
        for s in self._saved:
            name = s.name or (s.draft.names[0] if s.draft.names else "")
            label = f"{s.code}  ·  {s.status or '엑셀에 없음'}\n{name}"
            self.codes.addItem(QListWidgetItem(label))
        self.codes.blockSignals(False)
        index = next((i for i, s in enumerate(self._saved) if s.code == code), 0)
        if self._saved:
            self.codes.setCurrentRow(index)
            self._show(index)
        else:
            self._show(-1)

    def _current(self) -> ai_draft.SavedDraft | None:
        row = self.codes.currentRow()
        return self._saved[row] if 0 <= row < len(self._saved) else None

    def _show(self, _row: int) -> None:
        s = self._current()
        self.names.clear()
        if s is None:
            for w in (self.title, self.meta, self.current_name, self.current_desc, self.tags, self.colors):
                w.setText("")
            self.features.clear()
            self._update_buttons()
            return
        banned = load_settings(self.db).banned_words
        usable = s.draft.usable_names(banned)
        self.title.setText(s.code)
        state = ("등록 전 — 고른 값을 엑셀에 적으면 다음 등록에 쓰입니다." if s.editable else
                 "이미 등록된 상품 — 엑셀을 바꿔도 스마트스토어에는 반영되지 않습니다. 복사해 상품 수정에서 고치세요."
                 if s.in_excel else "작업 엑셀에 이 상품코드가 없습니다.")
        self.meta.setText(f"{s.status or '-'} · 만든 시각 {s.created_at} · {s.draft.model or '-'}\n{state}")
        for i, name in enumerate(s.draft.names, start=1):
            mark = "" if name in usable else "   (금지어·50자 초과 — 쓸 수 없음)"
            item = QListWidgetItem(f"{name}   [{len(name)}자]{mark}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            if name not in usable:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.names.addItem(item)
        current = next((i for i in range(self.names.count())
                        if self.names.item(i).data(Qt.ItemDataRole.UserRole) == s.name), -1)
        self.names.setCurrentRow(current if current >= 0 else (0 if usable else -1))
        self.current_name.setText(f"지금 엑셀 상품명: {s.name or '(비어 있음)'}")
        self.features.setPlainText(s.draft.description)
        same = s.description == s.draft.description
        self.current_desc.setText("지금 엑셀 설명: " + ("AI 특징 그대로" if same else (
            "(비어 있음)" if not s.description else "사람이 고친 내용")))
        self.tags.setText(", ".join(s.draft.tags) or "-")
        self.colors.setText("색상: " + (", ".join(s.draft.colors) or "-"))
        self._update_buttons()

    def _selected_name(self) -> str:
        item = self.names.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _update_buttons(self) -> None:
        s = self._current()
        editable = bool(s and s.editable)
        self.apply_name_btn.setEnabled(editable and bool(self._selected_name()) and self._selected_name() != s.name)
        self.apply_desc_btn.setEnabled(editable and bool(s.draft.features) and s.description != s.draft.description)

    def _copy(self, text: str) -> None:
        if text:
            QGuiApplication.clipboard().setText(text)

    def _apply(self, name: str | None = None, description: str | None = None) -> None:
        s = self._current()
        current = s.name if name else s.description
        field = "상품명(최종)" if name else "설명(최종)"
        human = current and current not in (s.draft.names if name else [s.draft.description])
        question = (f"{s.code}의 {field}에 사람이 적은 값이 있습니다.\n\n지금 값: {current}\n\n"
                    f"AI 초안으로 바꿀까요?" if human else f"{s.code}의 {field}을(를) 이 값으로 바꿀까요?")
        if QMessageBox.question(self, "AI 초안 적용", question) != QMessageBox.StandardButton.Yes:
            return
        try:
            ai_draft.apply_choice(self.db, self._excel(), s.code, name, description)
        except PermissionError:
            QMessageBox.warning(self, "AI 초안 적용", "작업 엑셀이 열려 있어 쓰지 못했습니다. 엑셀을 닫고 다시 누르세요.")
            return
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "AI 초안 적용", str(e))
            return
        self.reload(s.code)

    def on_apply_name(self) -> None:
        if self._selected_name():
            self._apply(name=self._selected_name())

    def on_apply_description(self) -> None:
        s = self._current()
        if s and s.draft.features:
            self._apply(description=s.draft.description)
