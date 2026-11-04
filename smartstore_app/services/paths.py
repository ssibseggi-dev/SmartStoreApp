"""앱에 포함된 리소스 경로 (화면·서비스 공용, Qt 없이 사용)."""
from __future__ import annotations

import sys
from pathlib import Path


def resource_path(name: str) -> Path:
    """resources 폴더 경로. PyInstaller로 묶었을 때는 압축 해제 폴더 기준."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "resources" / name
