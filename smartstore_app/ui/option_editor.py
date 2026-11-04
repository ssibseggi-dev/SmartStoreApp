"""조합형 옵션 편집기 — 옵션명/값 입력 → 조합표 자동 생성 → 추가금액·재고 편집."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from services.options import MAX_GROUPS, OptionGroup, OptionRow, generate_rows, parse_values, total_stock


class OptionEditor(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._group_names: list[str] = []
        self._loading = False

        grid = QGridLayout()
        grid.addWidget(QLabel("옵션명"), 0, 0)
        grid.addWidget(QLabel("옵션값 (쉼표로 구분)"), 0, 1)
        self.name_edits: list[QLineEdit] = []
        self.value_edits: list[QLineEdit] = []
        for i, (name_hint, value_hint) in enumerate(
                [("예: 색상", "예: 블랙, 화이트, 네이비"), ("예: 사이즈", "예: S, M, L"), ("(선택)", "")]):
            name, values = QLineEdit(), QLineEdit()
            name.setPlaceholderText(name_hint)
            values.setPlaceholderText(value_hint)
            name.setMaximumWidth(160)
            grid.addWidget(name, i + 1, 0)
            grid.addWidget(values, i + 1, 1)
            self.name_edits.append(name)
            self.value_edits.append(values)

        self.default_stock = QSpinBox()
        self.default_stock.setRange(0, 99_999_999)
        self.default_stock.setValue(10)
        self.default_stock.setPrefix("기본 재고 ")
        generate_btn = QPushButton("조합 생성")
        generate_btn.clicked.connect(self.on_generate)
        gen_row = QHBoxLayout()
        gen_row.addWidget(self.default_stock)
        gen_row.addWidget(generate_btn)
        gen_row.addWidget(QLabel("값을 바꾸고 다시 생성해도 같은 조합의 입력값은 유지됩니다."))
        gen_row.addStretch()

        self.table = QTableWidget(0, 0)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setMinimumHeight(220)
        self.table.itemChanged.connect(self._on_item_changed)

        self.bulk_price = QSpinBox()
        self.bulk_price.setRange(-99_999_990, 99_999_990)
        self.bulk_price.setSingleStep(100)
        self.bulk_price.setSuffix(" 원")
        self.bulk_stock = QSpinBox()
        self.bulk_stock.setRange(0, 99_999_999)
        price_btn = QPushButton("추가금액 적용")
        stock_btn = QPushButton("재고 적용")
        price_btn.clicked.connect(lambda: self._bulk_set(self._price_col(), self.bulk_price.value()))
        stock_btn.clicked.connect(lambda: self._bulk_set(self._stock_col(), self.bulk_stock.value()))
        bulk = QHBoxLayout()
        bulk.addWidget(QLabel("선택 행(없으면 전체)에"))
        bulk.addWidget(self.bulk_price)
        bulk.addWidget(price_btn)
        bulk.addWidget(self.bulk_stock)
        bulk.addWidget(stock_btn)
        bulk.addStretch()

        self.summary = QLabel()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(grid)
        layout.addLayout(gen_row)
        layout.addWidget(self.table)
        layout.addLayout(bulk)
        layout.addWidget(self.summary)
        self._update_summary()

    # --- 열 위치 ---
    def _price_col(self) -> int:
        return len(self._group_names)

    def _stock_col(self) -> int:
        return len(self._group_names) + 1

    def _code_col(self) -> int:
        return len(self._group_names) + 2

    def _usable_col(self) -> int:
        return len(self._group_names) + 3

    # --- 데이터 입출력 ---
    def groups(self) -> list[OptionGroup]:
        groups = []
        for name, values in zip(self.name_edits, self.value_edits):
            g = OptionGroup(name.text().strip(), parse_values(values.text()))
            if g.name or g.values:
                groups.append(g)
        return groups

    def rows(self) -> list[OptionRow]:
        rows = []
        n = len(self._group_names)
        for r in range(self.table.rowCount()):
            rows.append(OptionRow(
                values=[self.table.item(r, c).text() for c in range(n)],
                price=int(self.table.item(r, self._price_col()).data(Qt.ItemDataRole.EditRole) or 0),
                stock=int(self.table.item(r, self._stock_col()).data(Qt.ItemDataRole.EditRole) or 0),
                seller_code=self.table.item(r, self._code_col()).text(),
                usable=self.table.item(r, self._usable_col()).checkState() == Qt.CheckState.Checked,
            ))
        return rows

    def set_data(self, groups: list[OptionGroup], rows: list[OptionRow]) -> None:
        for i in range(MAX_GROUPS):
            g = groups[i] if i < len(groups) else OptionGroup()
            self.name_edits[i].setText(g.name)
            self.value_edits[i].setText(", ".join(g.values))
        self._fill_table([g.name for g in groups if g.name and g.values], rows)

    def on_generate(self) -> None:
        groups = [g for g in self.groups() if g.name and g.values]
        rows = generate_rows(groups, self.rows(), self.default_stock.value())
        self._fill_table([g.name for g in groups], rows)

    def _fill_table(self, group_names: list[str], rows: list[OptionRow]) -> None:
        self._loading = True
        self._group_names = group_names
        headers = group_names + ["추가금액", "재고", "판매자관리코드", "사용"]
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, v in enumerate(row.values):
                item = QTableWidgetItem(v)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(r, c, item)
            for col, value in ((self._price_col(), row.price), (self._stock_col(), row.stock)):
                item = QTableWidgetItem()
                item.setData(Qt.ItemDataRole.EditRole, int(value))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, col, item)
            self.table.setItem(r, self._code_col(), QTableWidgetItem(row.seller_code))
            usable = QTableWidgetItem()
            usable.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            usable.setCheckState(Qt.CheckState.Checked if row.usable else Qt.CheckState.Unchecked)
            self.table.setItem(r, self._usable_col(), usable)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self._code_col(), QHeaderView.ResizeMode.Stretch)
        self._loading = False
        self._update_summary()
        self.changed.emit()

    def _bulk_set(self, col: int, value: int) -> None:
        if not self.table.rowCount():
            return
        rows = {i.row() for i in self.table.selectedIndexes()} or range(self.table.rowCount())
        self._loading = True
        for r in rows:
            self.table.item(r, col).setData(Qt.ItemDataRole.EditRole, value)
        self._loading = False
        self._update_summary()
        self.changed.emit()

    def _on_item_changed(self, _item) -> None:
        if not self._loading:
            self._update_summary()
            self.changed.emit()

    def _update_summary(self) -> None:
        if not self.table.rowCount():
            self.summary.setText("조합 없음 — 옵션명과 값을 입력하고 '조합 생성'을 누르세요.")
            return
        rows = self.rows()
        usable = sum(1 for r in rows if r.usable)
        self.summary.setText(f"조합 {len(rows):,}개 (사용 {usable:,}개) · 재고 합계 {total_stock(rows):,}개")
