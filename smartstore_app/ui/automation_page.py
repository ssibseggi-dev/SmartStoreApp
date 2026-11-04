"""자동화 설정 — 공통 설정(수수료·끝자리·금지어·한도·경로), 사진·상세페이지·AI 초안, 분류 매핑 표, 검증 미리보기."""
from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from services import ai_draft, detail_page, templates
from services.automation.config import (
    AutomationSettings, CategoryMapping, load_mappings, load_settings, parse_banned_words, save_mappings,
    save_settings,
)
from services.automation.input_sheet import STATUS_FAILED, write_template
from services.automation.naming import NAME_TOKENS, unknown_tokens
from services.automation.pricing import calculate_price
from services.automation.run_lock import AlreadyRunningError
from services.automation.runner import (
    format_drafts, format_report, make_drafts, preview_pages, process_images, run_waiting,
)
from services.connection import AppServices, NotConfiguredError
from services.notify.dispatch import notify_run
from ui.ai_draft_dialog import AiDraftDialog
from ui.messages import describe_error
from ui.photo_intake_dialog import PhotoIntakeDialog
from ui.product_page import CategoryPickerDialog
from ui.theme import set_role
from ui.workers import run_async, run_with_progress

# 매핑 표 열: (헤더, CategoryMapping 필드 또는 특수 키)
_COLUMNS = [("분류명", "name"), ("카테고리ID", "category_id"), ("카테고리", "_category"), ("마진율(%)", "margin_rate"),
            ("상품명 규칙", "name_rule"), ("브랜드", "brand"), ("핵심키워드", "keyword"),
            ("배송", "delivery_template"), ("반품", "claim_template"), ("A/S", "as_template"),
            ("고시", "notice_template"), ("상세 템플릿", "detail_template")]
_COL = {key: i for i, (_, key) in enumerate(_COLUMNS)}
_TEMPLATE_KIND = {"delivery_template": "delivery", "claim_template": "claim", "as_template": "as",
                  "notice_template": "notice"}
_DEFAULT_LABEL = "(기본)"


