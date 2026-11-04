"""엑셀 일괄 등록 — 양식 생성, 업로드 파일 읽기·검증, 결과 저장.

양식 구성
- '상품' 시트: 1행 = 상품 1개
- '옵션상세' 시트(선택): 조합별 추가금액·재고 지정. 상품키로 '상품' 시트 행과 연결
- '안내' 시트: 작성 방법, 저장된 템플릿 목록
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from services import templates
from services.options import OptionGroup, generate_rows, parse_values
from services.product_builder import ProductDraft
from services.validator import validate
from storage.db import Database

PRODUCT_SHEET = "상품"
OPTION_SHEET = "옵션상세"
GUIDE_SHEET = "안내"

# (헤더, 필수 여부, 설명)
COLUMNS = [
    ("상품키", False, "행 식별용 코드(선택). 옵션상세 시트 연결에 사용"),
    ("카테고리ID", True, "최하위 카테고리 ID. 앱의 '카테고리 검색'에서 확인"),
    ("상품명", True, "100자 이하"),
    ("판매가", True, "10원 단위"),
    ("재고수량", False, "옵션 없는 상품만. 비우면 0"),
    ("대표이미지", True, "이미지 파일 경로. 엑셀 파일 기준 상대 경로 가능"),
    ("추가이미지", False, "여러 개는 | 로 구분 (최대 9개)"),
    ("상세설명", True, "HTML 또는 일반 텍스트"),
    ("옵션명1", False, "예: 색상"),
    ("옵션값1", False, "쉼표로 구분. 예: 블랙, 화이트"),
    ("옵션명2", False, ""),
    ("옵션값2", False, ""),
    ("옵션명3", False, ""),
    ("옵션값3", False, ""),
    ("옵션재고", False, "옵션상세에 없는 조합의 기본 재고"),
    ("배송템플릿", False, "비우면 기본(★) 템플릿"),
    ("반품템플릿", False, "비우면 기본(★) 템플릿"),
    ("AS템플릿", False, "비우면 기본(★) 템플릿"),
    ("고시템플릿", False, "비우면 기본(★) 템플릿"),
    ("AS전화번호", False, "입력하면 A/S 템플릿 대신 사용"),
    ("테스트등록", False, "Y(기본): 전시중지·판매중지로 등록 / N: 바로 판매"),
]
OPTION_COLUMNS = ["상품키", "옵션값1", "옵션값2", "옵션값3", "추가금액", "재고", "판매자관리코드", "사용"]
_TEMPLATE_COLUMNS = {"배송템플릿": "delivery", "반품템플릿": "claim", "AS템플릿": "as", "고시템플릿": "notice"}

_HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
_REQUIRED_FILL = PatternFill("solid", fgColor="FCE4D6")


@dataclass
class ExcelRow:
    row_no: int                 # 엑셀 행 번호
    key: str
    draft: ProductDraft
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# --- 양식 ---
def write_template(path: str | Path, db: Database) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = PRODUCT_SHEET
    ws.append([c[0] for c in COLUMNS])
    for i, (name, required, desc) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=i)
        cell.font = Font(bold=True)
        cell.fill = _REQUIRED_FILL if required else _HEADER_FILL
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[cell.column_letter].width = max(12, len(name) * 2 + 4)
    ws.append(["A001", "50000803", "예시) 기본 반팔 티셔츠", 19900, "", "images/tshirt.jpg", "",
               "부드러운 면 소재 티셔츠입니다.", "색상", "블랙, 화이트", "사이즈", "S, M, L", "", "", 10,
               "", "", "", "", "", "Y"])
    ws.freeze_panes = "B2"
    yn = DataValidation(type="list", formula1='"Y,N"', allow_blank=True)
    ws.add_data_validation(yn)
    yn.add(f"{ws.cell(row=2, column=len(COLUMNS)).column_letter}2:"
           f"{ws.cell(row=2, column=len(COLUMNS)).column_letter}1000")

    opt = wb.create_sheet(OPTION_SHEET)
    opt.append(OPTION_COLUMNS)
    for cell in opt[1]:
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
    opt.append(["A001", "블랙", "L", "", 1000, 5, "BK-L", "Y"])
    opt.column_dimensions["G"].width = 16

    guide = wb.create_sheet(GUIDE_SHEET)
    guide.column_dimensions["A"].width = 16
    guide.column_dimensions["B"].width = 70
    lines = [
        ("작성 방법", ""),
        ("", "• '상품' 시트 2행부터 상품을 1행에 1개씩 입력합니다. 예시 행은 지우고 사용하세요."),
        ("", "• 주황색 헤더는 필수 항목입니다."),
        ("", "• 이미지는 PC의 파일 경로로 입력합니다. 엑셀 파일과 같은 폴더 기준 상대 경로도 됩니다."),
        ("", "• 옵션: 옵션명/옵션값을 입력하면 모든 조합이 만들어지고, 추가금액은 0원, 재고는 '옵션재고'로 들어갑니다."),
        ("", "• 조합별 추가금액·재고를 다르게 하려면 '옵션상세' 시트에 상품키와 옵션값을 적어 지정합니다."),
        ("", "• 배송·반품·A/S·고시 정보는 앱에 저장한 템플릿 이름으로 지정합니다. 비우면 기본(★) 템플릿을 씁니다."),
        ("", ""),
        ("저장된 템플릿", ""),
    ]
    for kind, (label, _fields) in templates.TEMPLATE_KINDS.items():
        names = [("★ " if t.is_default else "") + t.name for t in templates.list_templates(db, kind)]
        lines.append((label, ", ".join(names) or "(없음 — 앱의 상품 등록 화면에서 저장하세요)"))
    lines += [("", ""), ("항목 설명", "")] + [(n, ("[필수] " if r else "") + d) for n, r, d in COLUMNS]
    for a, b in lines:
        guide.append([a, b])
        if a and not b:
            guide.cell(row=guide.max_row, column=1).font = Font(bold=True)
    wb.save(path)


# --- 값 변환 ---
def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _int(v, label: str, errors: list[str], default: int = 0) -> int:
    s = _text(v).replace(",", "").replace("원", "")
    if not s:
        return default
    try:
        return int(float(s))
    except ValueError:
        errors.append(f"{label}에 숫자를 입력하세요: {s}")
        return default


def _yes(v, default: bool = True) -> bool:
    s = _text(v).upper()
    return default if not s else s in ("Y", "YES", "O", "1", "TRUE", "예")


def _resolve_path(value: str, base_dir: Path) -> str:
    if not value:
        return ""
    p = Path(value)
    return str(p if p.is_absolute() else (base_dir / p).resolve())


# --- 읽기 ---
def _read_sheet(ws) -> tuple[list[str], list[tuple[int, dict]]]:
    rows = ws.iter_rows(values_only=True)
    header = [_text(h) for h in next(rows, [])]
    result = []
    for i, values in enumerate(rows, start=2):
        record = {h: values[j] if j < len(values) else None for j, h in enumerate(header) if h}
        if any(_text(v) for v in record.values()):
            result.append((i, record))
    return header, result


def _read_option_details(wb) -> dict[str, list[tuple[int, dict]]]:
    if OPTION_SHEET not in wb.sheetnames:
        return {}
    _header, rows = _read_sheet(wb[OPTION_SHEET])
    details: dict[str, list[tuple[int, dict]]] = {}
    for row_no, record in rows:
        details.setdefault(_text(record.get("상품키")), []).append((row_no, record))
    return details


def read_workbook(path: str | Path, db: Database) -> list[ExcelRow]:
    path = Path(path)
    wb = load_workbook(path, read_only=True, data_only=True)
    if PRODUCT_SHEET not in wb.sheetnames:
        raise ValueError(f"'{PRODUCT_SHEET}' 시트가 없습니다. 앱에서 받은 양식을 사용하세요.")
    header, rows = _read_sheet(wb[PRODUCT_SHEET])
    missing = [name for name, required, _ in COLUMNS if required and name not in header]
    if missing:
        raise ValueError(f"필수 열이 없습니다: {', '.join(missing)}")
    option_details = _read_option_details(wb)
    wb.close()

    tmpl_by_name = {kind: {t.name: t for t in templates.list_templates(db, kind)} for kind in templates.TEMPLATE_KINDS}
    has_categories = db.category_count() > 0
    result = []
    for row_no, rec in rows:
        errors: list[str] = []
        key = _text(rec.get("상품키"))
        d = templates.apply_defaults(db, ProductDraft())

        for col, kind in _TEMPLATE_COLUMNS.items():
            name = _text(rec.get(col))
            if name:
                t = tmpl_by_name[kind].get(name.removeprefix("★ "))
                if t:
                    templates.apply(kind, d, t.data)
                else:
                    errors.append(f"{col} '{name}'을(를) 찾을 수 없습니다.")

        d.leaf_category_id = _text(rec.get("카테고리ID"))
        if d.leaf_category_id and has_categories:
            category = db.get_category(d.leaf_category_id)
            if category is None:
                errors.append(f"카테고리ID {d.leaf_category_id}가 없습니다.")
            elif not category.last:
                errors.append(f"카테고리ID {d.leaf_category_id}는 최하위 카테고리가 아닙니다.")
            else:
                d.category_name = category.whole_name
        d.name = _text(rec.get("상품명"))
        d.sale_price = _int(rec.get("판매가"), "판매가", errors)
        d.stock_quantity = _int(rec.get("재고수량"), "재고수량", errors)
        d.representative_image = _resolve_path(_text(rec.get("대표이미지")), path.parent)
        d.optional_images = [_resolve_path(p.strip(), path.parent)
                             for p in _text(rec.get("추가이미지")).split("|") if p.strip()]
        d.detail_html = _text(rec.get("상세설명"))
        if _text(rec.get("AS전화번호")):
            d.as_phone = _text(rec.get("AS전화번호"))
        d.test_mode = _yes(rec.get("테스트등록"), default=True)

        groups = [OptionGroup(_text(rec.get(f"옵션명{i}")), parse_values(_text(rec.get(f"옵션값{i}"))))
                  for i in range(1, 4)]
        groups = [g for g in groups if g.name or g.values]
        if groups:
            d.use_options = True
            d.option_groups = groups
            d.option_rows = generate_rows([g for g in groups if g.name and g.values],
                                          default_stock=_int(rec.get("옵션재고"), "옵션재고", errors))
            _apply_option_details(d, option_details.get(key, []) if key else [], errors)
        elif key and option_details.get(key):
            errors.append("옵션상세가 있지만 옵션명/옵션값이 비어 있습니다.")

        errors += validate(d)
        result.append(ExcelRow(row_no, key, d, errors))
    return result


def _apply_option_details(d: ProductDraft, details: list[tuple[int, dict]], errors: list[str]) -> None:
    by_values = {tuple(r.values): r for r in d.option_rows}
    n = len(d.option_groups)
    for row_no, rec in details:
        values = tuple(_text(rec.get(f"옵션값{i}")) for i in range(1, n + 1))
        row = by_values.get(values)
        if row is None:
            errors.append(f"옵션상세 {row_no}행: 조합 '{' / '.join(values)}'이(가) 옵션값에 없습니다.")
            continue
        row.price = _int(rec.get("추가금액"), f"옵션상세 {row_no}행 추가금액", errors)
        row.stock = _int(rec.get("재고"), f"옵션상세 {row_no}행 재고", errors, default=row.stock)
        row.seller_code = _text(rec.get("판매자관리코드"))
        row.usable = _yes(rec.get("사용"), default=True)


# --- 결과 저장 ---
def write_results(path: str | Path, rows: list[ExcelRow], results: dict[int, dict]) -> None:
    """results: 엑셀 행 번호 → {"status": ..., "origin_product_no": ..., "message": ...}"""
    wb = Workbook()
    ws = wb.active
    ws.title = "등록결과"
    ws.append(["엑셀행", "상품키", "상품명", "결과", "원상품번호", "판매상태", "메시지"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
    for r in rows:
        res = results.get(r.row_no, {})
        status = res.get("status") or ("검증 오류" if r.errors else "미등록")
        message = res.get("message") or "\n".join(r.errors)
        ws.append([r.row_no, r.key, r.draft.name, status, res.get("origin_product_no"),
                   res.get("sale_status", ""), message])
    for col, width in zip("ABCDEFG", (8, 12, 40, 10, 16, 10, 80)):
        ws.column_dimensions[col].width = width
    wb.save(path)
