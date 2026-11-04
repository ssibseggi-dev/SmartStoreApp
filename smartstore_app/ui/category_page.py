"""카테고리 검색 — 로컬 캐시(SQLite)에서 검색, 필요 시 API로 새로고침.

CategorySearchWidget은 이후 상품 등록 화면에서 카테고리 선택용으로 재사용한다.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from api.categories import Category
from services.connection import AppServices
from ui.messages import describe_error
from ui.workers import run_async


class CategorySearchWidget(QWidget):
    category_selected = Signal(object)  # Category (더블클릭 시)

    def __init__(self, services: AppServices, parent=None):
        super().__init__(parent)
        self.services = services

        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("예: 여성 원피스 또는 카테고리 ID 50000807  (공백으로 여러 단어 AND 검색)")
        self.leaf_only = QCheckBox("최하위(등록 가능) 카테고리만")
        self.leaf_only.setChecked(True)
        self.refresh_btn = QPushButton("카테고리 새로고침")
        self.refresh_btn.clicked.connect(self.on_refresh)

        self._debounce = QTimer(self, singleShot=True, interval=250)
        self._debounce.timeout.connect(self.search)
        self.keyword.textChanged.connect(lambda _: self._debounce.start())
        self.leaf_only.toggled.connect(lambda _: self.search())

        top = QHBoxLayout()
        top.addWidget(QLabel("검색"))
        top.addWidget(self.keyword, 1)
        top.addWidget(self.leaf_only)
        top.addWidget(self.refresh_btn)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["카테고리 ID", "전체 카테고리명", "최하위"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._on_double_click)

        self.info = QLabel()

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.info)

        self._results: list[Category] = []
        self.search()

    def selected_category(self) -> Category | None:
        row = self.table.currentRow()
        return self._results[row] if 0 <= row < len(self._results) else None

    def search(self) -> None:
        total = self.services.db.category_count()
        if total == 0:
            self._show([])
            self.info.setText("저장된 카테고리가 없습니다. '카테고리 새로고침'을 누르거나 설정에서 연결 테스트를 하세요.")
            return
        results = self.services.db.search_categories(self.keyword.text(), self.leaf_only.isChecked())
        self._show(results)
        more = " (상위 500개만 표시)" if len(results) >= 500 else ""
        self.info.setText(f"검색 결과 {len(results):,}건{more} / 전체 {total:,}개")

    def _show(self, results: list[Category]) -> None:
        self._results = results
        self.table.setRowCount(len(results))
        for row, c in enumerate(results):
            id_item = QTableWidgetItem(c.id)
            id_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 0, id_item)
            self.table.setItem(row, 1, QTableWidgetItem(c.whole_name))
            last_item = QTableWidgetItem("O" if c.last else "")
            last_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 2, last_item)

    def _on_double_click(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._results):
            self.category_selected.emit(self._results[row])

    def on_refresh(self) -> None:
        self.refresh_btn.setEnabled(False)
        self.info.setText("카테고리를 불러오는 중...")

        def done(count: int) -> None:
            self.refresh_btn.setEnabled(True)
            self.search()

        def fail(exc: Exception) -> None:
            self.refresh_btn.setEnabled(True)
            self.search()
            QMessageBox.critical(self, "카테고리 조회 실패", describe_error(exc))

        run_async(self.services.refresh_categories, done, fail)
