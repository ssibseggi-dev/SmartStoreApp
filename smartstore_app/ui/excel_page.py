"""엑셀 일괄 등록 — 양식 다운로드 → 불러오기·검증·미리보기 → 일괄 등록 → 행별 결과."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from services.bulk_register import RowResult, register_rows
from services.connection import AppServices
from services.excel_import import ExcelRow, read_workbook, write_results, write_template
from services.product_builder import ProductDraft
from ui.messages import describe_error
from ui.workers import run_async, run_with_progress
from ui.theme import set_role

_COLUMNS = ["엑셀행", "상품키", "상품명", "판매가", "재고", "옵션", "이미지", "테스트", "검증", "결과", "원상품번호"]
_COLOR_ERROR = QColor("#FEF3F2")
_COLOR_SUCCESS = QColor("#ECFDF3")
_COLOR_FAILED = QColor("#FFFAEB")
_EXCEL_FILTER = "엑셀 파일 (*.xlsx)"


class ExcelPage(QWidget):
    open_in_editor = Signal(object)  # ProductDraft

    def __init__(self, services: AppServices, parent=None):
        super().__init__(parent)
        self.services = services
        self._path: Path | None = None
        self._rows: list[ExcelRow] = []
        self._results: dict[int, RowResult] = {}
        self._cancel = threading.Event()
        self._running = False

        self.template_btn = QPushButton("양식 다운로드")
        self.open_btn = QPushButton("엑셀 불러오기")
        self.reload_btn = QPushButton("다시 읽기")
        self.clear_btn = QPushButton("불러온 파일 닫기")
        self.register_btn = set_role(QPushButton("일괄 등록"), "primary")
        self.stop_btn = set_role(QPushButton("중지"), "danger")
        self.save_btn = QPushButton("결과 저장")
        self.template_btn.clicked.connect(self.on_template)
        self.open_btn.clicked.connect(self.on_open)
        self.reload_btn.clicked.connect(lambda: self._load(self._path) if self._path else None)
        self.clear_btn.clicked.connect(self.on_clear)
        self.register_btn.clicked.connect(self.on_register)
        self.stop_btn.clicked.connect(self.on_stop)
        self.save_btn.clicked.connect(self.on_save_results)
        top = QHBoxLayout()
        for b in (self.template_btn, self.open_btn, self.reload_btn, self.clear_btn):
            top.addWidget(b)
        top.addStretch()
        for b in (self.register_btn, self.stop_btn, self.save_btn):
            top.addWidget(b)

        self.file_label = QLabel("양식을 내려받아 작성한 뒤 '엑셀 불러오기'를 누르세요.")
        self.progress = QProgressBar()
        self.progress.setVisible(False)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._show_detail)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("행을 선택하면 검증 오류·등록 결과가 여기에 표시됩니다.")
        self.edit_btn = QPushButton("선택 행을 상품 등록 화면에서 열기")
        self.edit_btn.clicked.connect(self.on_open_in_editor)
        detail_box = QWidget()
        dl = QVBoxLayout(detail_box)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self.detail)
        dl.addWidget(self.edit_btn, 0, Qt.AlignmentFlag.AlignLeft)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(detail_box)
        splitter.setSizes([420, 140])

        self.summary = QLabel()

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.file_label)
        layout.addWidget(self.progress)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.summary)
        self._update_buttons()

    # --- 상태 ---
    def _pending_rows(self) -> list[ExcelRow]:
        """검증을 통과했고 아직 성공하지 않은 행."""
        return [r for r in self._rows if r.ok and not (r.row_no in self._results and self._results[r.row_no].ok)]

    def _update_buttons(self) -> None:
        running = self._running
        for b in (self.template_btn, self.open_btn):
            b.setEnabled(not running)
        self.reload_btn.setEnabled(not running and self._path is not None)
        self.clear_btn.setEnabled(not running and self._path is not None)
        self.register_btn.setEnabled(not running and bool(self._pending_rows()))
        self.stop_btn.setEnabled(running)
        self.save_btn.setEnabled(not running and bool(self._rows))
        self.edit_btn.setEnabled(not running and self.table.currentRow() >= 0)

    def _update_summary(self) -> None:
        valid = sum(1 for r in self._rows if r.ok)
        ok = sum(1 for r in self._results.values() if r.ok)
        failed = sum(1 for r in self._results.values() if not r.ok)
        self.summary.setText(f"전체 {len(self._rows)}행 · 검증 통과 {valid} · 오류 {len(self._rows) - valid}"
                             f" · 등록 성공 {ok} · 등록 실패 {failed}")

    # --- 양식 / 불러오기 ---
    def on_template(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "양식 저장", "스마트스토어_일괄등록_양식.xlsx", _EXCEL_FILTER)
        if not path:
            return
        try:
            write_template(path, self.services.db)
        except OSError as e:
            QMessageBox.critical(self, "양식 저장 실패", f"파일을 저장하지 못했습니다. 엑셀에서 열려 있다면 닫아주세요.\n{e}")
            return
        self.file_label.setText(f"양식을 저장했습니다: {path}")

    def on_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "엑셀 불러오기", "", _EXCEL_FILTER)
        if path:
            self._load(Path(path))

    def _confirm_discard_results(self, title: str) -> bool:
        """등록 결과가 있으면 화면에서 지워진다는 확인을 받는다."""
        if not any(r.ok for r in self._results.values()):
            return True
        return QMessageBox.question(
            self, title, "현재 등록 결과가 화면에서 지워집니다. 필요하면 먼저 '결과 저장'을 하세요.\n계속할까요?") \
            == QMessageBox.StandardButton.Yes

    def on_clear(self) -> None:
        """잘못 불러온 파일을 목록에서 내린다. PC의 엑셀 파일은 지우지 않는다."""
        if not self._confirm_discard_results("불러온 파일 닫기"):
            return
        self._path, self._rows, self._results = None, [], {}
        self.table.setRowCount(0)
        self.detail.clear()
        self.file_label.setText("불러온 파일을 닫았습니다. (PC의 엑셀 파일은 그대로입니다)")
        self.summary.clear()
        self._update_buttons()

    def _load(self, path: Path) -> None:
        if not self._confirm_discard_results("다시 읽기"):
            return
        self.file_label.setText(f"읽는 중: {path}")
        run_async(lambda: read_workbook(path, self.services.db),
                  lambda rows: self._on_loaded(path, rows), self._on_load_failed)

    def _on_loaded(self, path: Path, rows: list[ExcelRow]) -> None:
        self._path, self._rows, self._results = path, rows, {}
        self.file_label.setText(f"파일: {path}")
        self._fill_table()
        if not rows:
            QMessageBox.information(self, "엑셀", "'상품' 시트에 입력된 상품이 없습니다.")

    def _on_load_failed(self, exc: Exception) -> None:
        self.file_label.setText("엑셀을 읽지 못했습니다.")
        msg = describe_error(exc) if not isinstance(exc, PermissionError) else \
            "파일을 열 수 없습니다. 엑셀에서 열려 있다면 닫고 다시 시도하세요."
        QMessageBox.critical(self, "엑셀 읽기 실패", msg)
        self._update_buttons()

    # --- 표 ---
    def _fill_table(self) -> None:
        self.table.setRowCount(len(self._rows))
        for i, row in enumerate(self._rows):
            self._fill_row(i, row)
        self._update_summary()
        self._update_buttons()

    def _fill_row(self, i: int, row: ExcelRow) -> None:
        d = row.draft
        result = self._results.get(row.row_no)
        values = [
            str(row.row_no), row.key, d.name, f"{d.sale_price:,}", f"{d.effective_stock():,}",
            f"{len(d.option_rows)}개" if d.use_options else "-",
            str(1 + len(d.optional_images)) if d.representative_image else "0",
            "Y" if d.test_mode else "N",
            "OK" if row.ok else f"오류 {len(row.errors)}건",
            "" if result is None else ("성공" if result.ok else "실패"),
            str(result.origin_product_no or "") if result else "",
        ]
        color = (_COLOR_ERROR if not row.ok else _COLOR_SUCCESS if result and result.ok
                 else _COLOR_FAILED if result else None)
        for c, v in enumerate(values):
            item = QTableWidgetItem(v)
            if c in (3, 4):
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if color:
                item.setBackground(color)
            self.table.setItem(i, c, item)

    def _show_detail(self) -> None:
        self._update_buttons()
        i = self.table.currentRow()
        if not (0 <= i < len(self._rows)):
            self.detail.clear()
            return
        row = self._rows[i]
        lines = [f"엑셀 {row.row_no}행 — {row.draft.name}", f"카테고리: {row.draft.category_name or row.draft.leaf_category_id}"]
        if row.errors:
            lines += ["", "검증 오류:"] + [f"• {e}" for e in row.errors]
        result = self._results.get(row.row_no)
        if result:
            lines += ["", f"등록 {'성공' if result.ok else '실패'}"
                      + (f" — 원상품번호 {result.origin_product_no}" if result.origin_product_no else "")]
            if result.message:
                lines.append(result.message)
        self.detail.setPlainText("\n".join(lines))

    # --- 일괄 등록 ---
    def on_register(self) -> None:
        targets = self._pending_rows()
        if not targets:
            return
        live = sum(1 for r in targets if not r.draft.test_mode)
        errors = sum(1 for r in self._rows if not r.ok)
        msg = f"{len(targets)}개 상품을 스마트스토어에 등록합니다."
        if errors:
            msg += f"\n검증 오류가 있는 {errors}개 행은 건너뜁니다."
        if live:
            msg += f"\n\n테스트등록=N인 {live}개 상품은 바로 판매·전시됩니다."
        if QMessageBox.question(self, "일괄 등록", msg + "\n\n계속할까요?") != QMessageBox.StandardButton.Yes:
            return
        self._cancel.clear()
        self._running = True
        self.progress.setRange(0, len(targets))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self._update_buttons()
        run_with_progress(
            lambda report: register_rows(self.services, targets,
                                         on_progress=lambda res, done, total: report((res, done, total)),
                                         is_cancelled=self._cancel.is_set),
            self._on_progress, self._on_finished, self._on_failed)

    def on_stop(self) -> None:
        self._cancel.set()
        self.stop_btn.setEnabled(False)
        self.file_label.setText("현재 상품까지 처리하고 중지합니다...")

    def _on_progress(self, data) -> None:
        result, done, total = data
        self._results[result.row_no] = result
        self.progress.setValue(done)
        self.progress.setFormat(f"{done}/{total}")
        i = next(i for i, r in enumerate(self._rows) if r.row_no == result.row_no)
        self._fill_row(i, self._rows[i])
        self._update_summary()

    def _on_finished(self, results: list[RowResult]) -> None:
        self._running = False
        self.progress.setVisible(False)
        self._update_buttons()
        self._show_detail()
        ok = sum(1 for r in results if r.ok)
        stopped = " (중지됨)" if self._cancel.is_set() else ""
        self.file_label.setText(f"일괄 등록 완료{stopped}: 성공 {ok}, 실패 {len(results) - ok}")
        QMessageBox.information(self, "일괄 등록", f"일괄 등록{stopped}\n성공 {ok}개, 실패 {len(results) - ok}개\n\n"
                                "'결과 저장'으로 행별 결과를 엑셀로 저장할 수 있습니다.")

    def _on_failed(self, exc: Exception) -> None:
        self._running = False
        self.progress.setVisible(False)
        self._update_buttons()
        QMessageBox.critical(self, "일괄 등록 중단", describe_error(exc))

    # --- 결과 저장 / 편집 ---
    def on_save_results(self) -> None:
        default = (self._path.with_name(self._path.stem + "_결과.xlsx").name if self._path else "등록결과.xlsx")
        path, _ = QFileDialog.getSaveFileName(self, "결과 저장", default, _EXCEL_FILTER)
        if not path:
            return
        try:
            write_results(path, self._rows, {k: v.as_dict() for k, v in self._results.items()})
        except OSError as e:
            QMessageBox.critical(self, "결과 저장 실패", f"파일을 저장하지 못했습니다. 엑셀에서 열려 있다면 닫아주세요.\n{e}")
            return
        self.file_label.setText(f"결과를 저장했습니다: {path}")

    def on_open_in_editor(self) -> None:
        i = self.table.currentRow()
        if 0 <= i < len(self._rows):
            self.open_in_editor.emit(ProductDraft.from_dict(self._rows[i].draft.to_dict()))
