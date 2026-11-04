"""상품 수정 대화상자 — 상품명, 판매가, 재고(또는 옵션별 추가금액·재고·사용), 판매/전시 상태."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from services.connection import AppServices
from services.product_manager import (
    DISPLAY_LABELS, EDITABLE_STATUSES, NAME_MAX, STATUS_LABELS, ProductEdit, save_edit, validate_edit,
)
from ui.messages import describe_error
from ui.theme import set_role
from ui.workers import run_async


class ProductEditDialog(QDialog):
    def __init__(self, services: AppServices, edit: ProductEdit, parent=None, *,
                 banned_words: list[str] | None = None, lock_status: bool = False):
        """lock_status: 검수 화면에서 열 때 판매·전시 상태는 바꾸지 않게 한다 ('판매 시작' 버튼으로)."""
        super().__init__(parent)
        self.services, self.edit = services, edit
        self.banned_words = banned_words or []
        self.name_edit = QLineEdit(edit.name)
        self.name_edit.setMaxLength(NAME_MAX)
        self.name_count = QLabel()
        self.name_count.setObjectName("hint")
        self.name_edit.textChanged.connect(lambda t: self.name_count.setText(f"{len(t.strip())}/{NAME_MAX}자"))
        self.name_count.setText(f"{len(edit.name.strip())}/{NAME_MAX}자")
        name_row = QHBoxLayout()
        name_row.addWidget(self.name_edit, 1)
        name_row.addWidget(self.name_count)
        self.setWindowTitle(f"상품 수정 — {edit.origin_product_no}")
        self.resize(720, 560 if edit.has_options else 300)

        self.price_spin = QSpinBox()
        self.price_spin.setRange(0, 999_999_990)
        self.price_spin.setSingleStep(1000)
        self.price_spin.setGroupSeparatorShown(True)
        self.price_spin.setSuffix(" 원")
        self.price_spin.setValue(edit.sale_price)

        self.stock_spin = QSpinBox()
        self.stock_spin.setRange(0, 99_999_999)
        self.stock_spin.setGroupSeparatorShown(True)
        self.stock_spin.setValue(edit.effective_stock())
        self.stock_spin.setEnabled(not edit.has_options)
        self.stock_spin.valueChanged.connect(lambda v: None if edit.has_options else self._suggest_sale(v))

        self.status_combo = QComboBox()
        statuses = EDITABLE_STATUSES if edit.status in EDITABLE_STATUSES else [edit.status, *EDITABLE_STATUSES]
        for s in statuses:
            self.status_combo.addItem(STATUS_LABELS.get(s, s), s)
        self.status_combo.setCurrentIndex(self.status_combo.findData(edit.status))

        self.display_combo = QComboBox()
        for code, label in DISPLAY_LABELS.items():
            self.display_combo.addItem(label, code)
        self.display_combo.setCurrentIndex(max(0, self.display_combo.findData(edit.display_status)))

        form = QFormLayout()
        form.addRow("상품명", name_row)
        form.addRow("판매가", self.price_spin)
        form.addRow("재고수량 (옵션 합계)" if edit.has_options else "재고수량", self.stock_spin)
        if edit.has_options:
            self.stock_spin.setToolTip("옵션이 있는 상품은 아래 옵션별 '재고' 칸에 넣으면 합계가 자동으로 바뀝니다.")
        form.addRow("판매 상태", self.status_combo)
        form.addRow("전시 상태", self.display_combo)

        if lock_status:
            self.status_combo.setEnabled(False)
            self.display_combo.setEnabled(False)
            self.status_combo.setToolTip("판매 시작은 검수 대기 목록의 '선택 상품 판매 시작' 버튼으로 합니다.")

        layout = QVBoxLayout(self)
        layout.addLayout(form)

        self.table: QTableWidget | None = None
        if edit.has_options:
            guide = QLabel("옵션별 수정 — <b>재고</b>·추가금액 칸을 클릭해 숫자를 넣으세요. 합계가 위 재고수량이 됩니다. "
                           "(옵션 추가·삭제는 스마트스토어센터에서)")
            guide.setWordWrap(True)
            layout.addWidget(guide)
            self.table = self._build_option_table()
            self.table.itemChanged.connect(self._update_stock_total)
            layout.addWidget(self.table, 1)

        self.status = QLabel()
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        set_role(self.buttons.button(QDialogButtonBox.StandardButton.Save), "primary").setText("저장")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.accepted.connect(self.on_save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.saved_actions: list[str] = []

    def _build_option_table(self) -> QTableWidget:
        names = self.edit.group_names
        table = QTableWidget(len(self.edit.options), len(names) + 3)
        table.setHorizontalHeaderLabels([*names, "추가금액", "재고", "사용"])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        table.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)   # 한 번 클릭·바로 입력으로 편집
        for r, o in enumerate(self.edit.options):
            for c, v in enumerate(o.values):
                item = QTableWidgetItem(v)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(r, c, item)
            for c, value in ((len(names), o.price), (len(names) + 1, o.stock)):
                item = QTableWidgetItem()
                item.setData(Qt.ItemDataRole.EditRole, value)
                item.setBackground(QColor("#E8F4EC"))   # 입력할 수 있는 칸 표시
                item.setToolTip("클릭해서 숫자를 입력하세요.")
                table.setItem(r, c, item)
            usable = QTableWidgetItem()
            usable.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            usable.setCheckState(Qt.CheckState.Checked if o.usable else Qt.CheckState.Unchecked)
            table.setItem(r, len(names) + 2, usable)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        return table

    def _collect(self) -> ProductEdit:
        e = self.edit
        e.name = self.name_edit.text().strip()
        e.sale_price = self.price_spin.value()
        e.stock = self.stock_spin.value()
        e.status = self.status_combo.currentData()
        e.display_status = self.display_combo.currentData()
        if self.table is not None:
            n = len(e.group_names)
            for r, o in enumerate(e.options):
                o.price = int(self.table.item(r, n).data(Qt.ItemDataRole.EditRole) or 0)
                o.stock = int(self.table.item(r, n + 1).data(Qt.ItemDataRole.EditRole) or 0)
                o.usable = self.table.item(r, n + 2).checkState() == Qt.CheckState.Checked
        return e

    def _update_stock_total(self, _item=None) -> None:
        total = self._collect().effective_stock()
        self.stock_spin.setValue(total)
        self._suggest_sale(total)

    def _suggest_sale(self, total: int) -> None:
        """품절 상품에 재고를 넣으면 판매 상태를 '판매중'으로 바꿔 둔다 (저장 전에 다시 고를 수 있음)."""
        original = self.edit.raw.get("originProduct", {}).get("statusType")
        if original == "OUTOFSTOCK" and total > 0 and self.status_combo.isEnabled()                 and self.status_combo.currentData() == "OUTOFSTOCK":
            self.status_combo.setCurrentIndex(self.status_combo.findData("SALE"))
            self.status.setText("재고를 넣어 판매 상태를 '판매중'으로 바꿨습니다. 저장하면 다시 판매됩니다.")

    def on_save(self) -> None:
        edit = self._collect()
        errors = validate_edit(edit, self.banned_words)
        if errors:
            QMessageBox.warning(self, "입력 확인", "\n".join(f"• {e}" for e in errors))
            return
        if edit.status == "SALE" and edit.display_status == "ON" and \
                edit.raw["originProduct"].get("statusType") != "SALE" and QMessageBox.question(
                    self, "판매 시작", "판매중 + 전시중으로 저장하면 고객에게 바로 노출·판매됩니다. 계속할까요?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.buttons.setEnabled(False)
        self.status.setText("저장 중...")
        run_async(lambda: save_edit(self.services, edit), self._on_saved, self._on_failed)

    def _on_saved(self, actions: list[str]) -> None:
        self.saved_actions = actions
        if not actions:
            QMessageBox.information(self, "상품 수정", "변경된 내용이 없습니다.")
        self.accept()

    def _on_failed(self, exc: Exception) -> None:
        self.buttons.setEnabled(True)
        self.status.setText("저장 실패")
        QMessageBox.critical(self, "저장 실패", describe_error(exc))
