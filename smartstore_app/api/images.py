"""상품 이미지 업로드 API.

POST /external/v1/product-images/upload  (multipart/form-data, 필드명 imageFiles, 최대 10개)
응답: {"images": [{"url": "https://shop-phinf.pstatic.net/..."}]}
지원 형식: JPEG, GIF, PNG, BMP
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from .client import CommerceClient

MAX_FILES_PER_CALL = 10
SUPPORTED_MIME = {"image/jpeg", "image/gif", "image/png", "image/bmp"}


def guess_mime(path: str | Path) -> str | None:
    mime, _ = mimetypes.guess_type(str(path))
    return mime


class ImageApi:
    def __init__(self, client: CommerceClient):
        self._client = client

    def upload(self, paths: list[str | Path]) -> list[str]:
        """이미지 파일들을 업로드하고 입력 순서대로 URL을 돌려준다."""
        urls: list[str] = []
        for i in range(0, len(paths), MAX_FILES_PER_CALL):
            chunk = paths[i:i + MAX_FILES_PER_CALL]
            # 재시도 시 다시 보낼 수 있도록 파일 객체가 아닌 bytes로 전달
            files = [("imageFiles", (Path(p).name, Path(p).read_bytes(), guess_mime(p) or "application/octet-stream"))
                     for p in chunk]
            data = self._client.post("/external/v1/product-images/upload", files=files) or {}
            urls.extend(img["url"] for img in data.get("images", []))
        if len(urls) != len(paths):
            raise ValueError(f"이미지 {len(paths)}개 중 {len(urls)}개만 업로드되었습니다.")
        return urls
