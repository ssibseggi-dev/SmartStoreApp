"""입력 묶음(배송·반품·A/S·고시) 상단에 붙는 템플릿 선택/저장 바."""
from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QInputDialog, QLabel, QMenu, QMessageBox, QPushButton, QWidget,
)

from services import templates
from services.product_builder import ProductDraft
from storage.db import Database


class TemplateBar(QWidget):
    def __init__(self, kind: str, db: Database, get_draft: Callable[[], ProductDraft],
                 set_draft: Callable[[ProductDraft], None], parent=None):
        super().__init__(parent)
        self.kind, self.db = kind, db
        self._get_draft, self._set_draft = get_draft, set_draft
        self.label = templates.TEMPLATE_KINDS[kind][0]

        self.combo = QComboBox()
        self.combo.setMinimumWidth(120)
        self.combo.setPlaceholderText("템플릿 선택")
        apply_btn = QPushButton("적용")
        apply_btn.clicked.connect(self.on_apply)

        # 저장·기본 지정·삭제는 메뉴로 묶어 폭을 줄인다
        menu = QMenu(self)
        menu.addAction("현재 값을 템플릿으로 저장…", self.on_save)
        menu.addAction("기본 템플릿 지정/해제", self.on_default)
        menu.addSeparator()
        menu.addAction("선택한 템플릿 삭제", self.on_delete)
        manage_btn = QPushButton("관리 ▾")
        manage_btn.setMenu(menu)
        manage_btn.setObjectName("menuButton")

        label = QLabel("템플릿")
        label.setObjectName("hint")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.addWidget(label)
        layout.addWidget(self.combo, 1)
        layout.addWidget(apply_btn)
        layout.addWidget(manage_btn)
        self.reload()

    def reload(self, select_name: str | None = None) -> None:
        self.combo.clear()
        for t in templates.list_templates(self.db, self.kind):
            self.combo.addItem(f"★ {t.name}" if t.is_default else t.name, t)
        if select_name:
            for i in range(self.combo.count()):
                if self.combo.itemData(i).name == select_name:
                    self.combo.setCurrentIndex(i)

    def _current(self) -> templates.Template | None:
        return self.combo.currentData()

    def on_apply(self) -> None:
        t = self._current()
        if t:
            self._set_draft(templates.apply(self.kind, self._get_draft(), t.data))

    def on_save(self) -> None:
        current = self._current()
        name, ok = QInputDialog.getText(self, f"{self.label} 템플릿 저장", "템플릿 이름 (같은 이름이면 덮어씀)",
                                        text=current.name if current else "")
        name = name.strip()
        if not ok or not name:
            return
        self.db.save_template(self.kind, name, templates.extract(self.kind, self._get_draft()))
        self.reload(select_name=name)

    def on_default(self) -> None:
        t = self._current()
        if t:
            self.db.set_default_template(self.kind, None if t.is_default else t.id)
            self.reload(select_name=t.name)

    def on_delete(self) -> None:
        t = self._current()
        if t and QMessageBox.question(self, "템플릿 삭제", f"'{t.name}' 템플릿을 삭제할까요?") \
                == QMessageBox.StandardButton.Yes:
            self.db.delete_template(t.id)
            self.reload()
