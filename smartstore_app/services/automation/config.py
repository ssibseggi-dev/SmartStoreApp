"""자동화 설정 — 분류 매핑(분류명별 카테고리·템플릿·마진·상품명 규칙)과 공통 설정."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from storage.db import Database

_SETTINGS_KEY = "automation"


def default_work_dir() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents" / "스마트스토어"


@dataclass
class CategoryMapping:
    name: str                      # 분류명 (엑셀 '분류' 칸 값)
    category_id: str = ""
    delivery_template: str = ""    # 비우면 기본(★) 템플릿
    claim_template: str = ""
    as_template: str = ""
    notice_template: str = ""
    margin_rate: float = 30.0      # %
    detail_template: str = "기본"   # 4단계 상세페이지 템플릿 이름
    name_rule: str = "{원본상품명}"
    brand: str = ""
    keyword: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "CategoryMapping":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def template_names(self) -> dict[str, str]:
        """services.templates 종류별 템플릿 이름."""
        return {"delivery": self.delivery_template, "claim": self.claim_template,
                "as": self.as_template, "notice": self.notice_template}


# 배경 제거 품질 → rembg 모델 (처음 사용할 때 ~/.rembg/models에 한 번 내려받음)
BG_MODELS = {"high": "birefnet-general",         # 약 970MB, 상품당 약 11초 — 복잡한 배경·틈까지 가장 깔끔
             "normal": "birefnet-general-lite",  # 약 220MB, 약 6초 — 복잡한 배경에서 가끔 배경이 남음
             "fast": "silueta"}                  # 약 40MB, 약 1초 — 소매 틈을 메우거나 가장자리가 거침


@dataclass
class AutomationSettings:
    fee_rate: float = 5.5          # 판매 수수료율 %
    round_unit: int = 100          # 올림 단위(원)
    price_ending: int = 900        # 끝자리(천 원 미만 부분). 0이면 사용 안 함
    banned_words: list[str] = field(default_factory=list)
    daily_limit: int = 30          # 하루 최대 등록 수
    work_excel: str = ""           # 작업 엑셀 경로 (비우면 기본 위치)
    image_root: str = ""           # 상품 사진 폴더 (비우면 작업 엑셀 옆 '상품이미지')
    live_publish: bool = False     # True면 판매중으로 바로 등록 (기본: 판매중지 검수 모드)
    image_square_mode: str = "pad"  # 사진 정사각형: "pad"(여백 채우기) | "crop"(가운데 크롭)
    image_enhance: bool = True      # 자동 밝기·화이트밸런스
    remove_background: bool = False # 대표 이미지 배경 제거(rembg) 후 흰 배경
    bg_quality: str = "high"        # 배경 제거 품질: "high" | "normal" | "fast" (BG_MODELS)
    notice_image: str = ""          # 상세페이지 상단 공지 이미지 (비우면 넣지 않음)
    notice_title: str = ""          # 상단 공지 문구 (이미지가 없을 때 템플릿이 띠로 만듦)
    notice_subtitle: str = ""       # 상단 공지 보조 문구
    publish_naver_shopping: bool = True  # 검수 후 판매 시작할 때 네이버쇼핑에도 노출
    schedule_times: list[str] = field(default_factory=lambda: ["10:00", "18:00"])  # 예약 실행 시각
    kakao_enabled: bool = False      # 예약 실행 결과를 카카오톡으로 알림
    kakao_notify_app: bool = True    # 앱에서 [대기 상품 처리]를 눌렀을 때도 알림
    kakao_redirect_uri: str = "https://localhost:3000"
    kakao_link_url: str = "https://sell.smartstore.naver.com/"   # 알림 버튼 링크 (카카오 앱에 도메인 등록 필요)
    photo_inbox: str = ""            # 폰 사진이 동기화되는 폴더 (비우면 사진 분리 안 함)
    photo_max_gap_hours: int = 3     # QR 카드 뒤 이 시간 안에 찍은 사진만 그 상품으로 분류
    ai_enabled: bool = True          # 상품명(최종)·설명(최종)이 비면 AI 초안 사용 (Claude API 키가 있을 때만)
    ai_model: str = "claude-opus-5-5"

    @classmethod
    def from_dict(cls, data: dict) -> "AutomationSettings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def work_excel_path(self) -> Path:
        return Path(self.work_excel) if self.work_excel else default_work_dir() / "상품입력.xlsx"

    def image_root_path(self) -> Path:
        return Path(self.image_root) if self.image_root else self.work_excel_path().parent / "상품이미지"

    def image_options(self):
        from services.images.processing import ImageOptions
        return ImageOptions(square_mode=self.image_square_mode, enhance=self.image_enhance,
                            remove_background=self.remove_background,
                            bg_model=BG_MODELS.get(self.bg_quality, BG_MODELS["high"]))


def load_settings(db: Database) -> AutomationSettings:
    raw = db.get_setting(_SETTINGS_KEY)
    return AutomationSettings.from_dict(json.loads(raw)) if raw else AutomationSettings()


def save_settings(db: Database, settings: AutomationSettings) -> None:
    db.set_setting(_SETTINGS_KEY, json.dumps(asdict(settings), ensure_ascii=False))


def load_mappings(db: Database) -> dict[str, CategoryMapping]:
    return {m.name: m for m in (CategoryMapping.from_dict(d) for d in db.list_mappings())}


def save_mappings(db: Database, mappings: list[CategoryMapping]) -> None:
    names = [m.name.strip() for m in mappings]
    if any(not n for n in names):
        raise ValueError("분류명이 비어 있는 행이 있습니다.")
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"분류명이 중복되었습니다: {', '.join(dupes)}")
    db.replace_mappings([asdict(m) | {"name": m.name.strip()} for m in mappings])


def parse_banned_words(text: str) -> list[str]:
    """쉼표·줄바꿈으로 구분한 금지어 목록."""
    words: dict[str, None] = {}
    for part in text.replace("\n", ",").split(","):
        if part.strip():
            words.setdefault(part.strip(), None)
    return list(words)
