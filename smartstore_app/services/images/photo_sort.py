"""촬영 사진 자동 분리 — 동기화 폴더의 새 사진을 촬영 시각순으로 읽어 상품 폴더로 복사한다.

규칙
- 앱이 만든 QR 카드(SSAPP:상품코드)가 찍힌 사진 = 그 상품의 시작 표시. 카드 사진 자체는 상품 사진으로 쓰지 않는다.
- 각 사진은 '그보다 먼저 찍힌 가장 최근 QR 카드'의 상품으로 → {사진 폴더}/{상품코드}/원본/
  (이전 실행에서 찍힌 카드도 기억하므로 사진이 나눠서 동기화돼도 이어진다)
- 카드가 없거나 카드 뒤 max_gap 시간이 넘게 지난 사진 → {사진 폴더}/미분류/
- 동기화 폴더의 사진은 복사만 하고 지우거나 옮기지 않는다 (클라우드 원본 보존). 처리한 사진은 DB에 기록해 다시 처리하지 않는다.
- 막 생긴(아직 동기화 중일 수 있는) 파일은 다음 실행으로 미룬다.
"""
from __future__ import annotations

import bisect
import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps

from services.images import ORIGINAL_DIR, SOURCE_EXTENSIONS
from services.images.qr_cards import parse_payload
from services.images.processing import register_heif

log = logging.getLogger(__name__)

UNSORTED_DIR = "미분류"
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_EXIF_IFD, _DATETIME_ORIGINAL, _DATETIME = 0x8769, 36867, 306


@dataclass
class SortReport:
    inbox: Path
    assigned: dict[str, list[Path]] = field(default_factory=dict)   # 상품코드 → 복사한 파일
    qr_cards: list[tuple[str, Path]] = field(default_factory=list)   # (상품코드, 카드 사진)
    unsorted: list[Path] = field(default_factory=list)               # 미분류로 복사한 파일
    already: int = 0                                                 # 이전에 처리한 사진
    deferred: int = 0                                                # 동기화 중일 수 있어 미룬 사진
    warnings: list[str] = field(default_factory=list)

    @property
    def copied(self) -> int:
        return sum(len(v) for v in self.assigned.values()) + len(self.unsorted)

    def summary(self) -> str:
        parts = [f"새 사진 {self.copied + len(self.qr_cards)}장",
                 f"상품 {len(self.assigned)}개에 {sum(len(v) for v in self.assigned.values())}장",
                 f"QR 카드 {len(self.qr_cards)}장"]
        if self.unsorted:
            parts.append(f"미분류 {len(self.unsorted)}장")
        if self.deferred:
            parts.append(f"동기화 대기 {self.deferred}장")
        return " · ".join(parts)


def capture_time(path: Path) -> datetime:
    """EXIF 촬영 시각, 없으면 파일 수정 시각."""
    try:
        if path.suffix.lower() in (".heic", ".heif"):
            register_heif()
        with Image.open(path) as img:
            exif = img.getexif()
            value = exif.get_ifd(_EXIF_IFD).get(_DATETIME_ORIGINAL) or exif.get(_DATETIME)
        if value:
            return datetime.strptime(str(value).strip()[:19], "%Y:%m:%d %H:%M:%S")
    except (OSError, ValueError) as e:
        log.debug("촬영 시각 읽기 실패 %s: %s", path.name, e)
    return datetime.fromtimestamp(path.stat().st_mtime)


def _qr_attempts(gray: Image.Image):
    """빠른 시도부터: 1600px → (카드가 작게 찍혔을 때) 2400px → 2400px 대비 보정."""
    for size in (1600, 2400):
        if size > 1600 and max(gray.size) <= 1600:
            break
        img = gray.copy()
        img.thumbnail((size, size))
        yield img
    yield ImageOps.autocontrast(img, cutoff=1)


