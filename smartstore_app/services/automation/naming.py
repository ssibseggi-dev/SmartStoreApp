"""상품명 규칙 적용과 금지어 검사.

규칙 예: "{브랜드} {핵심키워드} {원본상품명}" — 값이 비어 있는 토큰은 빠지고 공백은 하나로 정리한다.
"""
from __future__ import annotations

import re

NAME_TOKENS = ("브랜드", "핵심키워드", "원본상품명", "분류")
_TOKEN = re.compile(r"\{([^{}]*)\}")


def unknown_tokens(rule: str) -> list[str]:
    return [t for t in _TOKEN.findall(rule) if t not in NAME_TOKENS]


def render_name(rule: str, values: dict[str, str]) -> str:
    bad = unknown_tokens(rule)
    if bad:
        raise ValueError(f"상품명 규칙에 알 수 없는 항목이 있습니다: {', '.join('{' + b + '}' for b in bad)} "
                         f"(사용 가능: {', '.join('{' + t + '}' for t in NAME_TOKENS)})")
    name = _TOKEN.sub(lambda m: (values.get(m.group(1)) or "").strip(), rule)
    return re.sub(r"\s+", " ", name).strip()


def find_banned(text: str, banned_words: list[str]) -> list[str]:
    lowered = text.lower()
    return [w for w in banned_words if w and w.lower() in lowered]
