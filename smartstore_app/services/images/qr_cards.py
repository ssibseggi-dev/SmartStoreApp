"""상품코드 QR 카드 — A4 PDF로 출력해 촬영 때 상품 앞에 한 장 찍으면, 그 뒤 사진이 그 상품으로 분류된다.

QR 내용: "SSAPP:{상품코드}" (다른 QR — 옷 태그 등 — 과 헷갈리지 않도록 앞에 표시를 붙인다)
A4 한 장에 2열 × 4행 = 8장, 가위로 자르기 쉽게 점선을 넣는다.
"""
from __future__ import annotations

import os
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont

QR_PREFIX = "SSAPP:"
DPI = 300
A4 = (2480, 3508)          # 300dpi
COLUMNS, ROWS = 2, 4
MARGIN = 120


def card_payload(code: str) -> str:
    return f"{QR_PREFIX}{code}"


def parse_payload(text: str) -> str | None:
    """앱이 만든 QR 카드면 상품코드, 아니면(옷 태그 QR 등) None."""
    text = (text or "").strip()
    if not text.startswith(QR_PREFIX):
        return None
    return text[len(QR_PREFIX):].strip() or None


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in (["malgunbd.ttf"] if bold else []) + ["malgun.ttf", "NotoSansKR-Regular.ttf"]:
        if (fonts / name).is_file():
            return ImageFont.truetype(str(fonts / name), size)
    return ImageFont.load_default(size)


def _fit(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def render_card(code: str, name: str, size: tuple[int, int]) -> Image.Image:
    w, h = size
    card = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(card)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2, box_size=10)
    qr.add_data(card_payload(code))
    qr.make(fit=True)
    side = int(min(w * 0.42, h * 0.78))
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((side, side), Image.NEAREST)
    card.paste(qr_img, (int(w * 0.04), (h - side) // 2))

    x = int(w * 0.04) + side + int(w * 0.05)
    text_w = w - x - int(w * 0.04)

    def line(y: float, text: str, size: float, color: str, bold: bool = False) -> None:
        font = _font(int(h * size), bold)
        while font.size > 12 and draw.textlength(text, font=font) > text_w and size > 0.04:   # 칸에 맞게 줄임
            size -= 0.005
            font = _font(int(h * size), bold)
        draw.text((x, int(h * y)), _fit(draw, text, font, text_w), fill=color, font=font)

    line(0.16, "상품코드", 0.065, "#6B6B6B")
    line(0.26, code, 0.2, "black", bold=True)
    line(0.53, name, 0.07, "#1F1F1F")
    line(0.70, "이 카드를 먼저 찍고", 0.052, "#6B6B6B")
    line(0.79, "이어서 상품 사진을 찍으세요", 0.052, "#6B6B6B")
    return card


def _dashed(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int], dash: int = 24) -> None:
    (x1, y1), (x2, y2) = start, end
    length = max(abs(x2 - x1), abs(y2 - y1))
    for s in range(0, length, dash * 2):
        e = min(s + dash, length)
        if x1 == x2:
            draw.line([(x1, y1 + s), (x1, y1 + e)], fill="#BBBBBB", width=3)
        else:
            draw.line([(x1 + s, y1), (x1 + e, y1)], fill="#BBBBBB", width=3)


def make_pdf(cards: list[tuple[str, str]], path: str | Path) -> Path:
    """cards: [(상품코드, 상품명)]. A4 여러 장 PDF로 저장한다."""
    if not cards:
        raise ValueError("QR 카드를 만들 상품이 없습니다.")
    path = Path(path)
    cell_w = (A4[0] - 2 * MARGIN) // COLUMNS
    cell_h = (A4[1] - 2 * MARGIN) // ROWS
    pages = []
    per_page = COLUMNS * ROWS
    for start in range(0, len(cards), per_page):
        page = Image.new("RGB", A4, "white")
        draw = ImageDraw.Draw(page)
        for i, (code, name) in enumerate(cards[start:start + per_page]):
            col, row = i % COLUMNS, i // COLUMNS
            x, y = MARGIN + col * cell_w, MARGIN + row * cell_h
            page.paste(render_card(code, name, (cell_w - 40, cell_h - 40)), (x + 20, y + 20))
        for c in range(COLUMNS + 1):   # 자르는 선
            _dashed(draw, (MARGIN + c * cell_w, MARGIN), (MARGIN + c * cell_w, MARGIN + ROWS * cell_h))
        for r in range(ROWS + 1):
            _dashed(draw, (MARGIN, MARGIN + r * cell_h), (MARGIN + COLUMNS * cell_w, MARGIN + r * cell_h))
        pages.append(page)
    pages[0].save(path, "PDF", resolution=DPI, save_all=True, append_images=pages[1:])
    return path
