"""작업 엑셀(상품입력.xlsx) — 양식 생성, 읽기, 결과 기록(상태·네이버상품번호·결과메시지 + 빈 칸에만 AI 초안).

'상품' 시트 1행 = 상품 1개. 상태가 '대기'인 행을 자동화가 처리한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

SHEET = "상품"
LIST_SHEET = "목록"
GUIDE_SHEET = "안내"

PHOTO_AUTO, PHOTO_AS_IS = "자동", "그대로"
PHOTO_MODES = [PHOTO_AUTO, PHOTO_AS_IS]

STATUS_WAITING = "대기"
STATUS_PROCESSING = "처리중"
STATUS_REGISTERED = "등록완료(판매중지)"
STATUS_ON_SALE = "판매중"
STATUS_FAILED = "실패"
STATUS_DELETED = "삭제됨"     # 검수 대기에서 스토어 상품을 지운 경우
STATUSES = [STATUS_WAITING, STATUS_PROCESSING, STATUS_REGISTERED, STATUS_ON_SALE, STATUS_FAILED, STATUS_DELETED]
METHODS = ["도매몰", "촬영"]

# (헤더, 필수, 설명, 열 너비)
COLUMNS = [
    ("상품코드", True, "직접 정하는 고유값 (예: A001). 사진 폴더 이름과 같아야 함", 10),
    ("입력방식", True, "도매몰 / 촬영", 9),
    ("원본상품명", True, "도매몰 상품명 또는 직접 입력", 30),
    ("원가", True, "공급가·매입가 (원)", 9),
    ("옵션", False, "예: 색상:베이지,블랙 / 사이즈:S,M,L", 30),
    ("재고", True, "숫자(모든 조합 동일) 또는 베이지-S:3, 블랙-M:2", 20),
    ("분류", True, "자동화 설정의 분류명 (드롭다운)", 12),
    ("브랜드", False, "비우면 분류 설정의 브랜드", 10),
    ("키워드", False, "비우면 분류 설정의 핵심키워드", 14),
    ("이미지URL", False, "도매몰 이미지 주소. 여러 개는 | 로 구분. 사진 폴더에 넣었으면 비워도 됨", 30),
    ("실측", False, "예: S:어깨40,가슴50,총장60 / M:어깨42,가슴52,총장62", 30),
    ("소재", False, "", 12),
    ("세탁", False, "", 12),
    ("상품명(최종)", False, "비우면 분류의 상품명 규칙으로 자동 생성", 30),
    ("설명(최종)", False, "비우면 자동 생성(상세페이지·AI 초안 단계)", 30),
    ("상태", True, "대기 / 처리중 / 등록완료(판매중지) / 판매중 / 실패", 16),
    ("네이버상품번호", False, "앱이 기록", 15),
    ("결과메시지", False, "앱이 기록", 40),
    # 나중에 추가한 선택 칸 — 기존 엑셀은 맨 뒤에 붙인다 (열 위치가 바뀌지 않도록)
    ("판매가", False, "직접 정한 판매가(원). 적으면 원가·마진 계산 대신 이 값으로 등록", 10),
    ("사진보정", False, "자동(기본) / 그대로 — 완성된 사진이면 '그대로'(밝기·배경 제거 안 함)", 12),
    ("상세템플릿", False, "비우면 분류 설정. 완성된 상세 이미지만 넣으려면 '이미지만'", 12),
]
HEADERS = [c[0] for c in COLUMNS]
APP_COLUMNS = {"네이버상품번호", "결과메시지"}

_FILL_REQUIRED = PatternFill("solid", fgColor="FCE4D6")
_FILL_OPTIONAL = PatternFill("solid", fgColor="DDEBF7")
_FILL_APP = PatternFill("solid", fgColor="E2EFDA")


@dataclass
class InputItem:
    row_no: int
    values: dict[str, str]

    def get(self, column: str) -> str:
        return self.values.get(column, "")

    @property
    def code(self) -> str:
        return self.get("상품코드")

    @property
    def status(self) -> str:
        return self.get("상태")


def _col_letter(index: int) -> str:
    from openpyxl.utils import get_column_letter
    return get_column_letter(index)


def write_template(path: str | Path, category_names: list[str]) -> None:
    """빈 작업 엑셀을 만든다. 분류 드롭다운은 '목록' 시트(숨김)를 참조한다."""
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    ws.append(HEADERS)
    for i, (name, required, desc, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=i)
        cell.font = Font(bold=True)
        cell.fill = _FILL_APP if name in APP_COLUMNS else _FILL_REQUIRED if required else _FILL_OPTIONAL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[_col_letter(i)].width = width
    ws.freeze_panes = "B2"

    lists = wb.create_sheet(LIST_SHEET)
    lists.append(["분류"])
    for name in category_names:
        lists.append([name])
    lists.sheet_state = "hidden"

    def add_list(column: str, formula: str) -> None:
        letter = _col_letter(HEADERS.index(column) + 1)
        dv = DataValidation(type="list", formula1=formula, allow_blank=True)
        dv.error, dv.errorTitle = "목록에서 선택하세요.", "입력 확인"
        ws.add_data_validation(dv)
        dv.add(f"{letter}2:{letter}2000")

    add_list("입력방식", '"' + ",".join(METHODS) + '"')
    add_list("상태", '"' + ",".join(STATUSES) + '"')
    add_list("사진보정", '"' + ",".join(PHOTO_MODES) + '"')
    add_list("분류", f"='{LIST_SHEET}'!$A$2:$A$200")

    guide = wb.create_sheet(GUIDE_SHEET)
    guide.column_dimensions["A"].width = 16
    guide.column_dimensions["B"].width = 80
    rows = [
        ("사용 방법", ""),
        ("", "• '상품' 시트 2행부터 상품을 1행에 1개씩 입력하고 상태를 '대기'로 둡니다."),
        ("", "• 주황색 헤더는 필수, 파란색은 선택, 초록색은 앱이 기록하는 칸입니다."),
        ("", "• 사진: 사진 폴더\\{상품코드}\\원본\\ 에 넣습니다. 이름순 첫 장이 대표 이미지,"
             " 파일명에 detail이 들어간 사진은 상세페이지용입니다."),
        ("", "• 도매몰 상품은 '이미지URL'에 주소를 적으면 등록할 때 사진 폴더로 내려받습니다."),
        ("", "• 상품명(최종)·설명(최종)을 직접 적으면 자동화가 절대 바꾸지 않습니다."),
        ("", "• 분류 목록을 바꿨다면 앱의 자동화 설정에서 '양식 만들기'로 새 양식을 받으세요."),
        ("", ""),
        ("항목 설명", ""),
    ] + [(name, ("[필수] " if required else "") + desc) for name, required, desc, _ in COLUMNS]
    for a, b in rows:
        guide.append([a, b])
        if a and not b:
            guide.cell(row=guide.max_row, column=1).font = Font(bold=True)
    wb.save(path)


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def read_items(path: str | Path) -> list[InputItem]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"작업 엑셀이 없습니다: {path}\n자동화 설정에서 '양식 만들기'로 만들 수 있습니다.")
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if SHEET not in wb.sheetnames:
            raise ValueError(f"'{SHEET}' 시트가 없습니다. 자동화용 양식인지 확인하세요: {path.name}")
        rows = wb[SHEET].iter_rows(values_only=True)
        header = [_text(h) for h in next(rows, [])]
        missing = [h for h, required, _, _ in COLUMNS if required and h not in header]
        if missing:
            raise ValueError(f"필수 열이 없습니다: {', '.join(missing)}")
        items = []
        for row_no, values in enumerate(rows, start=2):
            record = {h: _text(values[i]) if i < len(values) else "" for i, h in enumerate(header) if h}
            if any(record.values()):
                items.append(InputItem(row_no, record))
        return items
    finally:
        wb.close()


@dataclass
class ResultUpdate:
    status: str | None          # None이면 상태·결과메시지는 그대로 두고 fills만 쓴다
    naver_no: int | None = None
    message: str = ""
    row_no: int | None = None   # 읽을 때의 행 번호 (그 사이 정렬이 바뀌었으면 상품코드로 다시 찾음)
    fills: dict[str, str] = field(default_factory=dict)   # 비어 있을 때만 채울 칸 (AI 초안: 상품명(최종)·설명(최종))
    sets: dict[str, str] = field(default_factory=dict)    # 사람이 화면에서 고른 값 (칸을 바꿈)


def write_results(path: str | Path, updates: dict[str, ResultUpdate]) -> None:
    """앱이 기록하는 칸(상태·네이버상품번호·결과메시지)만 고친다. 사람이 입력한 칸은 건드리지 않는다.
    fills는 저장하는 순간에도 칸이 비어 있을 때만 쓴다 (그 사이 사람이 적었으면 그 값을 둔다).

    임시 파일에 저장한 뒤 바꿔치기한다. 엑셀에서 파일이 열려 있으면 PermissionError가 난다(호출 쪽에서 DB에 보관).
    """
    if not updates:
        return
    path = Path(path)
    wb = load_workbook(path)
    ws = wb[SHEET]
    header = [_text(c.value) for c in ws[1]]
    col = {name: header.index(name) + 1 for name in ("상품코드", "상태", "네이버상품번호", "결과메시지")}
    rows_by_code: dict[str, list[int]] = {}
    for r in range(2, ws.max_row + 1):
        rows_by_code.setdefault(_text(ws.cell(r, col["상품코드"]).value), []).append(r)
    for code, u in updates.items():
        rows = rows_by_code.get(code, [])
        if u.row_no in rows:
            rows = [u.row_no]
        for r in rows:
            if u.status is not None:
                ws.cell(r, col["상태"]).value = u.status
                if u.naver_no:
                    ws.cell(r, col["네이버상품번호"]).value = u.naver_no
                ws.cell(r, col["결과메시지"]).value = u.message
            for name, value in u.sets.items():
                if name in header:
                    ws.cell(r, header.index(name) + 1).value = value
            for name, value in u.fills.items():
                if name in header and not _text(ws.cell(r, header.index(name) + 1).value):
                    ws.cell(r, header.index(name) + 1).value = value
    temp = path.with_name(f"~{path.stem}.saving{path.suffix}")
    try:
        wb.save(temp)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
