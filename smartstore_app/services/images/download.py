"""도매몰 이미지 URL → 사진 폴더(원본/) 다운로드. 이미 사진이 있으면 받지 않는다(원본은 덮어쓰지 않음)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import requests

from services.images import ORIGINAL_DIR, SOURCE_EXTENSIONS, list_images, original_folder, product_folder

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/130.0 Safari/537.36")
_SIGNATURES = [(b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"GIF87a", ".gif"), (b"GIF89a", ".gif"),
               (b"BM", ".bmp")]


@dataclass
class DownloadResult:
    saved: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: bool = False   # 폴더에 사진이 이미 있어 받지 않음


def sniff_extension(data: bytes) -> str | None:
    return next((ext for sig, ext in _SIGNATURES if data.startswith(sig)), None)


def download_images(root: Path, code: str, urls: list[str], session: requests.Session | None = None,
                    timeout: float = 20.0) -> DownloadResult:
    result = DownloadResult()
    folder = product_folder(root, code) / ORIGINAL_DIR
    if list_images(original_folder(root, code), SOURCE_EXTENSIONS):   # 사진이 이미 있으면 받지 않음
        result.skipped = True
        return result
    folder.mkdir(parents=True, exist_ok=True)
    http = session or requests.Session()
    for i, url in enumerate(urls, start=1):
        try:
            resp = http.get(url, timeout=timeout, stream=True, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            buffer = bytearray()
            for chunk in resp.iter_content(64 * 1024):
                buffer += chunk
                if len(buffer) > MAX_DOWNLOAD_BYTES:
                    raise ValueError("파일이 20MB를 넘습니다")
            data = bytes(buffer)
        except (requests.RequestException, ValueError) as e:
            result.errors.append(f"이미지 {i} 다운로드 실패: {url} ({e})")
            continue
        ext = sniff_extension(data)
        if ext is None:
            result.errors.append(f"이미지 {i}가 JPG·PNG·GIF·BMP 파일이 아닙니다: {url}")
            continue
        path = folder / f"{i:02}{ext}"
        path.write_bytes(data)
        result.saved.append(path)
        log.info("[%s] 이미지 다운로드: %s → %s", code, url, path.name)
    return result
