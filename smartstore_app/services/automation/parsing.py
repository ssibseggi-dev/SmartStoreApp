"""작업 엑셀 문자열 칸 해석 — 옵션, 재고, 실측.

옵션: "색상:베이지,블랙 / 사이즈:S,M,L"
재고: "10"(모든 조합 동일) 또는 "베이지-S:3, 블랙-M:2"(조합별, 적지 않은 조합은 0)
실측: "S:어깨40,가슴50,총장60 / M:어깨42,가슴52,총장62"
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from services.options import MAX_GROUPS, OptionGroup, OptionRow, generate_rows, parse_values


@dataclass
class Parsed:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _to_int(text: str) -> int | None:
    s = text.strip().replace(",", "").replace("개", "")
    return int(s) if re.fullmatch(r"\d+", s) else None


# --- 옵션 ---
@dataclass
class OptionsParsed(Parsed):
    groups: list[OptionGroup] = field(default_factory=list)


def parse_options(text: str) -> OptionsParsed:
    result = OptionsParsed()
    text = (text or "").strip()
    if not text:
        return result
    for part in text.split("/"):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            result.errors.append(f"옵션 형식이 잘못되었습니다: '{part}' (예: 색상:베이지,블랙)")
            continue
        name, values = part.split(":", 1)
        group = OptionGroup(name.strip(), parse_values(values))
        if not group.name or not group.values:
            result.errors.append(f"옵션명과 옵션값을 모두 입력하세요: '{part}'")
            continue
        result.groups.append(group)
    names = [g.name for g in result.groups]
    if len(names) > MAX_GROUPS:
        result.errors.append(f"옵션은 최대 {MAX_GROUPS}개 그룹까지입니다.")
    if len(set(names)) != len(names):
        result.errors.append("옵션명이 중복되었습니다.")
    return result


# --- 재고 ---
@dataclass
class StockParsed(Parsed):
    quantity: int = 0                                   # 옵션 없는 상품
    rows: list[OptionRow] = field(default_factory=list)  # 옵션 상품


def parse_stock(text: str, groups: list[OptionGroup]) -> StockParsed:
    result = StockParsed()
    text = (text or "").strip()
    if not text:
        result.errors.append("재고를 입력하세요.")
        return result

    single = _to_int(text)
    if not groups:
        if single is None:
            result.errors.append(f"옵션 없는 상품의 재고는 숫자로 입력하세요: '{text}'")
        else:
            result.quantity = single
        return result

    rows = generate_rows(groups, default_stock=single or 0)
    result.rows = rows
    if single is not None:
        return result

    by_key = {"-".join(r.values): r for r in rows}
    seen: set[str] = set()
    for entry in text.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            result.errors.append(f"재고 형식이 잘못되었습니다: '{entry}' (예: 베이지-S:3)")
            continue
        key, qty_text = entry.rsplit(":", 1)
        key = "-".join(p.strip() for p in key.split("-"))
        qty = _to_int(qty_text)
        row = by_key.get(key)
        if row is None:
            result.errors.append(f"재고의 조합 '{key}'이(가) 옵션에 없습니다.")
        elif qty is None:
            result.errors.append(f"재고 수량을 숫자로 입력하세요: '{entry}'")
        else:
            row.stock = qty
            seen.add(key)
    missing = [k for k in by_key if k not in seen]
    if missing and not result.errors:
        result.warnings.append(f"재고를 적지 않은 조합 {len(missing)}개는 0개로 등록: {', '.join(missing[:5])}"
                               + (" 외" if len(missing) > 5 else ""))
    return result


# --- 실측 ---
@dataclass
class MeasurementsParsed(Parsed):
    sizes: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # 사이즈 → [(항목, 값)]

    def columns(self) -> list[str]:
        cols: dict[str, None] = {}
        for items in self.sizes.values():
            for name, _ in items:
                cols.setdefault(name, None)
        return list(cols)


_MEASURE_ITEM = re.compile(r"^\s*([^\d\s][^\d]*?)\s*(\d+(?:\.\d+)?)\s*(cm)?\s*$")


def parse_measurements(text: str) -> MeasurementsParsed:
    result = MeasurementsParsed()
    text = (text or "").strip()
    if not text:
        return result
    for part in text.split("/"):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            result.errors.append(f"실측 형식이 잘못되었습니다: '{part}' (예: S:어깨40,가슴50)")
            continue
        size, items = part.split(":", 1)
        parsed = []
        for item in items.split(","):
            if not item.strip():
                continue
            m = _MEASURE_ITEM.match(item)
            if not m:
                result.errors.append(f"실측 항목은 '이름+숫자'로 입력하세요: '{item.strip()}' (예: 어깨40)")
                continue
            parsed.append((m.group(1).strip(), m.group(2)))
        if size.strip() in result.sizes:
            result.errors.append(f"실측 사이즈가 중복되었습니다: {size.strip()}")
        result.sizes[size.strip()] = parsed
    return result
