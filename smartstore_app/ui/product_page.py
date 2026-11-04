"""상품 등록 화면 — 단일/조합형 옵션 상품, 템플릿 적용, 임시저장."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from api.categories import Category
from services import drafts, templates
from services.connection import AppServices
from services.product_builder import (
    DELIVERY_COMPANIES, DELIVERY_FEE_PAY_TYPES, DELIVERY_FEE_TYPES, NOTICE_DEFAULT, ORIGIN_AREAS,
    ProductDraft,
)
from services.product_service import RegistrationOutcome, register_product
from services.validator import MAX_OPTIONAL_IMAGES, validate
from ui.category_page import CategorySearchWidget
from ui.messages import describe_error
from ui.option_editor import OptionEditor
from ui.template_bar import TemplateBar
from ui.theme import set_role
from ui.workers import run_async

_IMAGE_FILTER = "이미지 (*.jpg *.jpeg *.png *.gif *.bmp)"


def _money_spin(maximum: int = 999_999_990, step: int = 100) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(0, maximum)
    spin.setSingleStep(step)
    spin.setGroupSeparatorShown(True)
    spin.setSuffix(" 원")
    return spin


def _combo(items: list[tuple[str, str]], editable: bool = False) -> QComboBox:
    combo = QComboBox()
    combo.setEditable(editable)
    combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    for label, code in items:
        combo.addItem(f"{label} ({code})" if editable else label, code)
    return combo


def _combo_value(combo: QComboBox) -> str:
    # 편집 가능 콤보에서 목록에 없는 코드를 직접 입력한 경우도 처리
    data = combo.currentData()
    if combo.isEditable() and (data is None or combo.currentText() != combo.itemText(combo.currentIndex())):
        return combo.currentText().strip()
    return data


def _set_combo_value(combo: QComboBox, code: str) -> None:
    index = combo.findData(code)
    if index >= 0:
        combo.setCurrentIndex(index)
    elif combo.isEditable():
        combo.setEditText(code)


class CategoryPickerDialog(QDialog):
    def __init__(self, services: AppServices, parent=None):
        super().__init__(parent)
        self.setWindowTitle("카테고리 선택")
        self.resize(800, 520)
        self.selected: Category | None = None
        self.search = CategorySearchWidget(services)
        self.search.category_selected.connect(self._accept_category)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("선택")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(lambda: self._accept_category(self.search.selected_category()))
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(buttons)

    def _accept_category(self, category: Category | None) -> None:
        if category is None:
            return
        if not category.last:
            QMessageBox.warning(self, "카테고리", "최하위 카테고리만 선택할 수 있습니다.")
            return
        self.selected = category
        self.accept()


class ProductPage(QWidget):
    drafts_changed = Signal()

    def __init__(self, services: AppServices, parent=None):
        super().__init__(parent)
        self.services = services
        self._category: Category | None = None
        self._draft_id: int | None = None
        db = services.db

        # --- 기본 정보 ---
        self.category_label = QLabel("선택 안 됨")
        category_btn = QPushButton("카테고리 선택")
        category_btn.clicked.connect(self.on_pick_category)
        category_row = QHBoxLayout()
        category_row.addWidget(self.category_label, 1)
        category_row.addWidget(category_btn)

        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(100)
        self.price_spin = _money_spin(step=1000)
        self.stock_spin = QSpinBox()
        self.stock_spin.setRange(0, 99_999_999)
        self.stock_spin.setGroupSeparatorShown(True)

        basic = QGroupBox("기본 정보")
        f = QFormLayout(basic)
        f.addRow("카테고리", category_row)
        f.addRow("상품명", self.name_edit)
        f.addRow("판매가", self.price_spin)
        f.addRow("재고수량", self.stock_spin)

        # --- 옵션 ---
        self.use_options_check = QCheckBox("조합형 옵션 사용 (재고는 옵션 재고 합계로 등록)")
        self.option_editor = OptionEditor()
        self.use_options_check.toggled.connect(self._update_option_state)
        self.option_editor.changed.connect(self._update_option_state)
        options = QGroupBox("옵션")
        v = QVBoxLayout(options)
        v.addWidget(self.use_options_check)
        v.addWidget(self.option_editor)

        # --- 이미지 ---
        self.rep_image_edit = QLineEdit()
        self.rep_image_edit.setReadOnly(True)
        rep_btn = QPushButton("찾아보기")
        rep_btn.clicked.connect(self.on_pick_rep_image)
        rep_row = QHBoxLayout()
        rep_row.addWidget(self.rep_image_edit, 1)
        rep_row.addWidget(rep_btn)

        self.opt_images = QListWidget()
        self.opt_images.setMaximumHeight(110)
        add_btn = QPushButton("추가")
        remove_btn = QPushButton("삭제")
        add_btn.clicked.connect(self.on_add_opt_images)
        remove_btn.clicked.connect(lambda: [self.opt_images.takeItem(self.opt_images.row(i))
                                            for i in self.opt_images.selectedItems()])
        opt_btns = QVBoxLayout()
        opt_btns.addWidget(add_btn)
        opt_btns.addWidget(remove_btn)
        opt_btns.addStretch()
        opt_row = QHBoxLayout()
        opt_row.addWidget(self.opt_images, 1)
        opt_row.addLayout(opt_btns)

        images = QGroupBox("이미지 (JPG·PNG·GIF·BMP, 10MB 이하)")
        f = QFormLayout(images)
        f.addRow("대표 이미지", rep_row)
        f.addRow(f"추가 이미지 (최대 {MAX_OPTIONAL_IMAGES})", opt_row)

        # --- 상세설명 ---
        self.detail_edit = QPlainTextEdit()
        self.detail_edit.setPlaceholderText("HTML 또는 일반 텍스트. 예: <p>상품 설명</p><img src=\"https://...\">")
        self.detail_edit.setMinimumHeight(140)
        detail = QGroupBox("상세설명")
        QVBoxLayout(detail).addWidget(self.detail_edit)

        # --- 배송 ---
        self.company_combo = _combo(DELIVERY_COMPANIES, editable=True)
        self.fee_type_combo = _combo(DELIVERY_FEE_TYPES)
        self.base_fee_spin = _money_spin(1_000_000)
        self.free_cond_spin = _money_spin(step=1000)
        self.pay_type_combo = _combo(DELIVERY_FEE_PAY_TYPES)
        self.fee_type_combo.currentIndexChanged.connect(self._update_fee_fields)
        delivery = QGroupBox("배송")
        f = QFormLayout(delivery)
        f.addRow(self._template_bar("delivery"))
        f.addRow("택배사", self.company_combo)
        f.addRow("배송비 유형", self.fee_type_combo)
        f.addRow("기본 배송비", self.base_fee_spin)
        f.addRow("무료 조건 금액", self.free_cond_spin)
        f.addRow("배송비 결제", self.pay_type_combo)

        # --- 반품/교환 ---
        self.return_fee_spin = _money_spin(1_000_000)
        self.exchange_fee_spin = _money_spin(1_000_000)
        claim = QGroupBox("반품 / 교환")
        f = QFormLayout(claim)
        f.addRow(self._template_bar("claim"))
        f.addRow("반품 배송비(편도)", self.return_fee_spin)
        f.addRow("교환 배송비(왕복)", self.exchange_fee_spin)

        # --- A/S ---
        self.as_phone_edit = QLineEdit()
        self.as_phone_edit.setPlaceholderText("예: 010-1234-5678")
        self.as_guide_edit = QLineEdit()
        self.as_guide_edit.setPlaceholderText(NOTICE_DEFAULT)
        after_service = QGroupBox("A/S")
        f = QFormLayout(after_service)
        f.addRow(self._template_bar("as"))
        f.addRow("A/S 전화번호", self.as_phone_edit)
        f.addRow("A/S 안내", self.as_guide_edit)

        # --- 고시정보·원산지 ---
        self.origin_combo = _combo(ORIGIN_AREAS, editable=True)
        self.origin_content_edit = QLineEdit()
        self.minor_check = QCheckBox("미성년자 구매 가능")
        self.notice_item_edit = QLineEdit()
        self.notice_model_edit = QLineEdit()
        self.notice_maker_edit = QLineEdit()
        notice = QGroupBox("상품정보제공고시(기타 재화) · 원산지")
        f = QFormLayout(notice)
        f.addRow(self._template_bar("notice"))
        f.addRow("품명", self.notice_item_edit)
        f.addRow("모델명", self.notice_model_edit)
        f.addRow("제조자(사)", self.notice_maker_edit)
        f.addRow("원산지 코드", self.origin_combo)
        f.addRow("원산지 표시 내용", self.origin_content_edit)
        f.addRow("", self.minor_check)

        # --- 등록 ---
        self.test_mode_check = QCheckBox("테스트 등록 (전시중지 + 등록 직후 판매중지로 전환)")
        self.naver_shopping_check = QCheckBox("네이버쇼핑 노출")
        self.test_mode_check.toggled.connect(lambda on: self.naver_shopping_check.setEnabled(not on))
        self.draft_label = QLabel()
        self.draft_label.setObjectName("hint")
        new_btn = QPushButton("새로 작성")
        save_draft_btn = QPushButton("임시저장")
        self.register_btn = set_role(QPushButton("상품 등록"), "primary", large=True)
        new_btn.clicked.connect(self.on_new)
        save_draft_btn.clicked.connect(self.on_save_draft)
        self.register_btn.clicked.connect(self.on_register)
        self.status = QLabel()
        self.status.setObjectName("statusText")
        self.status.setWordWrap(True)

        # 하단 고정 액션바
        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        checks = QVBoxLayout()
        checks.setSpacing(4)
        checks.addWidget(self.test_mode_check)
        checks.addWidget(self.naver_shopping_check)
        info = QVBoxLayout()
        info.setSpacing(2)
        info.addWidget(self.draft_label)
        info.addWidget(self.status)
        bar = QHBoxLayout(action_bar)
        bar.setContentsMargins(16, 10, 16, 10)
        bar.addLayout(checks)
        bar.addSpacing(16)
        bar.addLayout(info, 1)
        bar.addWidget(new_btn)
        bar.addWidget(save_draft_btn)
        bar.addWidget(self.register_btn)

        # 배송 | 반품·A/S 2열
        side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.addWidget(claim)
        side.addWidget(after_service)
        side.addStretch()
        columns = QHBoxLayout()
        columns.setSpacing(14)
        columns.addWidget(delivery, 1)
        columns.addLayout(side, 1)

        body = QWidget()
        body.setObjectName("formBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 8, 8)
        body_layout.setSpacing(4)
        for box in (basic, options, images, detail):
            body_layout.addWidget(box)
        body_layout.addLayout(columns)
        body_layout.addWidget(notice)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(body)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(self.scroll, 1)
        layout.addWidget(action_bar)

        self.load_draft(templates.apply_defaults(db, ProductDraft()))

    def _template_bar(self, kind: str) -> TemplateBar:
        return TemplateBar(kind, self.services.db, self.to_draft, self.load_draft)

    # --- 화면 상태 ---
    def _update_fee_fields(self) -> None:
        fee_type = self.fee_type_combo.currentData()
        self.base_fee_spin.setEnabled(fee_type != "FREE")
        self.pay_type_combo.setEnabled(fee_type != "FREE")
        self.free_cond_spin.setEnabled(fee_type == "CONDITIONAL_FREE")

    def _update_option_state(self) -> None:
        use = self.use_options_check.isChecked()
        self.option_editor.setVisible(use)
        self.stock_spin.setEnabled(not use)
        if use:
            self.stock_spin.setValue(self.to_draft().effective_stock())

    def _update_draft_label(self) -> None:
        self.draft_label.setText(f"임시저장 #{self._draft_id} 편집 중" if self._draft_id else "")

    # --- 입력 처리 ---
    def on_pick_category(self) -> None:
        dialog = CategoryPickerDialog(self.services, self)
        if dialog.exec() and dialog.selected:
            self._set_category(dialog.selected)

    def _set_category(self, category: Category | None) -> None:
        self._category = category
        self.category_label.setText(f"{category.whole_name}  ({category.id})".strip() if category else "선택 안 됨")

    def on_pick_rep_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "대표 이미지 선택", "", _IMAGE_FILTER)
        if path:
            self.rep_image_edit.setText(path)

    def on_add_opt_images(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "추가 이미지 선택", "", _IMAGE_FILTER)
        room = MAX_OPTIONAL_IMAGES - self.opt_images.count()
        self.opt_images.addItems(paths[:room])
        if len(paths) > room:
            QMessageBox.information(self, "추가 이미지", f"추가 이미지는 최대 {MAX_OPTIONAL_IMAGES}개까지입니다.")

    # --- 입력값 <-> ProductDraft ---
    def to_draft(self) -> ProductDraft:
        return ProductDraft(
            leaf_category_id=self._category.id if self._category else "",
            category_name=self._category.whole_name if self._category else "",
            name=self.name_edit.text(),
            sale_price=self.price_spin.value(),
            stock_quantity=self.stock_spin.value(),
            representative_image=self.rep_image_edit.text(),
            optional_images=[self.opt_images.item(i).text() for i in range(self.opt_images.count())],
            detail_html=self.detail_edit.toPlainText(),
            delivery_company=_combo_value(self.company_combo),
            delivery_fee_type=self.fee_type_combo.currentData(),
            base_fee=self.base_fee_spin.value(),
            free_condition_amount=self.free_cond_spin.value(),
            delivery_fee_pay_type=self.pay_type_combo.currentData(),
            return_fee=self.return_fee_spin.value(),
            exchange_fee=self.exchange_fee_spin.value(),
            as_phone=self.as_phone_edit.text(),
            as_guide=self.as_guide_edit.text(),
            origin_area_code=_combo_value(self.origin_combo),
            origin_content=self.origin_content_edit.text().strip(),
            minor_purchasable=self.minor_check.isChecked(),
            notice_item_name=self.notice_item_edit.text().strip(),
            notice_model_name=self.notice_model_edit.text().strip(),
            notice_manufacturer=self.notice_maker_edit.text().strip(),
            use_options=self.use_options_check.isChecked(),
            option_groups=self.option_editor.groups(),
            option_rows=self.option_editor.rows(),
            naver_shopping=self.naver_shopping_check.isChecked(),
            test_mode=self.test_mode_check.isChecked(),
        )

    def load_draft(self, d: ProductDraft) -> None:
        category = self.services.db.get_category(d.leaf_category_id) if d.leaf_category_id else None
        if d.leaf_category_id and category is None:
            category = Category(d.leaf_category_id, d.category_name, d.category_name, True)
        self._set_category(category)
        self.name_edit.setText(d.name)
        self.price_spin.setValue(d.sale_price)
        self.stock_spin.setValue(d.stock_quantity)
        self.rep_image_edit.setText(d.representative_image)
        self.opt_images.clear()
        self.opt_images.addItems(d.optional_images)
        self.detail_edit.setPlainText(d.detail_html)
        _set_combo_value(self.company_combo, d.delivery_company)
        _set_combo_value(self.fee_type_combo, d.delivery_fee_type)
        self.base_fee_spin.setValue(d.base_fee)
        self.free_cond_spin.setValue(d.free_condition_amount)
        _set_combo_value(self.pay_type_combo, d.delivery_fee_pay_type)
        self.return_fee_spin.setValue(d.return_fee)
        self.exchange_fee_spin.setValue(d.exchange_fee)
        self.as_phone_edit.setText(d.as_phone)
        self.as_guide_edit.setText(d.as_guide)
        _set_combo_value(self.origin_combo, d.origin_area_code)
        self.origin_content_edit.setText(d.origin_content)
        self.minor_check.setChecked(d.minor_purchasable)
        self.notice_item_edit.setText(d.notice_item_name)
        self.notice_model_edit.setText(d.notice_model_name)
        self.notice_maker_edit.setText(d.notice_manufacturer)
        self.option_editor.set_data(d.option_groups, d.option_rows)
        self.use_options_check.setChecked(d.use_options)
        self.naver_shopping_check.setChecked(d.naver_shopping)
        self.test_mode_check.setChecked(d.test_mode)
        self.naver_shopping_check.setEnabled(not d.test_mode)
        self._update_fee_fields()
        self._update_option_state()

    # --- 새로 작성 / 임시저장 ---
    def on_new(self) -> None:
        if QMessageBox.question(self, "새로 작성", "입력 중인 내용을 지우고 새로 작성할까요?") \
                != QMessageBox.StandardButton.Yes:
            return
        self._draft_id = None
        self._update_draft_label()
        self.load_draft(templates.apply_defaults(self.services.db, ProductDraft()))
        self.status.clear()

    def on_save_draft(self) -> None:
        self._draft_id = drafts.save(self.services.db, self._draft_id, self.to_draft())
        self._update_draft_label()
        self.status.setText(f"임시저장했습니다. (#{self._draft_id})")
        self.drafts_changed.emit()

    def open_draft(self, draft_id: int) -> None:
        d = drafts.load(self.services.db, draft_id)
        if d is None:
            QMessageBox.warning(self, "임시저장", "임시저장 항목을 찾을 수 없습니다.")
            return
        self._draft_id = draft_id
        self._update_draft_label()
        self.load_draft(d)
        self.scroll.verticalScrollBar().setValue(0)
        missing = drafts.missing_images(d)
        self.status.setText(f"임시저장 #{draft_id}을 불러왔습니다.")
        if missing:
            QMessageBox.warning(self, "이미지 확인", "다음 이미지 파일을 찾을 수 없습니다. 다시 선택하세요.\n\n"
                                + "\n".join(missing))

    def open_product(self, d: ProductDraft) -> None:
        """임시저장과 연결되지 않은 새 입력값으로 화면을 채운다(엑셀 행 열기 등)."""
        self._draft_id = None
        self._update_draft_label()
        self.load_draft(d)
        self.scroll.verticalScrollBar().setValue(0)
        self.status.setText("엑셀 행을 불러왔습니다. 수정 후 임시저장하거나 등록하세요.")

    # --- 등록 ---
    def on_register(self) -> None:
        draft = self.to_draft()
        errors = validate(draft)
        if errors:
            QMessageBox.warning(self, "입력 확인", "\n".join(f"• {e}" for e in errors))
            return
        mode = "테스트(판매중지)" if draft.test_mode else "판매중"
        option_text = f" / 옵션 {len(draft.option_rows):,}개" if draft.use_options else ""
        if QMessageBox.question(
                self, "상품 등록",
                f"'{draft.name.strip()}' 상품을 스마트스토어에 등록합니다.\n"
                f"판매가 {draft.sale_price:,}원 / 재고 {draft.effective_stock():,}개{option_text} / {mode}\n\n"
                f"계속할까요?") != QMessageBox.StandardButton.Yes:
            return
        self.register_btn.setEnabled(False)
        self.status.setText("이미지 업로드 및 상품 등록 중...")
        run_async(lambda: register_product(self.services, draft), self._on_registered, self._on_failed)

    def _on_registered(self, outcome: RegistrationOutcome) -> None:
        self.register_btn.setEnabled(True)
        status = "판매중지" if outcome.status == "SUSPENSION" else "판매중"
        msg = (f"등록 완료 — 원상품번호 {outcome.origin_product_no}, "
               f"채널상품번호 {outcome.channel_product_no or '-'}, 상태: {status}")
        self.status.setText(msg)
        if outcome.warning:
            QMessageBox.warning(self, "등록 완료 (확인 필요)", f"{msg}\n\n{outcome.warning}")
        else:
            QMessageBox.information(self, "등록 완료", msg)
        if self._draft_id and QMessageBox.question(
                self, "임시저장", f"등록한 상품의 임시저장 #{self._draft_id}을 삭제할까요?") \
                == QMessageBox.StandardButton.Yes:
            self.services.db.delete_draft(self._draft_id)
            self._draft_id = None
            self._update_draft_label()
            self.drafts_changed.emit()

    def _on_failed(self, exc: Exception) -> None:
        self.register_btn.setEnabled(True)
        self.status.setText("등록 실패")
        QMessageBox.critical(self, "등록 실패", describe_error(exc))
