"""검수 대기 — 자동화로 등록한 '등록완료(판매중지)' 상품을 확인하고 골라서 판매 시작."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from services.automation.config import load_settings, save_settings
from services.product_manager import ProductEdit, load_edit
from services.automation.review import PublishResult, ReviewItem, delete_registered, pending_review, publish
from services.automation.run_lock import AlreadyRunningError
from services.connection import AppServices, NotConfiguredError
from ui.ai_draft_dialog import AiDraftDialog
from ui.messages import describe_error
from ui.product_edit_dialog import ProductEditDialog
from ui.theme import set_role
from ui.workers import run_async, run_with_progress

_COLUMNS = ["", "사진", "상품코드", "상품명", "판매가", "검수", "등록 시각", "등록 메모"]
_THUMB = 72


class ReviewPage(QWidget):
    def __init__(self, services: AppServices, parent=None):
        super().__init__(parent)
        self.services = services
        self._items: list[ReviewItem] = []

        self.info = QLabel()
        self.info.setObjectName("hint")
        refresh_btn = QPushButton("새로고침")
        refresh_btn.clicked.connect(self.reload)
        self.shopping_check = QCheckBox("판매 시작할 때 네이버쇼핑에도 노출")
        self.shopping_check.toggled.connect(self._save_shopping_option)
        top = QHBoxLayout()
        top.addWidget(self.info, 1)
        top.addWidget(self.shopping_check)
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
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(1, _THUMB + 16)
        self.table.setColumnWidth(7, 220)
        self.table.cellDoubleClicked.connect(lambda row, _c: self._open_detail(row))
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.currentCellChanged.connect(lambda *_a: self._update_buttons())

        self.all_btn = QPushButton("전체 선택")
        self.preview_btn = QPushButton("상세페이지 보기")
        self.ai_btn = QPushButton("AI 초안 보기")
        self.edit_btn = QPushButton("선택 상품 수정")
        self.edit_btn.clicked.connect(self.on_edit)
        self.ai_btn.clicked.connect(self.on_show_ai_draft)
        self.publish_btn = set_role(QPushButton("선택 상품 판매 시작"), "primary")
        self.all_btn.clicked.connect(self.on_toggle_all)
        self.preview_btn.clicked.connect(lambda: self._open_detail(self._target_row()))
        self.publish_btn.clicked.connect(self.on_publish)
        self.delete_btn = set_role(QPushButton("선택 상품 삭제"), "danger")
        self.delete_btn.clicked.connect(self.on_delete)
        hint = QLabel("사진·상품명·가격을 확인하고 체크한 뒤 '판매 시작'을 누르면 판매중 + 전시중으로 바뀝니다. "
                      "행을 더블클릭하면 등록한 상세페이지를 브라우저로 엽니다. "
                      "상품명·가격·재고·옵션별 재고를 고치려면 행을 고르고 '선택 상품 수정'을 누르세요. "
                      "'AI 초안 · 검수 필요' 상품은 'AI 초안 보기'에서 다른 상품명 후보와 검색 태그를 볼 수 있습니다.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        bottom = QHBoxLayout()
        bottom.addWidget(self.all_btn)
        bottom.addWidget(self.preview_btn)
        bottom.addWidget(self.edit_btn)
        bottom.addWidget(self.ai_btn)
        bottom.addStretch()
        bottom.addWidget(self.delete_btn)
        bottom.addWidget(self.publish_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addWidget(hint)
        layout.addLayout(bottom)
        self.reload()

    def showEvent(self, event) -> None:  # 탭을 열 때마다 최신 상태로
        super().showEvent(event)
        self.reload()

    # --- 목록 ---
    def reload(self) -> None:
        self.shopping_check.blockSignals(True)
        self.shopping_check.setChecked(load_settings(self.services.db).publish_naver_shopping)
        self.shopping_check.blockSignals(False)
        self._items = pending_review(self.services.db)
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
                    icon.addPixmap(thumb, QIcon.Mode.Selected)   # 선택된 행에서 사진이 초록빛으로 물들지 않게
                    photo.setIcon(icon)
            self.table.setItem(r, 1, photo)
            values = [item.code, item.name, f"{item.sale_price:,}원" if item.sale_price else "-",
                      "AI 초안 · 검수 필요" if item.ai_draft else "자동 생성", item.registered_at or "", item.message]
            for c, value in enumerate(values, start=2):
                cell = QTableWidgetItem(value)
                if c == 4:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if c == 5 and item.ai_draft:
                    cell.setForeground(QColor("#B54708"))
                if c == 7:
                    cell.setToolTip(item.message)
                self.table.setItem(r, c, cell)
        self.table.blockSignals(False)
        self.info.setText(f"검수 대기 {len(self._items)}건 — 자동화로 판매중지 상태로 등록한 상품입니다."
                          if self._items else "검수 대기 상품이 없습니다. (자동화 설정 → 대기 상품 처리로 등록)")
        self._update_buttons()

    def _checked_codes(self) -> list[str]:
        return [self._items[r].code for r in range(self.table.rowCount())
                if self.table.item(r, 0).checkState() == Qt.CheckState.Checked]

    def _update_buttons(self) -> None:
        count = len(self._checked_codes())
        self.publish_btn.setText(f"선택 상품 판매 시작 ({count})" if count else "선택 상품 판매 시작")
        self.publish_btn.setEnabled(count > 0)
        self.delete_btn.setText(f"선택 상품 삭제 ({count})" if count else "선택 상품 삭제")
        self.delete_btn.setEnabled(count > 0)
        self.all_btn.setEnabled(bool(self._items))
        self.preview_btn.setEnabled(bool(self._items))
        row = self._target_row()
        target = self._items[row] if 0 <= row < len(self._items) else None
        self.ai_btn.setEnabled(bool(target and target.ai_draft))
        self.edit_btn.setEnabled(target is not None)
        self.edit_btn.setText(f"{target.code} 수정" if target else "선택 상품 수정")   # 무엇을 고치는지 버튼에 표시

    def _target_row(self) -> int:
        """수정·상세페이지·AI 초안 보기의 대상 행: 체크한 상품이 하나면 그 행, 아니면 선택(파란 줄)한 행."""
        checked = [r for r in range(self.table.rowCount())
                   if self.table.item(r, 0) and self.table.item(r, 0).checkState() == Qt.CheckState.Checked]
        if len(checked) == 1:
            return checked[0]
        return self.table.currentRow()

    def _on_item_changed(self, item) -> None:
        if item.column() == 0 and item.checkState() == Qt.CheckState.Checked:
            self.table.blockSignals(True)
            self.table.setCurrentCell(item.row(), 2)   # 체크한 행을 선택 줄로도 맞춰 헷갈리지 않게
            self.table.blockSignals(False)
        self._update_buttons()

    def on_toggle_all(self) -> None:
        state = Qt.CheckState.Unchecked if len(self._checked_codes()) == len(self._items) else Qt.CheckState.Checked
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            self.table.item(r, 0).setCheckState(state)
        self.table.blockSignals(False)
        self._update_buttons()

    def _save_shopping_option(self, on: bool) -> None:
        settings = load_settings(self.services.db)
        settings.publish_naver_shopping = on
        save_settings(self.services.db, settings)

    def _open_detail(self, row: int) -> None:
        if not (0 <= row < len(self._items)):
            return
        page = self._items[row].detail_page
        if page is None:
            QMessageBox.information(self, "상세페이지", "등록한 상세페이지 파일이 없습니다.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(page)))

    def on_show_ai_draft(self) -> None:
        row = self._target_row()
        if 0 <= row < len(self._items):
            AiDraftDialog(self.services.db, self._items[row].code, self).exec()

    # --- 수정 ---
    def on_edit(self) -> None:
        row = self._target_row()
        if not (0 <= row < len(self._items)):
            QMessageBox.information(self, "상품 수정", "목록에서 고칠 상품을 고르세요.")
            return
        item = self._items[row]
        self.edit_btn.setEnabled(False)
        self.info.setText(f"{item.code} 상품 정보를 스마트스토어에서 불러오는 중...")
        run_async(lambda: load_edit(self.services, item.origin_product_no),
                  lambda edit: self._open_editor(item.code, edit), self._on_load_failed)

    def _open_editor(self, code: str, edit: ProductEdit) -> None:
        self._update_buttons()
        dialog = ProductEditDialog(self.services, edit, self, lock_status=True,
                                   banned_words=load_settings(self.services.db).banned_words)
        if dialog.exec() and dialog.saved_actions:
            self.services.db.update_automation_display(code, edit.name, edit.sale_price)
            self.reload()
            self.info.setText(f"{code} 저장 완료: {', '.join(dialog.saved_actions)}")
        else:
            self.reload()

    def _on_load_failed(self, exc: Exception) -> None:
        self.reload()
        QMessageBox.critical(self, "상품 수정", describe_error(exc))

    # --- 판매 시작 ---
    def on_publish(self) -> None:
        codes = self._checked_codes()
        if not codes:
            return
        shopping = " + 네이버쇼핑 노출" if self.shopping_check.isChecked() else ""
        if QMessageBox.question(
                self, "판매 시작",
                f"선택한 {len(codes)}개 상품을 판매중 + 전시중{shopping}으로 바꿉니다.\n"
                f"바로 고객에게 노출·판매됩니다. 계속할까요?") != QMessageBox.StandardButton.Yes:
            return
        self.publish_btn.setEnabled(False)
        self.info.setText("판매 시작 처리 중...")
        run_with_progress(
            lambda report: publish(self.services, codes, on_progress=lambda c, i, n: report((c, i, n))),
            lambda data: self.info.setText(f"판매 시작 처리 중... ({data[1]}/{data[2]}) {data[0]}"),
            self._on_published, self._on_failed)

    def _on_published(self, result: tuple[list[PublishResult], bool]) -> None:
        results, excel_written = result
        ok = [r for r in results if r.ok]
        lines = [f"{r.code}: {'판매 시작' if r.ok else r.message}" for r in results]
        text = f"판매 시작 {len(ok)}건 / 실패 {len(results) - len(ok)}건"
        if not excel_written:
            text += "\n작업 엑셀이 열려 있어 결과는 DB에 보관했고 다음 실행 때 엑셀에 반영합니다."
        box = QMessageBox(self)
        box.setWindowTitle("판매 시작 결과")
        box.setIcon(QMessageBox.Icon.Information if len(ok) == len(results) else QMessageBox.Icon.Warning)
        box.setText(text)
        box.setDetailedText("\n".join(lines))
        box.exec()
        self.reload()

    def _on_failed(self, exc: Exception, title: str = "판매 시작 실패") -> None:
        self.reload()
        plain = isinstance(exc, (AlreadyRunningError, NotConfiguredError, FileNotFoundError))
        QMessageBox.critical(self, title, str(exc) if plain else describe_error(exc))

    # --- 삭제 ---
    def on_delete(self) -> None:
        codes = self._checked_codes()
        if not codes:
            return
        names = "\n".join(f"· {i.code}  {i.name}" for i in self._items if i.code in codes)
        shown = ", ".join(codes[:20]) + (f" 외 {len(codes) - 20}개" if len(codes) > 20 else "")
        box = QMessageBox(QMessageBox.Icon.Warning, "선택 상품 삭제",
                          f"선택한 {len(codes)}개 상품을 스마트스토어에서 삭제합니다.\n\n"
                          f"삭제할 상품코드: {shown}\n\n"
                          f"삭제한 상품은 되돌릴 수 없습니다. 상품코드를 확인하고 계속할까요?", parent=self)
        box.setDetailedText(names)
        yes = box.addButton(f"{len(codes)}개 삭제", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not yes:
            return
        self.delete_btn.setEnabled(False)
        self.publish_btn.setEnabled(False)
        self.info.setText("삭제하는 중...")
        run_with_progress(
            lambda report: delete_registered(self.services, codes, on_progress=lambda c, i, n: report((c, i, n))),
            lambda data: self.info.setText(f"삭제하는 중... ({data[1]}/{data[2]}) {data[0]}"),
            self._on_deleted, lambda e: self._on_failed(e, "삭제 실패"))

    def _on_deleted(self, result: tuple[list[PublishResult], bool]) -> None:
        results, excel_written = result
        ok = [r for r in results if r.ok]
        text = f"삭제 {len(ok)}건 / 실패 {len(results) - len(ok)}건"
        if not excel_written:
            text += "\n작업 엑셀이 열려 있어 '삭제됨' 표시는 DB에 보관했고 다음 실행 때 엑셀에 반영합니다."
        box = QMessageBox(self)
        box.setWindowTitle("삭제 결과")
        box.setIcon(QMessageBox.Icon.Information if len(ok) == len(results) else QMessageBox.Icon.Warning)
        box.setText(text)
        box.setDetailedText("\n".join(f"{r.code}: {r.message}" for r in results))
        box.exec()
        self.reload()
        self.info.setText(f"{len(ok)}개 상품을 삭제했습니다." if ok else self.info.text())
