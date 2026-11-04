"""판매가 계산 — 판매가 = 원가 × (1 + 마진율) ÷ (1 - 수수료율) → 올림 단위 → 끝자리 규칙.

끝자리 규칙: 천 원 미만 부분을 price_ending으로 맞춘다. 올림한 값보다 작아지면 다음 천 원대로 넘긴다.
  예) 13,757 → 100원 올림 13,800 → 끝자리 900 → 13,900 / 13,950 → 14,000 → 14,900
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal


@dataclass
class PriceResult:
    price: int
    steps: list[str] = field(default_factory=list)

    def explain(self) -> str:
        return " → ".join(self.steps)


def calculate_price(cost: int, margin_rate: float, fee_rate: float, round_unit: int = 100,
                    price_ending: int = 0) -> PriceResult:
    if cost <= 0:
        raise ValueError("원가는 0보다 커야 합니다.")
    if not 0 <= fee_rate < 100:
        raise ValueError("수수료율은 0 이상 100 미만이어야 합니다.")
    if round_unit <= 0 or round_unit % 10:
        raise ValueError("올림 단위는 10원 단위여야 합니다.")
    if not 0 <= price_ending < 1000 or price_ending % 10:
        raise ValueError("끝자리는 0~990 사이 10원 단위여야 합니다.")

    margin = Decimal(str(margin_rate)) / 100
    fee = Decimal(str(fee_rate)) / 100
    raw = Decimal(cost) * (1 + margin) / (1 - fee)
    steps = [f"원가 {cost:,}원 × (1 + 마진 {margin_rate:g}%) ÷ (1 - 수수료 {fee_rate:g}%) = {raw:,.0f}원"]

    unit = Decimal(round_unit)
    rounded = int((raw / unit).to_integral_value(rounding=ROUND_CEILING) * unit)
    steps.append(f"{round_unit:,}원 단위 올림 {rounded:,}원")

    price = rounded
    if price_ending:
        price = rounded // 1000 * 1000 + price_ending
        if price < rounded:
            price += 1000
        steps.append(f"끝자리 {price_ending} 맞춤 {price:,}원")
    return PriceResult(price, steps)