def read_qr_code(path: Path) -> str | None:
    """사진 속 앱 QR 카드의 상품코드. 다른 QR(옷 태그 등)이나 QR이 없으면 None."""
    import zxingcpp
    if path.suffix.lower() in (".heic", ".heif"):
        register_heif()
    with Image.open(path) as img:
        gray = ImageOps.exif_transpose(img).convert("L")
    for attempt in _qr_attempts(gray):
        for result in zxingcpp.read_barcodes(attempt, formats=zxingcpp.BarcodeFormat.QRCode):
            code = parse_payload(result.text)
            if code:
                return code
    return None


def _copy_unique(src: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / src.name
    n = 1
    while target.exists():
        if target.stat().st_size == src.stat().st_size:
            return target          # 같은 사진을 이미 복사함
        target = folder / f"{src.stem}_{n}{src.suffix}"
        n += 1
    shutil.copy2(src, target)
    return target


def sort_photos(db, inbox: Path, root: Path, max_gap_hours: int = 3, known_codes: set[str] | None = None,
                min_age_seconds: int = 60, on_progress: Callable[[str, int, int], None] | None = None) -> SortReport:
    inbox, root = Path(inbox), Path(root)
    report = SortReport(inbox)
    if not inbox.is_dir():
        raise FileNotFoundError(f"촬영 사진 폴더가 없습니다: {inbox}")
    root_resolved = root.resolve()
    done = db.sorted_photo_keys()
    now = time.time()

    candidates = []
    for path in sorted(inbox.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        if root_resolved in path.resolve().parents:   # 사진 폴더가 동기화 폴더 안에 있어도 결과물은 다시 읽지 않음
            continue
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime)
        if key in done:
            report.already += 1
        elif now - stat.st_mtime < min_age_seconds:
            report.deferred += 1
        else:
            candidates.append((path, stat))

    # 1) 새 사진의 촬영 시각·QR 읽기
    photos = []
    for index, (path, stat) in enumerate(candidates, start=1):
        try:
            code = read_qr_code(path)
        except Exception as e:   # 깨진 파일 등은 미분류로
            report.warnings.append(f"{path.name}: 사진을 읽지 못했습니다 ({e})")
            code = None
        photos.append((capture_time(path), path, stat, code))
        if on_progress:
            on_progress(path.name, index, len(candidates))
    photos.sort(key=lambda p: (p[0], p[1].name))

    # 2) QR 카드 시간표 (이전 실행 + 이번에 찾은 카드)
    timeline = [(datetime.strptime(t, _TIME_FORMAT), c) for t, c in db.qr_card_timeline()]
    timeline += [(t, code) for t, _p, _s, code in photos if code]
    timeline.sort()
    times = [t for t, _ in timeline]
    gap = timedelta(hours=max_gap_hours)

    # 3) 분류·복사
    for taken, path, stat, code in photos:
        stamp = taken.strftime(_TIME_FORMAT)
        if code:
            report.qr_cards.append((code, path))
            if known_codes is not None and code not in known_codes:
                report.warnings.append(f"QR 카드 {code}는 작업 엑셀에 없는 상품코드입니다. 사진은 {code} 폴더에 모읍니다.")
            db.add_sorted_photo(str(path), stat.st_size, stat.st_mtime, stamp, "qr", code, None)
            continue
        i = bisect.bisect_right(times, taken) - 1
        card_time, card_code = timeline[i] if i >= 0 else (None, None)
        if card_code and taken - card_time <= gap:
            target = _copy_unique(path, root / card_code / ORIGINAL_DIR)
            report.assigned.setdefault(card_code, []).append(target)
            db.add_sorted_photo(str(path), stat.st_size, stat.st_mtime, stamp, "product", card_code, str(target))
        else:
            target = _copy_unique(path, root / UNSORTED_DIR)
            report.unsorted.append(target)
            db.add_sorted_photo(str(path), stat.st_size, stat.st_mtime, stamp, "unsorted", None, str(target))
    log.info("촬영 사진 분리: %s", report.summary())
    return report
