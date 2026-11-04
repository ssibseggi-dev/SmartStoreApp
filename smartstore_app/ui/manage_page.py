"""상품 관리 — 스토어 상품 목록 조회, 수정, 판매 상태 일괄 변경."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from services.connection import AppServices
from services.product_manager import (
    DISPLAY_LABELS, STATUS_LABELS, BulkResult, ProductEdit, ProductSummary, bulk_change_status, bulk_sold_out, fetch_all,
    filter_products, load_edit,
)
from ui.messages import describe_error
from ui.product_edit_dialog import ProductEditDialog
from ui.workers import run_async
from ui.theme import STATUS_COLORS, set_role

_COLUMNS = ["원상품번호", "상품명", "판매상태", "전시", "판매가", "재고", "카테고리", "수정일"]


class ManagePage(QWidget):
    def __init__(self, services: AppServices, parent=None):
        super().__init__(parent)
        self.services = services
        self._all: list[ProductSummary] = []
        self._shown: list[ProductSummary] = []

        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("상품명 또는 상품번호")
        self.keyword.textChanged.connect(lambda _: self._apply_filter())
        self.status_filter = QComboBox()
        self.status_filter.addItem("전체 상태", "")
        for code, label in STATUS_LABELS.items():
            if code != "DELETE":
                self.status_filter.addItem(label, code)
        self.status_filter.currentIndexChanged.connect(lambda _: self._apply_filter())
        self.load_btn = set_role(QPushButton("스토어에서 불러오기"), "primary")
        self.load_btn.clicked.connect(self.on_load)

        top = QHBoxLayout()
        top.addWidget(self.keyword, 1)
        top.addWidget(self.status_filter)
        top.addWidget(self.load_btn)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(lambda row, _c: self.on_edit(row))

        self.edit_btn = QPushButton("수정")
        self.sale_btn = QPushButton("선택 상품 판매중으로")
        self.suspend_btn = QPushButton("선택 상품 판매중지로")
        self.soldout_btn = QPushButton("선택 상품 품절 처리")
        self.soldout_btn.clicked.connect(self.on_sold_out)
        self.edit_btn.clicked.connect(lambda: self.on_edit(self.table.currentRow()))
        self.sale_btn.clicked.connect(lambda: self.on_bulk_status("SALE"))
        self.suspend_btn.clicked.connect(lambda: self.on_bulk_status("SUSPENSION"))
        actions = QHBoxLayout()
        for b in (self.edit_btn, self.sale_btn, self.suspend_btn, self.soldout_btn):
            actions.addWidget(b)
        actions.addStretch()

        self.info = QLabel("'스토어에서 불러오기'를 눌러 등록 상품을 조회하세요.")

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addLayout(actions)
        layout.addWidget(self.info)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        for b in (self.load_btn, self.edit_btn, self.sale_btn, self.suspend_btn, self.soldout_btn):
            b.setEnabled(not busy)
        if message:
            self.info.setText(message)

    # --- 조회 ---
    def on_load(self) -> None:
        self._set_busy(True, "상품 목록을 불러오는 중...")
        run_async(lambda: fetch_all(self.services), self._on_loaded, self._on_error)

    def _on_loaded(self, items: list[ProductSummary]) -> None:
        self._set_busy(False)
        self._all = items
        self._apply_filter()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False, "작업 실패")
        QMessageBox.critical(self, "오류", describe_error(exc))

    def _apply_filter(self) -> None:
        self._shown = filter_products(self._all, self.keyword.text(), self.status_filter.currentData())
        self.table.setRowCount(len(self._shown))
        for r, p in enumerate(self._shown):
            values = [str(p.origin_product_no), p.name, STATUS_LABELS.get(p.status, p.status),
                      DISPLAY_LABELS.get(p.display_status, p.display_status), f"{p.sale_price:,}",
                      f"{p.stock:,}", p.category, p.modified]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c in (4, 5):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if c == 2:
                    item.setForeground(QColor(STATUS_COLORS.get(p.status, "#667085")))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.table.setItem(r, c, item)
        if self._all:
            self.info.setText(f"{len(self._shown):,}개 표시 / 전체 {len(self._all):,}개")

    def _selected(self) -> list[ProductSummary]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self._shown[r] for r in rows if r < len(self._shown)]

    # --- 수정 ---
    def on_edit(self, row: int) -> None:
        if not (0 <= row < len(self._shown)):
            return
        product = self._shown[row]
        self._set_busy(True, f"{product.origin_product_no} 상품 정보를 불러오는 중...")
        run_async(lambda: load_edit(self.services, product.origin_product_no), self._open_editor, self._on_error)

    def _open_editor(self, edit: ProductEdit) -> None:
        self._set_busy(False)
        dialog = ProductEditDialog(self.services, edit, self)
        if dialog.exec() and dialog.saved_actions:
            self.info.setText(f"{edit.origin_product_no} 저장 완료: {', '.join(dialog.saved_actions)}")
            self.on_load()

    # --- 일괄 판매 상태 변경 ---
    def on_bulk_status(self, status: str) -> None:
        products = self._selected()
        if not products:
            QMessageBox.information(self, "선택", "목록에서 상품을 선택하세요.")
            return
        label = STATUS_LABELS[status]
        warn = ""
        if status == "SALE":
            on_display = sum(1 for p in products if p.display_status == "ON")
            if on_display:
                warn = f"\n\n전시중인 상품 {on_display}개는 바로 고객에게 판매됩니다."
            if any(p.stock == 0 for p in products):
                warn += "\n재고가 0인 상품은 판매중으로 바뀌지 않을 수 있습니다."
        if QMessageBox.question(self, "판매 상태 변경",
                                f"선택한 상품 {len(products)}개를 '{label}'(으)로 바꿀까요?{warn}") \
                != QMessageBox.StandardButton.Yes:
            return
        nos = [p.origin_product_no for p in products]
        self._set_busy(True, f"{len(nos)}개 상품 상태 변경 중...")
        run_async(lambda: bulk_change_status(self.services, nos, status), self._on_bulk_done, self._on_error)

    def on_sold_out(self) -> None:
        products = self._selected()
        if not products:
            QMessageBox.information(self, "선택", "목록에서 상품을 선택하세요.")
            return
        names = ", ".join(str(p.origin_product_no) for p in products[:10]) + (" …" if len(products) > 10 else "")
        if QMessageBox.question(
                self, "품절 처리",
                f"선택한 상품 {len(products)}개({names})의 재고를 모두 0으로 바꿔 품절로 만듭니다.\n"
                f"옵션이 있는 상품은 모든 옵션 재고가 0이 됩니다.\n\n"
                f"다시 팔 때는 '수정'에서 재고를 넣으세요. (판매중으로 안 돌아오면 '선택 상품 판매중으로')\n\n"
                f"계속할까요?") != QMessageBox.StandardButton.Yes:
            return
        nos = [p.origin_product_no for p in products]
        self._set_busy(True, f"{len(nos)}개 상품 품절 처리 중...")
        run_async(lambda: bulk_sold_out(self.services, nos), self._on_bulk_done, self._on_error)

    def _on_bulk_done(self, results: list[BulkResult]) -> None:
        self._set_busy(False)
        failed = [r for r in results if not r.ok]
        msg = f"성공 {len(results) - len(failed)}개, 실패 {len(failed)}개"
        if failed:
            msg += "\n\n" + "\n".join(f"• {r.origin_product_no}: {r.message}" for r in failed)
            QMessageBox.warning(self, "판매 상태 변경 결과", msg)
        else:
            QMessageBox.information(self, "판매 상태 변경 결과", msg)
        self.on_load()