class AutomationPage(QWidget):
    def __init__(self, services: AppServices, parent=None):
        super().__init__(parent)
        self.services = services
        self.db = services.db

        # --- 공통 설정 ---
        self.fee_spin = QDoubleSpinBox()
        self.fee_spin.setRange(0, 50)
        self.fee_spin.setDecimals(2)
        self.fee_spin.setSuffix(" %")
        self.round_combo = QComboBox()
        for unit in (10, 100, 1000):
            self.round_combo.addItem(f"{unit:,}원 단위 올림", unit)
        self.ending_spin = QSpinBox()
        self.ending_spin.setRange(0, 990)
        self.ending_spin.setSingleStep(100)
        self.ending_spin.setSpecialValueText("사용 안 함")
        self.daily_spin = QSpinBox()
        self.daily_spin.setRange(1, 1000)
        self.daily_spin.setSuffix(" 건")
        self.banned_edit = QPlainTextEdit()
        self.banned_edit.setPlaceholderText("쉼표 또는 줄바꿈으로 구분. 예: 최저가, 정품, 1위")
        self.banned_edit.setFixedHeight(64)
        self.live_check = QCheckBox("판매중으로 바로 등록 (끄면 판매중지로 등록 후 검수 — 권장)")
        self.live_check.toggled.connect(self._warn_live)

        self.excel_edit = QLineEdit()
        self.image_edit = QLineEdit()
        excel_btn = QPushButton("찾아보기")
        image_btn = QPushButton("찾아보기")
        excel_btn.clicked.connect(self.on_pick_excel)
        image_btn.clicked.connect(self.on_pick_image_root)
        self.template_btn = QPushButton("양식 만들기")
        self.template_btn.clicked.connect(self.on_make_template)
        excel_row = QHBoxLayout()
        excel_row.addWidget(self.excel_edit, 1)
        excel_row.addWidget(excel_btn)
        excel_row.addWidget(self.template_btn)
        image_row = QHBoxLayout()
        image_row.addWidget(self.image_edit, 1)
        image_row.addWidget(image_btn)

        common = QGroupBox("공통 설정")
        f = QFormLayout(common)
        f.addRow("판매 수수료율", self.fee_spin)
        f.addRow("판매가 올림", self.round_combo)
        f.addRow("판매가 끝자리", self.ending_spin)
        f.addRow("하루 최대 등록 수", self.daily_spin)
        f.addRow("상품명 금지어", self.banned_edit)
        f.addRow("작업 엑셀", excel_row)
        f.addRow("사진 폴더", image_row)
        f.addRow("", self.live_check)

        # --- 판매가 계산 미리보기 ---
        self.calc_cost = QSpinBox()
        self.calc_cost.setRange(0, 99_999_990)
        self.calc_cost.setSingleStep(1000)
        self.calc_cost.setGroupSeparatorShown(True)
        self.calc_cost.setSuffix(" 원")
        self.calc_cost.setValue(10000)
        self.calc_margin = QDoubleSpinBox()
        self.calc_margin.setRange(0, 1000)
        self.calc_margin.setSuffix(" %")
        self.calc_margin.setValue(30)
        self.calc_result = QLabel()
        self.calc_result.setObjectName("hint")
        self.calc_result.setWordWrap(True)
        for w in (self.fee_spin, self.ending_spin, self.calc_cost, self.calc_margin):
            w.valueChanged.connect(self._update_calc)
        self.round_combo.currentIndexChanged.connect(self._update_calc)
        calc_row = QHBoxLayout()
        calc_row.addWidget(QLabel("원가"))
        calc_row.addWidget(self.calc_cost)
        calc_row.addWidget(QLabel("마진율"))
        calc_row.addWidget(self.calc_margin)
        calc_row.addStretch()
        calc = QGroupBox("판매가 계산 미리보기")
        v = QVBoxLayout(calc)
        v.addLayout(calc_row)
        v.addWidget(self.calc_result)

        # --- 사진 보정 ---
        self.square_combo = QComboBox()
        self.square_combo.addItem("여백 채우기 (사진 전체 보이게)", "pad")
        self.square_combo.addItem("가운데 크롭 (꽉 차게)", "crop")
        self.enhance_check = QCheckBox("자동 밝기·화이트밸런스 보정")
        self.bg_check = QCheckBox("대표 이미지 배경 제거 → 흰 배경")
        self.bg_quality_combo = QComboBox()
        self.bg_quality_combo.addItem("최고 품질 (권장, 상품당 약 11초)", "high")
        self.bg_quality_combo.addItem("보통 (약 6초, 복잡한 배경은 조금 남을 수 있음)", "normal")
        self.bg_quality_combo.addItem("빠르게 (약 1초, 소매 틈·가장자리가 덜 깔끔)", "fast")
        self.bg_check.toggled.connect(self.bg_quality_combo.setEnabled)
        self.photo_btn = QPushButton("대기 상품 사진 보정")
        self.photo_btn.clicked.connect(self.on_process_photos)
        self.intake_btn = set_role(QPushButton("상품 사진 넣기"), "primary")
        self.intake_btn.clicked.connect(self.on_photo_intake)
        photo_hint = QLabel("'상품 사진 넣기'로 상품코드별 대표·추가·상세 사진을 고르면 사진 폴더에 규칙대로 들어갑니다. "
                            "원본/ 사진은 그대로 두고 보정/ 폴더에 1000×1000으로 저장합니다. "
                            "등록할 때 자동으로 보정하며, 이미 보정한 사진은 다시 처리하지 않습니다. "
                            "배경 제거는 처음 사용할 때 모델(최고 품질 약 970MB)을 한 번 내려받습니다.")
        photo_hint.setObjectName("hint")
        photo_hint.setWordWrap(True)
        photo = QGroupBox("사진 보정")
        f = QFormLayout(photo)
        f.addRow("정사각형", self.square_combo)
        f.addRow("", self.enhance_check)
        f.addRow("", self.bg_check)
        f.addRow("배경 제거 품질", self.bg_quality_combo)
        f.addRow(photo_hint)
        photo_buttons = QHBoxLayout()
        photo_buttons.addWidget(self.intake_btn)
        photo_buttons.addWidget(self.photo_btn)
        f.addRow(photo_buttons)

        # --- 상세페이지 ---
        self.notice_edit = QLineEdit()
        self.notice_edit.setPlaceholderText("선택 — 넣으면 공지 문구 대신 이 이미지를 씀")
        notice_btn = QPushButton("찾아보기")
        notice_btn.clicked.connect(self.on_pick_notice)
        self.notice_clear_btn = set_role(QPushButton("삭제"), "danger")
        self.notice_clear_btn.clicked.connect(self.on_clear_notice)
        self.notice_edit.textChanged.connect(lambda t: self.notice_clear_btn.setEnabled(bool(t.strip())))
        notice_row = QHBoxLayout()
        notice_row.addWidget(self.notice_edit, 1)
        notice_row.addWidget(notice_btn)
        notice_row.addWidget(self.notice_clear_btn)
        template_btn = QPushButton("템플릿 폴더 열기")
        template_btn.clicked.connect(self.on_open_templates)
        self.page_preview_btn = QPushButton("대기 상품 상세페이지 미리보기")
        self.page_preview_btn.clicked.connect(self.on_preview_pages)
        page_hint = QLabel("구성: 상단 공지 → 설명 → 옵션 → 사진 → 사이즈표 → 소재·세탁 → 배송·교환 안내. "
                           "분류별 디자인은 템플릿 폴더의 '기본.html'을 복사해 고친 뒤 분류 매핑의 '상세 템플릿'에 이름을 적습니다.")
        page_hint.setObjectName("hint")
        page_hint.setWordWrap(True)
        page = QGroupBox("상세페이지")
        f = QFormLayout(page)
        self.notice_title_edit = QLineEdit()
        self.notice_title_edit.setPlaceholderText("예: 오늘 주문 시 내일 출발")
        self.notice_subtitle_edit = QLineEdit()
        self.notice_subtitle_edit.setPlaceholderText("예: 5만원 이상 구매 시 무료배송")
        f.addRow("공지 문구", self.notice_title_edit)
        f.addRow("공지 보조 문구", self.notice_subtitle_edit)
        f.addRow("공지 이미지", notice_row)
        f.addRow(page_hint)
        buttons = QHBoxLayout()
        buttons.addWidget(template_btn)
        buttons.addWidget(self.page_preview_btn)
        f.addRow(buttons)

        # --- AI 초안 ---
        self.ai_check = QCheckBox("상품명(최종)·설명(최종)이 비면 AI 초안으로 채우기")
        self.ai_key_status = QLabel()
        self.ai_key_status.setObjectName("hint")
        self.ai_key_status.setWordWrap(True)
        self.ai_key_edit = QLineEdit()
        self.ai_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.ai_key_edit.setPlaceholderText("sk-ant-...  (Windows 자격 증명 관리자에만 보관)")
        self.ai_key_save_btn = QPushButton("키 확인·저장")
        self.ai_key_delete_btn = set_role(QPushButton("키 삭제"), "danger")
        self.ai_key_save_btn.clicked.connect(self.on_save_ai_key)
        self.ai_key_delete_btn.clicked.connect(self.on_delete_ai_key)
        key_buttons = QHBoxLayout()
        key_buttons.addWidget(self.ai_key_status, 1)
        key_buttons.addWidget(self.ai_key_save_btn)
        key_buttons.addWidget(self.ai_key_delete_btn)
        self.ai_btn = QPushButton("대기 상품 AI 초안 만들기")
        self.ai_btn.clicked.connect(self.on_make_drafts)
        self.ai_view_btn = QPushButton("저장된 AI 초안 보기")
        self.ai_view_btn.clicked.connect(lambda: AiDraftDialog(self.db, parent=self).exec())
        ai_buttons = QHBoxLayout()
        ai_buttons.addWidget(self.ai_view_btn)
        ai_buttons.addWidget(self.ai_btn)
        ai_hint = QLabel("보정한 사진 2~4장과 엑셀 입력값을 Claude에 보내 상품명 후보 3개·특징·검색 태그·색상을 받습니다. "
                         "비어 있는 칸만 채우고 사람이 적은 값은 바꾸지 않습니다. 사진으로 확인할 수 없는 소재·효능·원산지는 "
                         "쓰지 않게 했지만 꼭 검수하세요. 상품마다 한 번만 호출하고 결과를 보관하며, '저장된 AI 초안 보기'에서 "
                         "다른 후보를 골라 엑셀에 적을 수 있습니다.")
        ai_hint.setObjectName("hint")
        ai_hint.setWordWrap(True)
        ai = QGroupBox("AI 초안 (Claude)")
        f = QFormLayout(ai)
        f.addRow("", self.ai_check)
        f.addRow("API 키", self.ai_key_edit)
        f.addRow("", key_buttons)
        f.addRow(ai_hint)
        f.addRow(ai_buttons)

        # --- 분류 매핑 ---
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels([h for h, _ in _COLUMNS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setMinimumHeight(240)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for key, width in (("name", 110), ("category_id", 90), ("_category", 200), ("margin_rate", 80),
                           ("name_rule", 230), ("brand", 90), ("keyword", 110), ("detail_template", 90)):
            self.table.setColumnWidth(_COL[key], width)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.itemChanged.connect(self._on_item_changed)

        add_btn = QPushButton("분류 추가")
        remove_btn = set_role(QPushButton("선택 분류 삭제"), "danger")
        pick_btn = QPushButton("카테고리 찾기")
        add_btn.clicked.connect(lambda: self._add_row(CategoryMapping(name="")))
        remove_btn.clicked.connect(self.on_remove_rows)
        pick_btn.clicked.connect(lambda: self._pick_category(self.table.currentRow()))
        table_buttons = QHBoxLayout()
        for b in (add_btn, pick_btn, remove_btn):
            table_buttons.addWidget(b)
        table_buttons.addStretch()
        hint = QLabel("상품명 규칙에 쓸 수 있는 항목: " + " ".join("{" + t + "}" for t in NAME_TOKENS)
                      + " · 카테고리ID 칸을 더블클릭하면 카테고리를 찾을 수 있습니다. · 템플릿 (기본)은 ★ 기본 템플릿")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        mapping = QGroupBox("분류 매핑")
        v = QVBoxLayout(mapping)
        v.addLayout(table_buttons)
        v.addWidget(self.table)
        v.addWidget(hint)

        # --- 하단 버튼 ---
        self.save_btn = QPushButton("설정 저장")
        self.preview_btn = QPushButton("대기 상품 검증 미리보기")
        self.process_btn = set_role(QPushButton("대기 상품 처리"), "primary")
        self.save_btn.clicked.connect(self.on_save)
        self.preview_btn.clicked.connect(self.on_preview)
        self.process_btn.clicked.connect(self.on_process)
        self.status = QLabel()
        self.status.setObjectName("statusText")
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.save_btn)
        bottom.addWidget(self.preview_btn)
        bottom.addWidget(self.process_btn)

        body = QWidget()
        body.setObjectName("formBody")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 8, 8)
        top = QHBoxLayout()
        top.setSpacing(14)
        # 두 열 높이가 비슷하도록: 왼쪽 = 공통·상세페이지, 오른쪽 = 판매가·사진·AI 초안
        left = QVBoxLayout()
        left.addWidget(common)
        left.addWidget(page, 1)   # 오른쪽 열과 아래 끝을 맞춘다
        right = QVBoxLayout()
        right.addWidget(calc)
        right.addWidget(photo)
        right.addWidget(ai)
        right.addStretch()
        top.addLayout(left, 1)
        top.addLayout(right, 1)
        bl.addLayout(top)
        bl.addWidget(mapping)
        bl.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addLayout(bottom)

        self._loading = False
        self.load()

    # --- 불러오기 / 저장 ---
    def load(self) -> None:
        s = load_settings(self.db)
        self.fee_spin.setValue(s.fee_rate)
        self.round_combo.setCurrentIndex(max(0, self.round_combo.findData(s.round_unit)))
        self.ending_spin.setValue(s.price_ending)
        self.daily_spin.setValue(s.daily_limit)
        self.banned_edit.setPlainText(", ".join(s.banned_words))
        self.excel_edit.setText(str(s.work_excel_path()))
        self.image_edit.setText(str(s.image_root_path()))
        self.live_check.blockSignals(True)
        self.live_check.setChecked(s.live_publish)
        self.live_check.blockSignals(False)
        self.square_combo.setCurrentIndex(max(0, self.square_combo.findData(s.image_square_mode)))
        self.enhance_check.setChecked(s.image_enhance)
        self.bg_check.setChecked(s.remove_background)
        self.bg_quality_combo.setCurrentIndex(max(0, self.bg_quality_combo.findData(s.bg_quality)))
        self.bg_quality_combo.setEnabled(s.remove_background)
        self.notice_edit.setText(s.notice_image)
        self.notice_clear_btn.setEnabled(bool(s.notice_image.strip()))
        self.notice_title_edit.setText(s.notice_title)
        self.notice_subtitle_edit.setText(s.notice_subtitle)
        self.ai_check.setChecked(s.ai_enabled)
        self._update_ai_key_status()
        self._loading = True
        self.table.setRowCount(0)
        for m in load_mappings(self.db).values():
            self._add_row(m)
        self._loading = False
        self._update_calc()

    def _collect_settings(self) -> AutomationSettings:
        # 화면에 없는 값(배경 제거 모델 등)은 저장된 값을 유지
        return replace(
            load_settings(self.db),
            image_square_mode=self.square_combo.currentData(),
            image_enhance=self.enhance_check.isChecked(),
            remove_background=self.bg_check.isChecked(),
            bg_quality=self.bg_quality_combo.currentData(),
            notice_image=self.notice_edit.text().strip(),
            notice_title=self.notice_title_edit.text().strip(),
            notice_subtitle=self.notice_subtitle_edit.text().strip(),
            fee_rate=self.fee_spin.value(),
            round_unit=self.round_combo.currentData(),
            price_ending=self.ending_spin.value() // 10 * 10,
            banned_words=parse_banned_words(self.banned_edit.toPlainText()),
            daily_limit=self.daily_spin.value(),
            work_excel=self.excel_edit.text().strip(),
            image_root=self.image_edit.text().strip(),
            live_publish=self.live_check.isChecked(),
            ai_enabled=self.ai_check.isChecked(),
        )

    def on_save(self) -> bool:
        mappings = [self._row_mapping(r) for r in range(self.table.rowCount())]
        errors = []
        for m in mappings:
            bad = unknown_tokens(m.name_rule)
            if bad:
                errors.append(f"'{m.name or '(이름 없음)'}' 상품명 규칙에 알 수 없는 항목: {', '.join(bad)}")
        if errors:
            QMessageBox.warning(self, "설정 확인", "\n".join(errors))
            return False
        try:
            save_mappings(self.db, mappings)
        except ValueError as e:
            QMessageBox.warning(self, "설정 확인", str(e))
            return False
        save_settings(self.db, self._collect_settings())
        self.status.setText(f"저장했습니다. (분류 {len(mappings)}개)")
        return True

    # --- 경로 / 양식 ---
    def on_pick_excel(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "작업 엑셀 위치", self.excel_edit.text(), "엑셀 파일 (*.xlsx)",
                                              options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self.excel_edit.setText(path)

    def on_pick_image_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "사진 폴더", self.image_edit.text())
        if path:
            self.image_edit.setText(path)

    def on_make_template(self) -> None:
        if not self.on_save():
            return
        target = Path(self.excel_edit.text().strip() or load_settings(self.db).work_excel_path())
        if target.exists():
            # 입력한 내용을 덮어쓰지 않도록 다른 이름으로 저장
            path, _ = QFileDialog.getSaveFileName(self, "작업 엑셀이 이미 있습니다 — 새 양식 저장 위치",
                                                  str(target.with_name(target.stem + "_새양식.xlsx")),
                                                  "엑셀 파일 (*.xlsx)")
            if not path:
                return
            target = Path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            write_template(target, list(load_mappings(self.db)))
        except OSError as e:
            QMessageBox.critical(self, "양식 만들기 실패", f"저장하지 못했습니다. 엑셀에서 열려 있다면 닫아주세요.\n{e}")
            return
        self.status.setText(f"양식을 만들었습니다: {target}")

    # --- 판매가 계산 ---
    def _update_calc(self) -> None:
        try:
            r = calculate_price(self.calc_cost.value(), self.calc_margin.value(), self.fee_spin.value(),
                                self.round_combo.currentData() or 100, self.ending_spin.value() // 10 * 10)
            self.calc_result.setText(f"<b>판매가 {r.price:,}원</b><br>{r.explain()}")
        except ValueError as e:
            self.calc_result.setText(str(e))

    def _warn_live(self, on: bool) -> None:
        if on and QMessageBox.question(
                self, "바로 판매", "자동화로 등록한 상품이 검수 없이 바로 판매·노출됩니다. 켤까요?") \
                != QMessageBox.StandardButton.Yes:
            self.live_check.setChecked(False)

    # --- 매핑 표 ---
    def _template_combo(self, kind: str, current: str) -> QComboBox:
        combo = QComboBox()
        combo.addItem(_DEFAULT_LABEL, "")
        for t in templates.list_templates(self.db, kind):
            combo.addItem(t.name, t.name)
        if current and combo.findData(current) < 0:
            combo.addItem(f"{current} (없음)", current)  # 삭제된 템플릿도 보이게
        combo.setCurrentIndex(max(0, combo.findData(current)))
        return combo

    def _add_row(self, m: CategoryMapping) -> None:
        loading, self._loading = self._loading, True
        r = self.table.rowCount()
        self.table.insertRow(r)
        for key, col in _COL.items():
            if key in _TEMPLATE_KIND:
                self.table.setCellWidget(r, col, self._template_combo(_TEMPLATE_KIND[key], getattr(m, key)))
            elif key == "_category":
                item = QTableWidgetItem(self._category_name(m.category_id))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(r, col, item)
            elif key == "margin_rate":
                item = QTableWidgetItem()
                item.setData(Qt.ItemDataRole.EditRole, float(m.margin_rate))
                self.table.setItem(r, col, item)
            else:
                self.table.setItem(r, col, QTableWidgetItem(str(getattr(m, key))))
        self._loading = loading
        if not loading:
            self.table.setCurrentCell(r, _COL["name"])
            self.table.editItem(self.table.item(r, _COL["name"]))

    def _row_mapping(self, r: int) -> CategoryMapping:
        def text(key: str) -> str:
            item = self.table.item(r, _COL[key])
            return item.text().strip() if item else ""
        values = {key: text(key) for key in ("name", "category_id", "name_rule", "brand", "keyword", "detail_template")}
        values["margin_rate"] = float(self.table.item(r, _COL["margin_rate"]).data(Qt.ItemDataRole.EditRole) or 0)
        for key in _TEMPLATE_KIND:
            values[key] = self.table.cellWidget(r, _COL[key]).currentData()
        values["name_rule"] = values["name_rule"] or "{원본상품명}"
        values["detail_template"] = values["detail_template"] or "기본"
        return CategoryMapping(**values)

    def _category_name(self, category_id: str) -> str:
        if not category_id:
            return ""
        c = self.db.get_category(category_id)
        if c is None:
            return "(목록에 없음)" if self.db.category_count() else ""
        return c.whole_name + ("" if c.last else "  ※ 최하위 아님")

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if not self._loading and item.column() == _COL["category_id"]:
            self._loading = True
            self.table.item(item.row(), _COL["_category"]).setText(self._category_name(item.text().strip()))
            self._loading = False

    def _on_cell_double_clicked(self, row: int, col: int) -> None:
        if col in (_COL["category_id"], _COL["_category"]):
            self._pick_category(row)

    def _pick_category(self, row: int) -> None:
        if row < 0:
            QMessageBox.information(self, "카테고리 찾기", "표에서 분류 행을 먼저 선택하세요.")
            return
        dialog = CategoryPickerDialog(self.services, self)
        if dialog.exec() and dialog.selected:
            self.table.item(row, _COL["category_id"]).setText(dialog.selected.id)

    def on_remove_rows(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        names = ", ".join(self.table.item(r, _COL["name"]).text() or "(이름 없음)" for r in rows)
        if QMessageBox.question(self, "분류 삭제", f"{names} 분류를 표에서 지울까요?\n('설정 저장'을 눌러야 반영됩니다)") \
                == QMessageBox.StandardButton.Yes:
            for r in rows:
                self.table.removeRow(r)

    # --- 검증 미리보기 (dry-run) / 대기 상품 처리 ---
    def _set_running(self, running: bool) -> None:
        for b in (self.preview_btn, self.process_btn, self.save_btn, self.photo_btn, self.page_preview_btn, self.intake_btn,
                  self.ai_btn, self.ai_key_save_btn, self.ai_key_delete_btn):
            b.setEnabled(not running)
        if not running:
            self._update_ai_key_status()   # 키가 없으면 AI 버튼은 꺼 둔다

    def on_photo_intake(self) -> None:
        if not self.on_save():   # 사진 폴더 경로를 바꿨으면 먼저 저장
            return
        dialog = PhotoIntakeDialog(self.db, self)
        if dialog.exec() and dialog.saved:
            self.status.setText(f"상품 사진 {len(dialog.saved.saved)}장 저장: {dialog.saved.folder}")

    def on_process_photos(self) -> None:
        if not self.on_save():
            return
        self._set_running(True)
        self.status.setText("사진 보정 중...")
        run_with_progress(
            lambda report: process_images(self.db, on_progress=lambda code, i, n: report((code, i, n))),
            lambda data: self.status.setText(f"사진 보정 중... ({data[1]}/{data[2]}) {data[0]}"),
            self._on_photos_done, lambda e: self._on_run_failed("사진 보정 실패", e))

    def _on_photos_done(self, results) -> None:
        self._set_running(False)
        done = sum(len(r.processed) for r in results)
        skipped = sum(r.skipped for r in results)
        self.status.setText(f"사진 보정 완료: {len(results)}개 상품 · 보정 {done}장 · 건너뜀 {skipped}장")
        lines = [f"{r.code}: {r.summary()}" + "".join(f"\n    [오류] {e}" for e in r.errors) for r in results]
        box = QMessageBox(self)
        box.setWindowTitle("사진 보정 결과")
        box.setText(f"{len(results)}개 상품 · 보정 {done}장 · 건너뜀 {skipped}장\n\n"
                    f"보정본은 각 상품 폴더의 '보정' 폴더에 있습니다.")
        box.setDetailedText("\n".join(lines) or "대기 상품이 없습니다.")
        open_btn = box.addButton("사진 폴더 열기", QMessageBox.ButtonRole.ActionRole)
        box.addButton("닫기", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(load_settings(self.db).image_root_path())))

    def on_preview(self) -> None:
        if not self.on_save():
            return
        self._set_running(True)
        self.status.setText("작업 엑셀의 대기 상품을 검증하는 중...")
        run_async(lambda: run_waiting(self.services, dry_run=True), self._on_preview_done,
                  lambda e: self._on_run_failed("검증 실패", e))

    def _on_preview_done(self, report) -> None:
        self._set_running(False)
        self.status.setText(f"검증 완료: 통과 {len(report.ok_items)} / 실패 {len(report.failed_items)}")
        self._show_report("대기 상품 검증 미리보기 (API 호출 없음)", report)

    def on_process(self) -> None:
        if not self.on_save():
            return
        s = load_settings(self.db)
        mode = "판매중으로 바로 등록(검수 없음)" if s.live_publish else "판매중지로 등록(검수 후 판매 시작)"
        if QMessageBox.question(
                self, "대기 상품 처리",
                f"작업 엑셀의 '대기' 상품을 스마트스토어에 등록합니다.\n\n"
                f"· 등록 방식: {mode}\n· 하루 최대 {s.daily_limit}건 (넘는 상품은 '대기' 유지)\n"
                f"· 검증에 실패한 상품은 등록하지 않고 '실패'로 기록\n\n계속할까요?") != QMessageBox.StandardButton.Yes:
            return
        self._set_running(True)
        self.status.setText("대기 상품 처리 중...")
        run_with_progress(
            lambda report: self._run_and_notify(lambda code, i, n: report((code, i, n))),
            lambda data: self.status.setText(f"대기 상품 처리 중... ({data[1]}/{data[2]}) {data[0]}"),
            self._on_process_done, lambda e: self._on_run_failed("대기 상품 처리 실패", e))

    def _run_and_notify(self, on_progress):
        report = run_waiting(self.services, dry_run=False, on_progress=on_progress)
        report.notify_status = notify_run(self.db, report, "app")   # 알림 실패는 등록 결과에 영향 없음
        return report

    def _on_process_done(self, report) -> None:
        self._set_running(False)
        self.status.setText(f"처리 완료: 등록 {report.registered_count} / 실패 {report.count(STATUS_FAILED)}"
                            + ("" if report.excel_written else " · 엑셀이 열려 있어 결과는 DB에 보관"))
        self._show_report("대기 상품 처리 결과", report, getattr(report, "notify_status", None))

    def _show_report(self, title: str, report, extra: str | None = None) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(900, 600)
        text = QPlainTextEdit(format_report(report) + (f"\n{extra}" if extra else ""))
        text.setReadOnly(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addWidget(text)
        layout.addWidget(buttons)
        dialog.exec()

    def _on_run_failed(self, title: str, exc: Exception) -> None:
        self._set_running(False)
        self.status.setText(title)
        plain = isinstance(exc, (FileNotFoundError, ValueError, PermissionError, AlreadyRunningError,
                                 NotConfiguredError, ai_draft.AiDraftError))
        QMessageBox.critical(self, title, str(exc) if plain else describe_error(exc))

    # --- AI 초안 ---
    def _update_ai_key_status(self) -> None:
        if ai_draft.has_api_key():
            self.ai_key_status.setText("키 저장됨 · 모델 " + load_settings(self.db).ai_model)
        else:
            self.ai_key_status.setText("키 없음 (AI 초안 건너뜀)")
        self.ai_key_delete_btn.setEnabled(ai_draft.has_api_key())
        self.ai_btn.setEnabled(ai_draft.has_api_key())

    def on_save_ai_key(self) -> None:
        key = self.ai_key_edit.text().strip()
        if not key:
            QMessageBox.information(self, "Claude API 키", "키를 입력하세요. (Claude Console의 API Keys에서 만듭니다)")
            return
        self._set_running(True)
        self.ai_key_status.setText("키 확인 중...")
        model = load_settings(self.db).ai_model
        run_async(lambda: ai_draft.check_api_key(key, model), lambda name: self._on_ai_key_ok(key, name),
                  self._on_ai_key_failed)

    def _on_ai_key_ok(self, key: str, model_name: str) -> None:
        ai_draft.save_api_key(key)
        self.ai_key_edit.clear()
        self._set_running(False)
        self._update_ai_key_status()
        self.status.setText(f"Claude API 키를 확인하고 저장했습니다. ({model_name})")

    def _on_ai_key_failed(self, exc: Exception) -> None:
        self._set_running(False)
        self._update_ai_key_status()
        QMessageBox.warning(self, "Claude API 키", str(exc) if isinstance(exc, ai_draft.AiDraftError) else describe_error(exc))

    def on_delete_ai_key(self) -> None:
        if QMessageBox.question(self, "Claude API 키", "저장된 Claude API 키를 지울까요?") == QMessageBox.StandardButton.Yes:
            ai_draft.delete_api_key()
            self._update_ai_key_status()

    def on_make_drafts(self) -> None:
        if not self.on_save():
            return
        self._set_running(True)
        self.status.setText("AI 초안 만드는 중...")
        run_with_progress(
            lambda report: make_drafts(self.db, on_progress=lambda code, i, n: report((code, i, n))),
            lambda data: self.status.setText(f"AI 초안 만드는 중... ({data[1]}/{data[2]}) {data[0]}"),
            self._on_drafts_done, lambda e: self._on_run_failed("AI 초안 실패", e))

    def _on_drafts_done(self, outcome) -> None:
        results, excel_error = outcome
        self._set_running(False)
        self._update_ai_key_status()
        if not results:
            self.status.setText("AI 초안을 만들 상품이 없습니다.")
            QMessageBox.information(self, "AI 초안", "'대기' 상품 중 상품명(최종)·설명(최종)이 빈 상품이 없습니다.")
            return
        failed = sum(1 for r in results if r.error)
        self.status.setText(f"AI 초안 {len(results) - failed}건 완료" + (f" · 실패 {failed}건" if failed else ""))
        self._show_text("AI 초안 결과 (검수 필요)",
                        format_drafts(results, load_settings(self.db).banned_words, excel_error)
                        + "\n\n다시 보려면 '저장된 AI 초안 보기'를 누르세요. 다른 상품명 후보도 거기서 고를 수 있습니다.")

    # --- 상세페이지 ---
    def on_pick_notice(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "상단 공지 이미지", self.notice_edit.text(),
                                              "이미지 (*.jpg *.jpeg *.png *.gif)")
        if path:
            self.notice_edit.setText(path)

    def on_clear_notice(self) -> None:
        """공지 이미지 지정을 지우고 바로 저장한다 (이미지 파일 자체는 지우지 않음)."""
        if not self.notice_edit.text().strip():
            return
        self.notice_edit.clear()
        if self.on_save():
            self.status.setText("공지 이미지를 뺐습니다. 이제 상세페이지에 공지 이미지가 들어가지 않습니다. (파일은 그대로)")

    def on_open_templates(self) -> None:
        """사용자 템플릿 폴더를 만들고(처음이면 기본 템플릿을 예시로 복사) 연다."""
        folder = detail_page.user_template_dir(self._collect_settings())
        folder.mkdir(parents=True, exist_ok=True)
        example = folder / "기본_예시.html"
        if not any(folder.glob("*.html")):
            shutil.copy(detail_page.builtin_template_dir() / f"{detail_page.DEFAULT_TEMPLATE}.html", example)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def on_preview_pages(self) -> None:
        if not self.on_save():
            return
        self._set_running(True)
        self.status.setText("상세페이지 미리보기를 만드는 중 (사진 보정 포함)...")
        run_with_progress(
            lambda report: preview_pages(self.db, on_progress=lambda code, i, n: report((code, i, n))),
            lambda data: self.status.setText(f"상세페이지 미리보기 만드는 중... ({data[1]}/{data[2]}) {data[0]}"),
            self._on_pages_done, lambda e: self._on_run_failed("상세페이지 미리보기 실패", e))

    def _on_pages_done(self, results) -> None:
        self._set_running(False)
        made = [p for p in results if p.preview]
        self.status.setText(f"상세페이지 미리보기 {len(made)}/{len(results)}개 생성")
        if not results:
            QMessageBox.information(self, "상세페이지 미리보기", "작업 엑셀에 '대기' 상품이 없습니다.")
            return
        lines = [f"{p.code}: {'생성' if p.preview else '실패'}" + "".join(f"\n    [오류] {e}" for e in p.errors)
                 for p in results]
        box = QMessageBox(self)
        box.setWindowTitle("상세페이지 미리보기")
        box.setText(f"{len(made)}/{len(results)}개 상품의 미리보기를 만들었습니다.\n"
                    f"브라우저로 열어 확인하세요. (실패한 상품은 '자세히 보기'에 사유가 있습니다)")
        box.setDetailedText("\n".join(lines))
        open_btn = box.addButton("첫 상품 열기", QMessageBox.ButtonRole.ActionRole) if made else None
        folder_btn = box.addButton("미리보기 폴더 열기", QMessageBox.ButtonRole.ActionRole) if made else None
        box.addButton("닫기", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if made and box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(made[0].preview)))
        elif made and box.clickedButton() is folder_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(made[0].preview.parent)))
