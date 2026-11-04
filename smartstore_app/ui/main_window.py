"""메인 윈도우 — 좌측 사이드바(메뉴·연결 상태) + 페이지 헤더 + 페이지 전환."""
from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QStackedWidget, QTabWidget,
    QVBoxLayout,
    QWidget,
)

from services.connection import AppServices
from storage import credentials
from ui.automation_page import AutomationPage
from ui.category_page import CategorySearchWidget
from ui.drafts_page import DraftsPage
from ui.excel_page import ExcelPage
from ui.manage_page import ManagePage
from ui.product_page import ProductPage
from ui.review_page import ReviewPage
from ui.deleted_page import DeletedPage
from ui.photo_inbox_page import PhotoInboxPage
from ui.schedule_page import SchedulePage
from ui.settings_page import SettingsPage
from ui.theme import nav_icon


def _with_header(title: str, subtitle: str, page: QWidget) -> QWidget:
    container = QWidget()
    container.setObjectName("content")
    header = QWidget()
    header.setObjectName("pageHeader")
    hl = QVBoxLayout(header)
    hl.setContentsMargins(24, 20, 24, 4)
    hl.setSpacing(2)
    title_label = QLabel(title)
    title_label.setObjectName("pageTitle")
    subtitle_label = QLabel(subtitle)
    subtitle_label.setObjectName("pageSubtitle")
    hl.addWidget(title_label)
    hl.addWidget(subtitle_label)

    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    layout.addWidget(header)
    if page.layout():
        page.layout().setContentsMargins(24, 8, 24, 16)
    layout.addWidget(page, 1)
    return container


class MainWindow(QMainWindow):
    def __init__(self, services: AppServices):
        super().__init__()
        self.setWindowTitle("스마트스토어 상품 등록")
        self.resize(1240, 820)
        self.setMinimumSize(980, 640)

        self.settings_page = SettingsPage(services)
        self.category_page = CategorySearchWidget(services)
        self.settings_page.categories_updated.connect(self.category_page.search)
        self.settings_page.credentials_changed.connect(self._update_connection_status)

        self.product_page = ProductPage(services)
        self.drafts_page = DraftsPage(services.db)
        self.product_page.drafts_changed.connect(self.drafts_page.reload)
        self.drafts_page.open_requested.connect(self.open_draft)

        self.manage_page = ManagePage(services)
        self.review_page = ReviewPage(services)
        self.manage_tabs = QTabWidget()
        self.manage_tabs.addTab(self.review_page, "검수 대기")
        self.deleted_page = DeletedPage(services)
        self.manage_tabs.addTab(self.deleted_page, "삭제 내역")
        self.manage_tabs.addTab(self.manage_page, "스토어 전체 상품")
        manage_container = QWidget()
        QVBoxLayout(manage_container).addWidget(self.manage_tabs)
        self.excel_page = ExcelPage(services)
        self.excel_page.open_in_editor.connect(self.open_excel_row)
        self.automation_page = AutomationPage(services)
        self.schedule_page = SchedulePage(services)
        self.photo_inbox_page = PhotoInboxPage(services)

        # --- 사이드바 ---
        brand_title = QLabel("SmartStore")
        brand_title.setObjectName("brandTitle")
        brand_subtitle = QLabel("상품 등록 · 관리")
        brand_subtitle.setObjectName("brandSubtitle")

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setIconSize(QSize(18, 18))
        self.pages = QStackedWidget()
        self._index: dict[QWidget, int] = {}
        pages = [
            ("product", "상품 등록", "단일·옵션 상품을 입력해 스마트스토어에 등록합니다.", self.product_page),
            ("excel", "엑셀 일괄 등록", "엑셀 양식으로 여러 상품을 한 번에 등록합니다.", self.excel_page),
            ("drafts", "임시저장함", "작성 중에 저장한 상품을 불러오거나 삭제합니다.", self.drafts_page),
            ("manage", "상품 관리", "자동 등록한 상품을 검수해 판매를 시작하고, 스토어 상품의 가격·재고·상태를 수정합니다.",
             manage_container),
            ("automation", "자동화 설정", "분류별 카테고리·마진·상품명 규칙과 자동 등록 공통 설정을 관리합니다.",
             self.automation_page),
            ("camera", "촬영 사진", "QR 카드를 출력하고, 폰으로 찍은 사진을 상품별 폴더로 자동 분리합니다.",
             self.photo_inbox_page),
            ("bell", "예약 · 알림", "정한 시각에 자동 등록을 실행하고, 결과를 카카오톡으로 받습니다.", self.schedule_page),
            ("search", "카테고리 검색", "상품 등록에 쓸 최하위 카테고리를 찾습니다.", self.category_page),
            ("settings", "설정", "커머스API 인증 정보와 호출 IP를 관리합니다.", self.settings_page),
        ]
        for icon, title, subtitle, page in pages:
            self.nav.addItem(QListWidgetItem(nav_icon(icon), title))
            self._index[page] = self.pages.addWidget(_with_header(title, subtitle, page))
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)

        self.connection_status = QLabel()
        self.connection_status.setObjectName("connectionStatus")
        self.connection_status.setWordWrap(True)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(208)
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(14, 22, 14, 14)
        sl.setSpacing(0)
        sl.addWidget(brand_title)
        sl.addWidget(brand_subtitle)
        sl.addSpacing(22)
        sl.addWidget(self.nav, 1)
        sl.addWidget(self.connection_status)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(sidebar)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(central)

        # 인증 정보가 없으면 설정 화면부터 보여준다
        self._update_connection_status()
        self.nav.setCurrentRow(self.nav.count() - 1 if credentials.load() is None else 0)

    def _show(self, page: QWidget) -> None:
        self.nav.setCurrentRow(self._index[page])

    def _update_connection_status(self) -> None:
        if credentials.load():
            self.connection_status.setText("<span style='color:#03C75A'>●</span>  커머스API 인증 정보 저장됨")
        else:
            self.connection_status.setText("<span style='color:#F79009'>●</span>  인증 정보 없음 — 설정에서 입력")

    def open_draft(self, draft_id: int) -> None:
        if QMessageBox.question(self, "임시저장 불러오기", "상품 등록 화면의 입력 중인 내용을 덮어씁니다. 계속할까요?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.product_page.open_draft(draft_id)
        self._show(self.product_page)

    def open_excel_row(self, draft) -> None:
        if QMessageBox.question(self, "상품 등록 화면에서 열기",
                                "상품 등록 화면의 입력 중인 내용을 덮어씁니다. 계속할까요?\n"
                                "(수정 후 임시저장하거나 바로 등록할 수 있습니다. 엑셀 파일은 바뀌지 않습니다.)") \
                != QMessageBox.StandardButton.Yes:
            return
        self.product_page.open_product(draft)
        self._show(self.product_page)
