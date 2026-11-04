"""자격 증명 → API 클라이언트 구성, 연결 테스트, 카테고리 캐시 갱신."""
from __future__ import annotations

from dataclasses import dataclass

from api.auth import TokenProvider
from api.categories import CategoryApi
from api.client import CommerceClient
from storage import credentials
from storage.credentials import Credentials
from storage.db import Database


class NotConfiguredError(Exception):
    pass


@dataclass
class ConnectionResult:
    category_count: int
    leaf_count: int


def build_client(creds: Credentials) -> CommerceClient:
    return CommerceClient(TokenProvider(creds.client_id, creds.client_secret))


class AppServices:
    """UI가 사용하는 서비스 진입점. API 클라이언트는 자격 증명이 바뀌면 다시 만든다."""

    def __init__(self, db: Database):
        self.db = db
        self._client: CommerceClient | None = None

    def client(self) -> CommerceClient:
        if self._client is None:
            creds = credentials.load()
            if creds is None:
                raise NotConfiguredError("설정 화면에서 애플리케이션 ID와 시크릿을 먼저 저장하세요.")
            self._client = build_client(creds)
        return self._client

    def reset_client(self) -> None:
        self._client = None

    def test_connection(self, creds: Credentials) -> ConnectionResult:
        """입력한 자격 증명으로 토큰 발급 + 카테고리 조회를 해본다. 성공 시 카테고리 캐시도 갱신."""
        categories = CategoryApi(build_client(creds)).list_all()
        self.db.replace_categories(categories)
        return ConnectionResult(len(categories), sum(1 for c in categories if c.last))

    def refresh_categories(self) -> int:
        categories = CategoryApi(self.client()).list_all()
        self.db.replace_categories(categories)
        return len(categories)
