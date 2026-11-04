"""조합형 옵션 — 옵션 그룹(최대 3개)으로 조합표를 만들고, 커머스API optionInfo로 변환한다.

originProduct.detailAttribute.optionInfo = {
  "optionCombinationSortType": "CREATE",
  "optionCombinationGroupNames": {"optionGroupName1": "색상", "optionGroupName2": "사이즈"},
  "optionCombinations": [{"optionName1": "블랙", "optionName2": "S", "stockQuantity": 10, "price": 0, "usable": true}],
  "useStockManagement": true
}
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

MAX_GROUPS = 3
MAX_NAME_LEN = 25


@dataclass
class OptionGroup:
    name: str = ""
    values: list[str] = field(default_factory=list)


@dataclass
class OptionRow:
    values: list[str] = field(default_factory=list)   # 그룹 순서대로 옵션값
    price: int = 0                                    # 추가금액(판매가 대비, 음수 가능)
    stock: int = 0
    seller_code: str = ""
    usable: bool = True


def parse_values(text: str) -> list[str]:
    """쉼표로 구분한 옵션값을 정리한다. 공백 제거, 빈 값·중복 제거(순서 유지)."""
    seen: dict[str, None] = {}
    for v in text.split(","):
        v = v.strip()
        if v:
            seen.setdefault(v, None)
    return list(seen)


def generate_rows(groups: list[OptionGroup], existing: list[OptionRow] | None = None,
                  default_stock: int = 0) -> list[OptionRow]:
    """그룹 값의 모든 조합으로 행을 만든다. 기존 행과 조합이 같으면 가격·재고 등 입력값을 유지한다."""
    groups = [g for g in groups if g.name.strip() and g.values]
    if not groups:
        return []
    kept = {tuple(r.values): r for r in (existing or [])}
    rows = []
    for combo in itertools.product(*(g.values for g in groups)):
        old = kept.get(combo)
        rows.append(OptionRow(list(combo), old.price, old.stock, old.seller_code, old.usable) if old
                    else OptionRow(list(combo), 0, default_stock))
    return rows


def total_stock(rows: list[OptionRow]) -> int:
    return sum(r.stock for r in rows if r.usable)


def price_range(sale_price: int) -> tuple[int, int]:
    """판매가 기준 옵션 추가금액 허용 범위 (스마트스토어 옵션가 정책)."""
    if sale_price < 2000:
        return 0, sale_price
    if sale_price < 10000:
        return -(sale_price // 2), sale_price
    return -(sale_price // 2), sale_price // 2


def build_option_info(groups: list[OptionGroup], rows: list[OptionRow]) -> dict:
    groups = [g for g in groups if g.name.strip() and g.values]
    combinations = []
    for r in rows:
        item = {f"optionName{i}": v for i, v in enumerate(r.values, start=1)}
        item.update(stockQuantity=r.stock, price=r.price, usable=r.usable)
        if r.seller_code.strip():
            item["sellerManagerCode"] = r.seller_code.strip()
        combinations.append(item)
    return {
        "optionCombinationSortType": "CREATE",
        "optionCombinationGroupNames": {f"optionGroupName{i}": g.name.strip()
                                        for i, g in enumerate(groups, start=1)},
        "optionCombinations": combinations,
        "useStockManagement": True,
    }


def validate_options(groups: list[OptionGroup], rows: list[OptionRow], sale_price: int) -> list[str]:
    errors: list[str] = []
    groups = [g for g in groups if g.name.strip() or g.values]
    if not groups:
        return ["옵션 그룹을 1개 이상 입력하세요."]
    if len(groups) > MAX_GROUPS:
        errors.append(f"옵션 그룹은 최대 {MAX_GROUPS}개입니다.")
    names = [g.name.strip() for g in groups]
    if any(not n for n in names):
        errors.append("옵션명을 입력하세요.")
    if len(set(names)) != len(names):
        errors.append("옵션명이 중복되었습니다.")
    for g in groups:
        if not g.values:
            errors.append(f"'{g.name}' 옵션값을 입력하세요.")
        if len(g.name.strip()) > MAX_NAME_LEN:
            errors.append(f"옵션명은 {MAX_NAME_LEN}자 이하로 입력하세요: {g.name}")
        long = [v for v in g.values if len(v) > MAX_NAME_LEN]
        if long:
            errors.append(f"옵션값은 {MAX_NAME_LEN}자 이하로 입력하세요: {', '.join(long)}")
    if errors:
        return errors

    if not rows:
        return ["'조합 생성'을 눌러 옵션 조합표를 만드세요."]
    expected = {tuple(c) for c in itertools.product(*(g.values for g in groups))}
    if {tuple(r.values) for r in rows} != expected or len(rows) != len(expected):
        errors.append("옵션값이 바뀌었습니다. '조합 생성'을 다시 누르세요.")

    usable = [r for r in rows if r.usable]
    if not usable:
        errors.append("사용하는 옵션 조합이 1개 이상 있어야 합니다.")
    if not any(r.price == 0 for r in usable):
        errors.append("추가금액이 0원인 옵션이 1개 이상 있어야 합니다.")
    lo, hi = price_range(sale_price)
    bad_price = [r for r in rows if r.price % 10 or not (lo <= r.price <= hi)]
    if bad_price:
        errors.append(f"옵션 추가금액은 10원 단위, {lo:,}원 ~ {hi:,}원 범위여야 합니다. "
                      f"(예: {' / '.join(bad_price[0].values)} = {bad_price[0].price:,}원)")
    if any(not (0 <= r.stock <= 99_999_999) for r in rows):
        errors.append("옵션 재고는 0 ~ 99,999,999 사이여야 합니다.")
    return errors
