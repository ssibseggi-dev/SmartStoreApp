"""임시저장함 — 저장한 상품 입력값 목록, 불러오기·삭제."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from services import drafts
from storage.db import Database
from ui.theme import set_role


class DraftsPage(QWidget):
    open_requested = Signal(int)

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["상품명", "카테고리", "판매가", "옵션", "저장 시각"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(lambda row, _c: self._open(row))

        open_btn = set_role(QPushButton("불러오기"), "primary")
        delete_btn = set_role(QPushButton("삭제"), "danger")
        open_btn.clicked.connect(lambda: self._open(self.table.currentRow()))
        delete_btn.clicked.connect(self.on_delete)
        buttons = QHBoxLayout()
        buttons.addWidget(open_btn)
        buttons.addWidget(delete_btn)
        buttons.addStretch()

        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(self.table)

        self._ids: list[int] = []
        self.reload()

    def reload(self) -> None:
        items = drafts.list_summaries(self.db)
        self._ids = [s.id for s in items]
        self.table.setRowCount(len(items))
        for r, s in enumerate(items):
            values = [s.name, s.category, f"{s.sale_price:,}원",
                      f"{s.option_count:,}개" if s.option_count else "-", s.updated_at]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c in (2, 3):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, item)

    def _open(self, row: int) -> None:
        if 0 <= row < len(self._ids):
            self.open_requested.emit(self._ids[row])

    def on_delete(self) -> None:
        row = self.table.currentRow()
        if not (0 <= row < len(self._ids)):
            return
        name = self.table.item(row, 0).text()
        if QMessageBox.question(self, "삭제", f"'{name}' 임시저장을 삭제할까요?") == QMessageBox.StandardButton.Yes:
            self.db.delete_draft(self._ids[row])
            self.reload()
