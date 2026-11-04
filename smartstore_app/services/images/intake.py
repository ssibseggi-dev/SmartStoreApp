"""상품 사진 넣기 — 고른 사진을 사진 폴더 규칙대로 이름 붙여 {사진 폴더}/{상품코드}/원본/ 에 복사한다.

이름 규칙 (이름순이 곧 등록 순서)
  00_대표_*    대표 이미지 1장
  01_* ~ 09_*  추가 이미지 최대 9장
  90_detail_*  상세페이지 이미지 (파일명에 detail → 상세페이지에만 들어감)

고른 원래 파일은 옮기거나 지우지 않는다. 상품 폴더에 이미 사진이 있으면 지우지 않고
원본/_이전_{시각}/ 으로 옮겨 둔다 (그 안의 사진은 등록에 쓰이지 않는다).
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from services.images import (
    MAX_PRODUCT_IMAGES, ORIGINAL_DIR, SOURCE_EXTENSIONS, is_detail, list_images, original_folder, product_folder,
)

MAX_OPTIONAL = MAX_PRODUCT_IMAGES - 1   # 대표 1장 + 추가 9장
ARCHIVE_PREFIX = "_이전_"
_CODE_RE = re.compile(r"^[A-Za-z0-9가-힣_-]+$")
_LEADING_ORDER = re.compile(r"^(?:추가|상세|detail|대표)?[\s_-]*\d+[\s_.-]*", re.IGNORECASE)


@dataclass
class PhotoPlan:
    representative: Path | None = None
    optional: list[Path] = field(default_factory=list)
    detail: list[Path] = field(default_factory=list)

    @property
    def count(self) -> int:
        return (1 if self.representative else 0) + len(self.optional) + len(self.detail)


@dataclass
class SaveResult:
    folder: Path
    saved: list[Path] = field(default_factory=list)
    archived: Path | None = None   # 기존 사진을 옮겨 둔 폴더


def validate_code(code: str) -> str | None:
    code = code.strip()
    if not code:
        return "상품코드를 입력하세요."
    if not _CODE_RE.match(code):
        return "상품코드에는 글자·숫자·_·- 만 쓸 수 있습니다."
    return None


def validate_plan(plan: PhotoPlan) -> list[str]:
    errors = []
    if not plan.representative:
        errors.append("대표 이미지를 1장 넣으세요.")
    if len(plan.optional) > MAX_OPTIONAL:
        errors.append(f"추가 이미지는 최대 {MAX_OPTIONAL}장입니다. (지금 {len(plan.optional)}장)")
    files = [plan.representative, *plan.optional, *plan.detail] if plan.representative else [*plan.optional, *plan.detail]
    for path in files:
        if path.suffix.lower() not in SOURCE_EXTENSIONS:
            errors.append(f"사진 파일이 아닙니다: {path.name}")
        elif not path.is_file():
            errors.append(f"파일이 없습니다: {path}")
    return errors


def existing_photos(root: Path, code: str) -> list[Path]:
    """이 상품에 이미 들어 있는 원본 사진 (원본/ 또는 상품 폴더에 바로 넣은 사진)."""
    return list_images(original_folder(root, code), SOURCE_EXTENSIONS)


def _clean_stem(stem: str) -> str:
    """'추가01_모델컷' → '모델컷', '00_대표_4컬러' → '4컬러' 처럼 앞의 순서 번호·'대표'를 떼어낸다.

    이미 저장된 사진을 다시 저장해도 이름이 길어지지 않게 한다.
    """
    cleaned = _LEADING_ORDER.sub("", stem).strip(" _-.")
    cleaned = re.sub(r"^대표[\s_-]+", "", cleaned)
    return re.sub(r"[\\/:*?\"<>|]", "_", cleaned)


def target_names(plan: PhotoPlan) -> list[tuple[Path, str]]:
    """(원래 파일, 저장할 이름) — 이름순 = 대표 → 추가 → 상세."""
    pairs = []
    if plan.representative:
        stem = _clean_stem(plan.representative.stem)
        pairs.append((plan.representative, f"00_대표{'_' + stem if stem else ''}{plan.representative.suffix.lower()}"))
    for i, path in enumerate(plan.optional, start=1):
        stem = _clean_stem(path.stem)
        pairs.append((path, f"{i:02}{'_' + stem if stem else ''}{path.suffix.lower()}"))
    for i, path in enumerate(plan.detail, start=1):
        pairs.append((path, f"90_detail_{i:02}{path.suffix.lower()}"))
    return pairs


def save_photos(root: Path, code: str, plan: PhotoPlan) -> SaveResult:
    """사진을 규칙대로 복사한다. 이미 있던 사진은 원본/_이전_{시각}/ 으로 옮긴다."""
    code = code.strip()
    error = validate_code(code)
    errors = ([error] if error else []) + validate_plan(plan)
    if errors:
        raise ValueError("\n".join(errors))
    base = product_folder(root, code)
    target = base / ORIGINAL_DIR
    result = SaveResult(target)
    old = existing_photos(root, code)
    moved: dict[Path, Path] = {}
    if old:
        archive = target / f"{ARCHIVE_PREFIX}{datetime.now():%Y%m%d-%H%M%S}"
        archive.mkdir(parents=True, exist_ok=False)
        for path in old:
            shutil.move(str(path), str(archive / path.name))
            moved[path.resolve()] = archive / path.name
        result.archived = archive
    plan = PhotoPlan(   # 기존 사진을 다시 고른 경우: 보관 폴더로 옮긴 파일에서 복사
        moved.get(plan.representative.resolve(), plan.representative),
        [moved.get(p.resolve(), p) for p in plan.optional],
        [moved.get(p.resolve(), p) for p in plan.detail],
    )
    target.mkdir(parents=True, exist_ok=True)
    for src, name in target_names(plan):
        dst = target / name
        shutil.copy2(src, dst)
        result.saved.append(dst)
    return result


# --- 원고 폴더 불러오기 ---
def _images_in(folder: Path) -> list[Path]:
    return list_images(folder, SOURCE_EXTENSIONS)


def _find_dir(folder: Path, *keywords: str) -> Path | None:
    for sub in sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("_")):
        if any(k in sub.name for k in keywords):
            return sub
    return None


def plan_from_manuscript(folder: Path) -> PhotoPlan:
    """원고 폴더(01_대표이미지 / 04_추가이미지 / 05_상세페이지\\분할)에서 사진 구성을 만든다.

    - 대표: '대표' 폴더의 이름순 첫 장 (보통 '대표이미지_A_…'가 추천 대표)
    - 추가: '추가' 폴더의 사진 이름순 (최대 9장)
    - 상세: '상세' 폴더 안의 '분할' 폴더 사진 이름순 (없으면 '상세' 폴더 바로 아래 사진)
    썸네일·누끼·'_'로 시작하는 폴더(이전 버전 등)는 쓰지 않는다.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"폴더가 없습니다: {folder}")
    plan = PhotoPlan()
    rep_dir = _find_dir(folder, "대표")
    if rep_dir and _images_in(rep_dir):
        plan.representative = _images_in(rep_dir)[0]
    add_dir = _find_dir(folder, "추가")
    if add_dir:
        plan.optional = _images_in(add_dir)[:MAX_OPTIONAL]
    detail_dir = _find_dir(folder, "상세")
    if detail_dir:
        split = _find_dir(detail_dir, "분할")
        plan.detail = _images_in(split) if split else [p for p in _images_in(detail_dir)
                                                         if not p.stem.endswith("전체")]
    if not plan.count:
        raise ValueError("원고 폴더에서 사진을 찾지 못했습니다. '대표', '추가', '상세' 가 들어간 하위 폴더가 있어야 합니다.")
    return plan


def plan_from_existing(root: Path, code: str) -> PhotoPlan:
    """이미 저장된 사진으로 구성을 만든다 (수정용). 이름순 첫 장 = 대표, detail = 상세, 나머지 = 추가."""
    files = existing_photos(root, code)
    main = [p for p in files if not is_detail(p)]
    return PhotoPlan(main[0] if main else None, main[1:], [p for p in files if is_detail(p)])
