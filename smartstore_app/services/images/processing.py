"""상품 사진 보정 — 원본/ → 보정/ (원본은 읽기만 한다).

상품 사진: EXIF 회전 → (대표 이미지만, 설정 시) 배경 제거 후 흰 배경 → 자동 밝기·화이트밸런스
          → 정사각형 1000×1000 (여백 채우기 또는 가운데 크롭) → JPEG 압축(용량 제한 이하)
상세용 사진(파일명에 detail): EXIF 회전 → 가로 최대 1000px로 축소 → JPEG 압축 (보정·크롭 없음)
이미 처리한 사진은 보정/.processed.json 기록과 비교해 다시 처리하지 않는다(원본이나 설정이 바뀌면 다시 처리).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image, ImageEnhance, ImageOps, ImageStat

from services.images import (
    EDITED_DIR, SOURCE_EXTENSIONS, is_detail, list_images, original_folder, product_folder,
)

log = logging.getLogger(__name__)

MANIFEST = ".processed.json"
WHITE = (255, 255, 255)

_heif_registered = False


def register_heif() -> None:
    global _heif_registered
    if not _heif_registered:
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            log.warning("pillow-heif가 없어 HEIC 사진을 읽을 수 없습니다.")
        _heif_registered = True


@dataclass
class ImageOptions:
    size: int = 1000
    square_mode: str = "pad"          # "pad"(여백 채우기) | "crop"(가운데 크롭)
    enhance: bool = True              # 자동 밝기·화이트밸런스
    remove_background: bool = False   # 대표 이미지만 배경 제거 후 흰 배경
    bg_model: str = "birefnet-general"   # rembg 모델 (처음 사용 시 내려받음, 설정 BG_MODELS)
    bg_version: int = 7               # 배경 제거 후처리를 바꾸면 올림 → 대표 이미지 다시 처리
    enhance_version: int = 2          # 자동 보정 방식을 바꾸면 올림 → 상품 사진 다시 처리
    max_bytes: int = 2_000_000        # 보정본 최대 용량 (네이버 업로드 제한 10MB보다 작게)
    detail_width: int = 1000

    def signature(self, role: str) -> str:
        data = asdict(self)
        if role != "representative":
            data.pop("remove_background")
            data.pop("bg_model")
            data.pop("bg_version")
        if role == "detail":
            data = {"detail_width": self.detail_width, "max_bytes": self.max_bytes}
        return hashlib.sha1(json.dumps([role, data], sort_keys=True).encode()).hexdigest()[:12]


@dataclass
class ProcessResult:
    code: str
    processed: list[Path] = field(default_factory=list)
    skipped: int = 0
    removed: list[str] = field(default_factory=list)   # 원본이 없어져 지운 보정본
    errors: list[str] = field(default_factory=list)
    background_removed: bool = False

    def summary(self) -> str:
        parts = [f"보정 {len(self.processed)}장", f"건너뜀 {self.skipped}장"]
        if self.background_removed:
            parts.append("대표 이미지 배경 제거")
        if self.removed:
            parts.append(f"정리 {len(self.removed)}장")
        if self.errors:
            parts.append(f"오류 {len(self.errors)}건")
        return " · ".join(parts)


# --- 단위 보정 함수 ---
def load_image(path: Path) -> Image.Image:
    if path.suffix.lower() in (".heic", ".heif"):
        register_heif()
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, WHITE)
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return img.convert("RGB")


def _percentile(hist: list[int], q: float) -> int:
    target, acc = sum(hist) * q, 0
    for value, count in enumerate(hist):
        acc += count
        if acc >= target:
            return value
    return 255


def white_balance_factors(img: Image.Image) -> tuple[float, float, float] | None:
    """밝고 무채색에 가까운 부분(흰 벽·배경 등)의 색 기울기로 화이트밸런스 배율을 구한다.

    상품 색은 보지 않으므로, 색이 강한 상품(네이비 셔츠 등)이 화면을 채워도 사진을 물들이지 않는다.
    기준이 될 부분이 없거나 이미 중성이면 None (보정하지 않음).
    """
    try:
        import numpy as np
    except ImportError:
        return None
    small = np.asarray(img.resize((200, 200)), dtype=float)
    lum = small @ np.array([0.299, 0.587, 0.114])
    chroma = small.max(axis=2) - small.min(axis=2)
    bright = max(float(np.percentile(lum, 90)), 120.0)
    mask = (lum >= bright) & (lum <= 253) & (chroma <= 0.2 * lum + 8)   # 하얗게 날아간 픽셀은 판단에서 제외
    if mask.sum() < 0.005 * mask.size:
        return None
    ref = small[mask].mean(axis=0)
    factors = [max(0.9, min(1.1, 1 + (ref.mean() / c - 1) * 0.7)) for c in ref]
    return None if max(abs(f - 1) for f in factors) < 0.02 else tuple(factors)


def auto_enhance(img: Image.Image) -> Image.Image:
    """가벼운 자동 보정. 사진을 망치지 않도록 약하게 적용한다.

    1) 레벨: 명암 차가 충분한 사진만 자동 대비(절반 강도)
    2) 화이트밸런스: 밝은 무채색 부분이 한쪽 색으로 기울었을 때만 바로잡음(±10% 제한)
    3) 밝기: 평균 밝기가 낮으면 밝게(최대 1.35배)
    """
    hist = img.convert("L").histogram()
    if _percentile(hist, 0.99) - _percentile(hist, 0.01) > 60:
        img = Image.blend(img, ImageOps.autocontrast(img, cutoff=0.5, preserve_tone=True), 0.5)

    factors = white_balance_factors(img)
    if factors:
        img = Image.merge("RGB", [ch.point(lambda v, f=f: min(255, int(v * f + 0.5)))
                                  for ch, f in zip(img.split(), factors)])

    luminance = ImageStat.Stat(img.convert("L")).mean[0]
    if luminance < 115:
        img = ImageEnhance.Brightness(img).enhance(min(1.35, 125 / max(luminance, 1)))
    return img


def to_square(img: Image.Image, size: int, mode: str = "pad") -> Image.Image:
    if mode == "crop":
        return ImageOps.fit(img, (size, size), Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    fitted = ImageOps.contain(img, (size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (size, size), WHITE)
    canvas.paste(fitted, ((size - fitted.width) // 2, (size - fitted.height) // 2))
    return canvas


def on_white_centered(cutout: Image.Image, size: int, margin: float = 0.08) -> Image.Image:
    """배경을 지운 RGBA 이미지를 물체 영역만 잘라 흰 정사각형 가운데에 놓는다."""
    alpha = cutout.split()[-1]
    box = alpha.point(lambda a: 255 if a > 16 else 0).getbbox()
    if box:
        cutout = cutout.crop(box)
    inner = int(size * (1 - 2 * margin))
    cutout = ImageOps.contain(cutout, (inner, inner), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (size, size), WHITE)
    canvas.paste(cutout, ((size - cutout.width) // 2, (size - cutout.height) // 2), mask=cutout.split()[-1])
    return canvas


def save_jpeg(img: Image.Image, path: Path, max_bytes: int) -> int:
    """용량 제한 안에 들어갈 때까지 품질을 낮춰 저장한다. 저장한 바이트 수를 돌려준다."""
    data = b""
    for quality in (90, 85, 80, 75, 70, 65, 60):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            break
    path.write_bytes(data)
    return len(data)


# --- 배경 제거 (rembg) ---
_sessions: dict[str, object] = {}


def rembg_remover(model: str) -> Callable[[Image.Image], Image.Image]:
    def remove(img: Image.Image) -> Image.Image:
        from rembg import new_session, remove as rembg_remove
        if model not in _sessions:
            _sessions[model] = new_session(model)   # 처음 사용 시 모델 다운로드(~/.rembg/models)
        # 마스크만 받아 원래 색 위에 씌운다 (rembg 결과는 배경 부분 색을 지워 안쪽을 다시 채울 수 없음)
        # post_process_mask: 반투명 경계를 없앤다. 흰 옷은 반투명 경계가 희미한 회색 잔상·빈틈으로 보이므로
        # 경계는 또렷하게 자른 뒤 clean_cutout에서 아주 살짝만 다듬는다
        mask = rembg_remove(img, session=_sessions[model], only_mask=True, post_process_mask=True)
        cutout = img.convert("RGBA")
        cutout.putalpha(mask.convert("L").resize(img.size))
        return cutout
    return remove


def fill_enclosed(cutout: Image.Image, outline_ratio: float = 0.5, pinhole_ratio: float = 0.003) -> Image.Image:
    """모델이 상품 안쪽을 배경으로 잘못 지운 경우 채운다 (원래 색 그대로).

    - 흰 옷을 밝은 배경에서 찍으면 윤곽선만 남는다(남은 부분이 둘러싼 넓이의 outline_ratio 미만)
      → 둘러싸인 안쪽을 모두 채운다.
    - 그 밖에는 아주 작은 구멍(단추 사이 등)만 채운다. 소매가 치마에 닿아 생긴 틈처럼 큰 구멍은
      실제 배경이므로 지운 채로 둔다.
    """
    try:
        import numpy as np
        from scipy import ndimage
    except ImportError:
        return cutout
    alpha = np.array(cutout.split()[-1])
    solid = alpha > 128
    filled = ndimage.binary_fill_holes(solid)
    holes = filled & ~solid
    if not holes.any():
        return cutout
    if solid.sum() >= filled.sum() * outline_ratio:
        labels, count = ndimage.label(holes)
        areas = ndimage.sum(np.ones_like(alpha), labels, index=range(1, count + 1))
        small = [i + 1 for i, area in enumerate(areas) if area < filled.sum() * pinhole_ratio]
        holes = np.isin(labels, small)
        if not holes.any():
            return cutout
    alpha[holes] = 255
    result = cutout.copy()
    result.putalpha(Image.fromarray(alpha))
    return result


def defringe(cutout: Image.Image, band_ratio: float = 0.004, tolerance: float = 14.0) -> Image.Image:
    """가장자리에 남은 배경색 띠를 지운다.

    모델 마스크는 상품보다 1~3px 넓게 잡혀 원래 배경색(베이지·회색) 띠가 남고, 흰 배경 위에서는
    지저분한 테두리로 보인다. 가장자리 띠 안에서 '근처 배경색과 거의 같고, 근처 상품 색보다 배경색에
    훨씬 가까운' 픽셀만 투명하게 한다. 흰 옷처럼 상품 색이 배경과 비슷하면 건드리지 않는다.
    """
    try:
        import numpy as np
        from scipy import ndimage
    except ImportError:
        return cutout
    rgba = np.array(cutout.convert("RGBA"))
    solid = rgba[..., 3] > 128
    if not solid.any():
        return cutout
    band_px = max(2, round(max(cutout.size) * band_ratio))
    size = band_px * 4 + 1
    inner = ndimage.binary_erosion(solid, iterations=band_px)
    band = solid & ~inner
    ring = ndimage.binary_dilation(solid, iterations=band_px) & ~solid      # 바로 바깥 배경
    if ring.sum() < 50 or not inner.any():
        return cutout
    rgb = rgba[..., :3].astype(np.float32)

    def local_mean(region):
        """region 픽셀만으로 낸 근처 평균색 (근처에 region이 없으면 None 자리는 nan)."""
        weight = ndimage.uniform_filter(region.astype(np.float32), size=size)
        mean = np.stack([ndimage.uniform_filter(rgb[..., c] * region, size=size) for c in range(3)], -1)
        ok = weight > 1e-3
        mean[ok] /= weight[ok][:, None]
        mean[~ok] = np.nan
        return mean

    d_bg = np.linalg.norm(rgb - local_mean(ring), axis=-1)
    d_fg = np.linalg.norm(rgb - local_mean(inner), axis=-1)
    with np.errstate(invalid="ignore"):
        fringe = band & (d_bg < tolerance) & (d_fg > d_bg * 2 + tolerance)
    if not fringe.any():
        return cutout
    rgba[..., 3][fringe] = 0
    return Image.fromarray(rgba, "RGBA")


def feather(cutout: Image.Image) -> Image.Image:
    """경계의 점 모양 잔여 픽셀을 정리하고 아주 살짝 부드럽게 해 계단 모양을 없앤다."""
    from PIL import ImageFilter
    alpha = cutout.split()[-1].filter(ImageFilter.MedianFilter(3))
    result = cutout.copy()
    result.putalpha(alpha.filter(ImageFilter.GaussianBlur(max(0.5, max(cutout.size) / 2000))))
    return result


def drop_small_parts(cutout: Image.Image, min_ratio: float = 0.15) -> Image.Image:
    """물체와 떨어진 작은 덩어리(바닥 그림자 등)를 지운다. 가장 큰 덩어리의 min_ratio보다 작으면 제거."""
    try:
        import numpy as np
        from scipy import ndimage
    except ImportError:
        return cutout
    alpha = np.array(cutout.split()[-1])
    labels, count = ndimage.label(alpha > 128)
    if count <= 1:
        return cutout
    areas = ndimage.sum(np.ones_like(alpha), labels, index=range(1, count + 1))
    keep = [i + 1 for i, area in enumerate(areas) if area >= areas.max() * min_ratio]
    # 남길 덩어리를 조금 넓혀 가장자리의 반투명 픽셀은 살린다
    mask = ndimage.binary_dilation(np.isin(labels, keep), iterations=3)
    alpha[~mask] = 0
    result = cutout.copy()
    result.putalpha(Image.fromarray(alpha))
    return result


def clean_cutout(cutout: Image.Image) -> Image.Image:
    """배경 제거 후처리: 흰 옷 안쪽 채우기 → 떨어진 그림자 지우기 → 배경색 테두리 지우기 → 경계 다듬기."""
    return feather(defringe(drop_small_parts(fill_enclosed(cutout))))


# --- 상품 단위 처리 ---
def _role(path: Path, representative: Path | None) -> str:
    if is_detail(path):
        return "detail"
    return "representative" if path == representative else "optional"


def process_product(root: Path, code: str, options: ImageOptions,
                    remover: Callable[[Image.Image], Image.Image] | None = None) -> ProcessResult:
    result = ProcessResult(code)
    base = product_folder(root, code)
    source_dir = original_folder(root, code)
    originals = list_images(source_dir, SOURCE_EXTENSIONS)
    out_dir = base / EDITED_DIR
    manifest_path = out_dir / MANIFEST
    try:
        manifest: dict = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    if not originals:
        result.errors.append(f"원본 사진이 없습니다: {source_dir}")
        return result
    out_dir.mkdir(parents=True, exist_ok=True)

    main = [p for p in originals if not is_detail(p)]
    representative = main[0] if main else None
    outputs: dict[str, str] = {}
    for src in originals:
        role = _role(src, representative)
        stat = src.stat()
        target = out_dir / f"{src.stem}.jpg"
        if target.name in outputs.values():                     # 01.png와 01.jpg처럼 이름이 겹칠 때
            target = out_dir / f"{src.stem}_{src.suffix.lstrip('.').lower()}.jpg"
        signature = options.signature(role)
        entry = manifest.get(src.name)
        outputs[src.name] = target.name
        if (entry and entry.get("size") == stat.st_size and entry.get("mtime") == stat.st_mtime
                and entry.get("sig") == signature and entry.get("output") == target.name and target.exists()):
            result.skipped += 1
            continue
        try:
            img = load_image(src)
            if role == "detail":
                if img.width > options.detail_width:
                    img = img.resize((options.detail_width, round(img.height * options.detail_width / img.width)),
                                     Image.Resampling.LANCZOS)
            else:
                if options.enhance:
                    img = auto_enhance(img)   # 배경 제거 전에 보정해야 흰 배경이 물들지 않는다
                cut = None
                if role == "representative" and options.remove_background:
                    try:
                        cut = clean_cutout((remover or rembg_remover(options.bg_model))(img))
                        result.background_removed = True
                    except Exception as e:   # 모델 다운로드 실패 등 → 배경 제거 없이 진행
                        log.warning("[%s] 배경 제거 실패, 원래 배경 유지: %s", code, e)
                        result.errors.append(f"배경 제거 실패(원래 배경 유지): {e}")
                img = on_white_centered(cut, options.size) if cut is not None else \
                    to_square(img, options.size, options.square_mode)
            size = save_jpeg(img, target, options.max_bytes)
        except Exception as e:
            result.errors.append(f"{src.name} 보정 실패: {e}")
            continue
        manifest[src.name] = {"size": stat.st_size, "mtime": stat.st_mtime, "sig": signature,
                              "output": target.name, "role": role}
        result.processed.append(target)
        log.info("[%s] 사진 보정: %s → %s (%s, %d KB)", code, src.name, target.name, role, size // 1024)

    # 원본이 사라졌거나 이름이 바뀐 보정본 정리 (앱이 만든 파일만)
    for name in list(manifest):
        if name not in outputs:
            stale = out_dir / manifest.pop(name)["output"]
            if stale.name not in outputs.values():
                stale.unlink(missing_ok=True)
                result.removed.append(stale.name)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return result
