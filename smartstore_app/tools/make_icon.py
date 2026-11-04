"""앱 아이콘(resources/app.ico, app.png) 생성. 디자인을 바꿀 때만 실행한다.

    ..\\.venv\\Scripts\\python.exe tools\\make_icon.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

RESOURCES = Path(__file__).resolve().parent.parent / "resources"

SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#FFFFFF"
 stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/></svg>"""


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#03C75A"))
    radius = size * 0.22
    p.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
    margin = size * 0.2
    QSvgRenderer(QByteArray(SVG.encode())).render(p, QRectF(margin, margin, size - 2 * margin, size - 2 * margin))
    p.end()
    return image


def main() -> None:
    QGuiApplication(sys.argv)
    image = render(256)
    image.save(str(RESOURCES / "app.png"))
    if not image.save(str(RESOURCES / "app.ico"), "ICO"):
        raise SystemExit("ICO 저장 실패 (Qt 이미지 형식 플러그인 확인)")
    print("생성:", RESOURCES / "app.ico")


if __name__ == "__main__":
    main()
