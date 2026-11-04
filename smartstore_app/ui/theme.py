"""앱 테마 — Fusion 스타일 + 팔레트 + resources/style.qss, 사이드바 아이콘, 버튼 역할."""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QEvent, QLibraryInfo, QLocale, QObject, QRectF, Qt, QTranslator
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from services.paths import resource_path  # noqa: F401 (main.py에서 사용)

ACCENT = "#03C75A"        # 선·막대·포커스 테두리
ACCENT_STRONG = "#008040"  # 흰 글씨가 올라가는 배경 (명암비 5:1)
TEXT = "#1D2939"
MUTED = "#667085"

STATUS_COLORS = {"SALE": "#039855", "SUSPENSION": "#667085", "OUTOFSTOCK": "#D92D20", "WAIT": "#DC6803"}

_FONT_CANDIDATES = ["Pretendard", "Pretendard Variable", "Noto Sans KR", "Malgun Gothic"]

# 24x24 선 아이콘 (stroke 색은 렌더링 시 지정)
_ICONS = {
    "product": '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
    "excel": '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M4 9h16M4 15h16M10 3v18"/>',
    "drafts": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
              '<path d="M14 3v5h5M9 13h6M9 17h4"/>',
    "manage": '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/>'
              '<circle cx="4.5" cy="18" r="1"/>',
    "automation": '<path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z"/>',
    "camera": '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
    "bell": '<path d="M6 9a6 6 0 0 1 12 0c0 6 3 8 3 8H3s3-2 3-8"/><path d="M10 21a2 2 0 0 0 4 0"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
    "settings": '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/>'
                '<circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
}


def _pick_font() -> str:
    families = set(QFontDatabase.families())
    return next((f for f in _FONT_CANDIDATES if f in families), QApplication.font().family())


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(QFont(_pick_font(), 10))

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#F4F6F8"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#FAFBFC"))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#344054"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT_STRONG))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#98A2B3"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#FFFFFF"))
    app.setPalette(palette)

    qss = resource_path("style.qss")
    if qss.is_file():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))
    _install_korean(app)
    _fitter = getattr(app, "_message_box_fitter", None)
    if _fitter is None:
        app._message_box_fitter = _MessageBoxFitter(app)
        app.installEventFilter(app._message_box_fitter)


class _MessageBoxFitter(QObject):
    """알림창이 뜨기 직전에 버튼마다 글자가 다 들어가는 최소 너비를 준다.

    Qt 알림창은 너비를 '최소 크기' 기준으로 정하는데, 스타일(여백)이 붙은 버튼의 최소 크기가 실제보다 작아
    버튼 글씨가 잘렸다. 버튼 최소 너비를 맞추면 알림창이 그만큼 넓어지고, 본문은 Qt가 알아서 줄바꿈한다.
    (본문 칸 너비를 고정하면 긴 본문이 잘리므로 건드리지 않는다.)
    """

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Show and isinstance(obj, QMessageBox):
            for button in obj.findChildren(QPushButton):
                button.setMinimumWidth(button.sizeHint().width())
        return False


_translators: list[QTranslator] = []


def _install_korean(app: QApplication) -> None:
    """Qt 기본 버튼·대화상자 문구(Show Details, Yes/No, Cancel 등)를 한국어로."""
    folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    translator = QTranslator(app)
    if translator.load(QLocale(QLocale.Language.Korean, QLocale.Country.SouthKorea), "qtbase", "_", folder):
        app.installTranslator(translator)
        _translators.append(translator)


def _render(name: str, color: str, size: int) -> QPixmap:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{_ICONS[name]}</svg>')
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


def nav_icon(name: str, size: int = 36) -> QIcon:
    """사이드바용: 평소 회색, 선택 시 흰색."""
    icon = QIcon()
    icon.addPixmap(_render(name, "#9CA3AF", size), QIcon.Mode.Normal)
    icon.addPixmap(_render(name, "#FFFFFF", size), QIcon.Mode.Selected)
    icon.addPixmap(_render(name, "#FFFFFF", size), QIcon.Mode.Active)
    return icon


def set_role(button: QPushButton, role: str, large: bool = False) -> QPushButton:
    """버튼 강조: role = "primary" | "danger"."""
    button.setProperty("role", role)
    if large:
        button.setProperty("size", "large")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button
