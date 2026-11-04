"""촬영 사진 — 상품코드 QR 카드 출력, 폰 사진 동기화 폴더 설정, QR 카드 기준 자동 분리."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from services.automation.config import load_settings, save_settings
from services.automation.input_sheet import STATUS_WAITING, read_items
from services.automation.run_lock import RunLock
from services.automation.runner import lock_path
from services.connection import AppServices
from services.images.photo_sort import UNSORTED_DIR, SortReport, sort_photos
from services.images.qr_cards import make_pdf
from ui.theme import set_role
from ui.workers import run_async, run_with_progress


class PhotoInboxPage(QWidget):
    def __init__(self, services: AppServices, parent=None):
        super().__init__(parent)
        self.services = services
        self.db = services.db

        # --- 1. QR 카드 ---
        self.cards = QTableWidget(0, 3)
        self.cards.setHorizontalHeaderLabels(["", "상품코드", "상품명"])
        self.cards.verticalHeader().setVisible(False)
        self.cards.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cards.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.cards.setShowGrid(False)
        self.cards.setAlternatingRowColors(True)
        self.cards.setMinimumHeight(180)
        header = self.cards.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        reload_btn = QPushButton("엑셀에서 다시 불러오기")
        reload_btn.clicked.connect(self.load_cards)
        self.pdf_btn = set_role(QPushButton("선택 상품 QR 카드 PDF 만들기"), "primary")
        self.pdf_btn.clicked.connect(self.on_make_pdf)
        card_hint = QLabel("작업 엑셀에서 '대기'이면서 입력방식이 '촬영'인 상품입니다. A4 한 장에 8장씩 인쇄해 잘라 쓰세요. "
                           "촬영할 때 카드를 먼저 한 장 찍고 이어서 그 상품 사진을 찍으면 됩니다.")
        card_hint.setObjectName("hint")
        card_hint.setWordWrap(True)
        card_buttons = QHBoxLayout()
        card_buttons.addWidget(reload_btn)
        card_buttons.addStretch()
        card_buttons.addWidget(self.pdf_btn)
        cards_box = QGroupBox("① 상품코드 QR 카드 출력")
        v = QVBoxLayout(cards_box)
        v.addWidget(self.cards)
        v.addWidget(card_hint)
        v.addLayout(card_buttons)

        # --- 2. 사진 분리 ---
        self.inbox_edit = QLineEdit()
        self.inbox_edit.setPlaceholderText("예: C:\\Users\\...\\OneDrive\\사진\\카메라 앨범 (구글 드라이브·OneDrive 동기화 폴더)")
        inbox_btn = QPushButton("찾아보기")
        inbox_btn.clicked.connect(self.on_pick_inbox)
        inbox_row = QHBoxLayout()
        inbox_row.addWidget(self.inbox_edit, 1)
        inbox_row.addWidget(inbox_btn)
        self.gap_spin = QSpinBox()
        self.gap_spin.setRange(1, 48)
        self.gap_spin.setSuffix(" 시간")
        self.sort_btn = set_role(QPushButton("지금 사진 분리"), "primary")
        unsorted_btn = QPushButton("미분류 폴더 열기")
        photos_btn = QPushButton("사진 폴더 열기")
        self.sort_btn.clicked.connect(self.on_sort)
        unsorted_btn.clicked.connect(lambda: self._open_folder(UNSORTED_DIR))
        photos_btn.clicked.connect(lambda: self._open_folder(""))
        sort_buttons = QHBoxLayout()
        sort_buttons.addWidget(photos_btn)
        sort_buttons.addWidget(unsorted_btn)
        sort_buttons.addStretch()
        sort_buttons.addWidget(self.sort_btn)
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setPlaceholderText("분리 결과가 여기에 표시됩니다.")
        self.result.setMinimumHeight(120)
        sort_hint = QLabel("폰 사진은 복사만 하고 동기화 폴더에서 지우지 않습니다. 이미 처리한 사진은 다시 처리하지 않으며, "
                           "QR 카드 사진은 상품 사진으로 쓰지 않습니다. 카드 없이 찍었거나 카드 뒤 설정한 시간이 넘게 지난 사진은 "
                           "'미분류' 폴더로 갑니다. 폴더를 설정해 두면 '대기 상품 처리'·예약 실행 때 자동으로 먼저 분리합니다.")
        sort_hint.setObjectName("hint")
        sort_hint.setWordWrap(True)
        sort_box = QGroupBox("② 촬영 사진 자동 분리")
        f = QFormLayout(sort_box)
        f.addRow("촬영 사진 폴더", inbox_row)
        f.addRow("카드 뒤 유효 시간", self.gap_spin)
        f.addRow(sort_hint)
        f.addRow(sort_buttons)
        f.addRow(self.result)

        layout = QVBoxLayout(self)
        layout.addWidget(cards_box)
        layout.addWidget(sort_box, 1)
        s = load_settings(self.db)
        self.inbox_edit.setText(s.photo_inbox)
        self.gap_spin.setValue(s.photo_max_gap_hours)
        self.inbox_edit.editingFinished.connect(self._save)
        self.gap_spin.valueChanged.connect(self._save)
        self.load_cards()

    def _save(self) -> None:
        s = load_settings(self.db)
        s.photo_inbox, s.photo_max_gap_hours = self.inbox_edit.text().strip(), self.gap_spin.value()
        save_settings(self.db, s)

    # --- QR 카드 ---
    def load_cards(self) -> None:
        try:
            items = read_items(load_settings(self.db).work_excel_path())
        except (OSError, ValueError):
            items = []
        targets = [i for i in items if i.status == STATUS_WAITING and i.get("입력방식") == "촬영" and i.code]
        self.cards.setRowCount(len(targets))
        for r, item in enumerate(targets):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Checked)
            self.cards.setItem(r, 0, check)
            self.cards.setItem(r, 1, QTableWidgetItem(item.code))
            self.cards.setItem(r, 2, QTableWidgetItem(item.get("원본상품명")))
        self.pdf_btn.setEnabled(bool(targets))

    def _checked_cards(self) -> list[tuple[str, str]]:
        return [(self.cards.item(r, 1).text(), self.cards.item(r, 2).text()) for r in range(self.cards.rowCount())
                if self.cards.item(r, 0).checkState() == Qt.CheckState.Checked]

    def on_make_pdf(self) -> None:
        cards = self._checked_cards()
        if not cards:
            QMessageBox.information(self, "QR 카드", "카드를 만들 상품을 선택하세요.")
            return
        default = str(load_settings(self.db).work_excel_path().with_name("QR카드.pdf"))
        path, _ = QFileDialog.getSaveFileName(self, "QR 카드 PDF 저장", default, "PDF (*.pdf)")
        if not path:
            return
        try:
            make_pdf(cards, path)
        except OSError as e:
            QMessageBox.critical(self, "QR 카드", f"저장하지 못했습니다. PDF가 열려 있다면 닫아주세요.\n{e}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # --- 사진 분리 ---
    def on_pick_inbox(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "촬영 사진 폴더", self.inbox_edit.text())
        if path:
            self.inbox_edit.setText(path)
            self._save()

    def _open_folder(self, sub: str) -> None:
        folder = load_settings(self.db).image_root_path() / sub
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def on_sort(self) -> None:
        self._save()
        s = load_settings(self.db)
        if not s.photo_inbox:
            QMessageBox.information(self, "사진 분리", "촬영 사진 폴더를 먼저 지정하세요.")
            return
        self.sort_btn.setEnabled(False)
        self.result.setPlainText("사진을 읽는 중...")

        def work(report):
            with RunLock(lock_path(), "sort-photos"):
                known = None
                if s.work_excel_path().is_file():
                    known = {i.code for i in read_items(s.work_excel_path())}
                return sort_photos(self.db, Path(s.photo_inbox), s.image_root_path(), s.photo_max_gap_hours, known,
                                   on_progress=lambda name, i, n: report((name, i, n)))

        run_with_progress(work, lambda d: self.result.setPlainText(f"사진을 읽는 중... ({d[1]}/{d[2]}) {d[0]}"),
                          self._on_sorted, self._on_failed)

    def _on_sorted(self, report: SortReport) -> None:
        self.sort_btn.setEnabled(True)
        lines = [f"결과: {report.summary()} · 이전에 처리한 사진 {report.already}장", ""]
        for code, files in sorted(report.assigned.items()):
            lines.append(f"■ {code}  {len(files)}장  → {files[0].parent}")
        if report.unsorted:
            lines += ["", f"[주의] 미분류 {len(report.unsorted)}장 — QR 카드 없이 찍었거나 카드 뒤 시간이 많이 지난 사진입니다:"]
            lines += [f"    {p.name}" for p in report.unsorted[:20]]
            if len(report.unsorted) > 20:
                lines.append(f"    외 {len(report.unsorted) - 20}장")
        lines += [f"[주의] {w}" for w in report.warnings]
        if report.deferred:
            lines.append(f"방금 생긴 사진 {report.deferred}장은 동기화가 끝난 뒤 다음 실행 때 처리합니다.")
        if not report.copied and not report.qr_cards:
            lines.append("새로 분리할 사진이 없습니다.")
        self.result.setPlainText("\n".join(lines))

    def _on_failed(self, exc: Exception) -> None:
        self.sort_btn.setEnabled(True)
        self.result.setPlainText(f"사진 분리 실패: {exc}")
        QMessageBox.critical(self, "사진 분리", str(exc))
