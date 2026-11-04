"""삭제 내역 — 검수 대기에서 삭제한 상품 목록. 작업 엑셀 줄과 사진으로 다시 등록(새 상품번호)하거나 내역에서 치운다."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from services.automation.review import DeletedItem, deleted_items, forget_deleted, reregister
from services.automation.run_lock import AlreadyRunningError
from services.automation.runner import format_report
from services.connection import AppServices, NotConfiguredError
from ui.messages import describe_error
from ui.theme import set_role
from ui.workers import run_with_progress

_COLUMNS = ["", "사진", "상품코드", "상품명", "판매가", "삭제 시각", "삭제 전 상품번호", "다시 등록"]
_THUMB = 72


class DeletedPage(QWidget):
    def __init__(self, services: AppServices, parent=None):
        super().__init__(parent)
        self.services = services
        self._items: list[DeletedItem] = []

        self.info = QLabel()
        self.info.setObjectName("hint")
        refresh_btn = QPushButton("새로고침")
        refresh_btn.clicked.connect(self.reload)
        top = QHBoxLayout()
        top.addWidget(self.info, 1)
        top.addWidget(refresh_btn)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(_THUMB + 12)
        self.table.setIconSize(QSize(_THUMB, _THUMB))
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, _THUMB + 16)
        self.table.itemChanged.connect(lambda _i: self._update_buttons())

        self.all_btn = QPushButton("전체 선택")
        self.all_btn.clicked.connect(self.on_toggle_all)
        self.forget_btn = set_role(QPushButton("내역에서 지우기"), "danger")
        self.forget_btn.clicked.connect(self.on_forget)
        self.restore_btn = set_role(QPushButton("선택 상품 다시 등록"), "primary")
        self.restore_btn.clicked.connect(self.on_restore)
        hint = QLabel("검수 대기에서 삭제한 상품입니다. 네이버는 삭제한 상품을 되살릴 수 없어서, '다시 등록'은 작업 엑셀의 그 상품 줄과 "
                      "사진 폴더로 판매중지 상태로 새로 등록합니다(새 상품번호, 이전 리뷰·찜은 이어지지 않음). "
                      "작업 엑셀에서 줄을 지운 상품은 다시 등록할 수 없습니다.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        bottom = QHBoxLayout()
        bottom.addWidget(self.all_btn)
        bottom.addStretch()
        bottom.addWidget(self.forget_btn)
        bottom.addWidget(self.restore_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addWidget(hint)
        layout.addLayout(bottom)
        self.reload()

    def showEvent(self, event) -> None:   # 탭을 열 때마다 최신 상태로
        super().showEvent(event)
        self.reload()

    # --- 목록 ---
    def reload(self) -> None:
        self._items = deleted_items(self.services.db)
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._items))
        for r, item in enumerate(self._items):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(r, 0, check)
            photo = QTableWidgetItem()
            if item.image:
                pixmap = QPixmap(str(item.image))
                if not pixmap.isNull():
                    thumb = pixmap.scaled(_THUMB, _THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation)
                    icon = QIcon(thumb)
                    icon.addPixmap(thumb, QIcon.Mode.Selected)
                    photo.setIcon(icon)
            self.table.setItem(r, 1, photo)
            values = [item.code, item.name, f"{item.sale_price:,}원" if item.sale_price else "-", item.deleted_at,
                      str(item.old_product_no or "-"), "가능" if item.in_excel else "엑셀에 없음"]
            for c, value in enumerate(values, start=2):
                cell = QTableWidgetItem(value)
                if c == 4:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if c == 7 and not item.in_excel:
                    cell.setForeground(QColor("#B54708"))
                    cell.setToolTip("작업 엑셀에 이 상품코드 줄이 없어 다시 등록할 수 없습니다.")
                self.table.setItem(r, c, cell)
        self.table.blockSignals(False)
        self.info.setText(f"삭제 내역 {len(self._items)}건" if self._items else "삭제한 상품이 없습니다.")
        self._update_buttons()

    def _checked(self) -> list[DeletedItem]:
        return [self._items[r] for r in range(self.table.rowCount())
                if self.table.item(r, 0).checkState() == Qt.CheckState.Checked]

    def _update_buttons(self) -> None:
        checked = self._checked()
        restorable = [i for i in checked if i.in_excel]
        self.restore_btn.setText(f"선택 상품 다시 등록 ({len(restorable)})" if restorable else "선택 상품 다시 등록")
        self.restore_btn.setEnabled(bool(restorable))
        self.forget_btn.setText(f"내역에서 지우기 ({len(checked)})" if checked else "내역에서 지우기")
        self.forget_btn.setEnabled(bool(checked))
        self.all_btn.setEnabled(bool(self._items))

    def on_toggle_all(self) -> None:
        state = Qt.CheckState.Unchecked if len(self._checked()) == len(self._items) else Qt.CheckState.Checked
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            self.table.item(r, 0).setCheckState(state)
        self.table.blockSignals(False)
        self._update_buttons()

    # --- 다시 등록 ---
    def on_restore(self) -> None:
        items = [i for i in self._checked() if i.in_excel]
        skipped = [i.code for i in self._checked() if not i.in_excel]
        if not items:
            return
        codes = [i.code for i in items]
        text = (f"{len(codes)}개 상품을 판매중지 상태로 다시 등록합니다: {', '.join(codes)}\n\n"
                f"작업 엑셀의 상품 정보와 사진 폴더를 그대로 쓰고, 새 상품번호가 붙습니다.")
        if skipped:
            text += f"\n\n엑셀에 없어 제외: {', '.join(skipped)}"
        if QMessageBox.question(self, "다시 등록", text + "\n\n계속할까요?") != QMessageBox.StandardButton.Yes:
            return
        self.restore_btn.setEnabled(False)
        self.forget_btn.setEnabled(False)
        self.info.setText("다시 등록하는 중...")
        run_with_progress(
            lambda report: reregister(self.services, codes, on_progress=lambda c, i, n: report((c, i, n))),
            lambda data: self.info.setText(f"다시 등록하는 중... ({data[1]}/{data[2]}) {data[0]}"),
            self._on_restored, self._on_failed)

    def _on_restored(self, report) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("다시 등록 결과")
        ok = report.registered_count == len(report.prepared)
        box.setIcon(QMessageBox.Icon.Information if ok else QMessageBox.Icon.Warning)
        box.setText(f"다시 등록 {report.registered_count}건 / 대상 {len(report.prepared)}건\n"
                    f"등록된 상품은 검수 대기 탭에서 확인하세요.")
        box.setDetailedText(format_report(report))
        box.exec()
        self.reload()

    def _on_failed(self, exc: Exception) -> None:
        self.reload()
        plain = isinstance(exc, (AlreadyRunningError, NotConfiguredError, FileNotFoundError, PermissionError,
                                 ValueError))
        QMessageBox.critical(self, "다시 등록 실패", str(exc) if plain else describe_error(exc))

    # --- 내역에서 지우기 ---
    def on_forget(self) -> None:
        codes = [i.code for i in self._checked()]
        if not codes or QMessageBox.question(
                self, "내역에서 지우기",
                f"{', '.join(codes)}\n\n삭제 내역 목록에서만 지웁니다. 스마트스토어와 작업 엑셀에는 영향이 없습니다. 계속할까요?") \
                != QMessageBox.StandardButton.Yes:
            return
        count = forget_deleted(self.services.db, codes)
        self.reload()
        self.info.setText(f"{count}건을 삭제 내역에서 지웠습니다.")
