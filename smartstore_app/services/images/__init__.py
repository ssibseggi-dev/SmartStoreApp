"""상품 사진 폴더 규칙 — {사진 폴더}/{상품코드}/원본/, 보정본은 /보정/ (3단계).

이름순 첫 장 = 대표 이미지, 나머지 = 추가 이미지, 파일명에 'detail'이 들어간 사진 = 상세페이지용.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}          # 그대로 업로드 가능
SOURCE_EXTENSIONS = IMAGE_EXTENSIONS | {".heic", ".heif", ".webp"}     # 보정하면 쓸 수 있는 원본 (폰 사진 등)
ORIGINAL_DIR = "원본"
EDITED_DIR = "보정"
MAX_PRODUCT_IMAGES = 10  # 대표 1 + 추가 9


@dataclass
class ImageSet:
    folder: Path
    representative: Path | None = None
    optional: list[Path] = field(default_factory=list)
    detail: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)  # 장수 제한을 넘어 빠진 사진
    edited: bool = False                                # True: 보정본, False: 원본(등록 전에 보정함)

    @property
    def found(self) -> bool:
        return self.representative is not None


def product_folder(root: Path, code: str) -> Path:
    return Path(root) / code


def is_detail(path: Path) -> bool:
    return "detail" in path.stem.lower()


def list_images(folder: Path, extensions: set[str] = IMAGE_EXTENSIONS) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in extensions),
                  key=lambda p: p.name.lower())


def original_folder(root: Path, code: str) -> Path:
    """원본 사진 폴더. 원본/ 폴더 없이 상품 폴더에 바로 넣은 사진도 원본으로 쓴다."""
    base = product_folder(root, code)
    if not list_images(base / ORIGINAL_DIR, SOURCE_EXTENSIONS) and list_images(base, SOURCE_EXTENSIONS):
        return base
    return base / ORIGINAL_DIR


def product_images(root: Path, code: str) -> ImageSet:
    """보정본이 있으면 보정본, 없으면 원본(HEIC 등 보정 가능한 형식 포함)을 쓴다."""
    base = product_folder(root, code)
    edited = list_images(base / EDITED_DIR)
    originals = original_folder(root, code)
    files = edited or list_images(originals, SOURCE_EXTENSIONS)
    result = ImageSet(base / EDITED_DIR if edited else originals, edited=bool(edited))
    main = [p for p in files if not is_detail(p)]
    result.detail = [p for p in files if is_detail(p)]
    if main:
        result.representative = main[0]
        result.optional = main[1:MAX_PRODUCT_IMAGES]
        result.skipped = main[MAX_PRODUCT_IMAGES:]
    return result
