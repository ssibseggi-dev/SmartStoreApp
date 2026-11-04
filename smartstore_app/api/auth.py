"""커머스API 인증 토큰 발급·캐시.

POST {BASE_URL}/external/v1/oauth2/token (application/x-www-form-urlencoded)
  client_id, timestamp(ms), client_secret_sign, grant_type=client_credentials, type=SELF
client_secret_sign = base64(bcrypt(f"{client_id}_{timestamp}", salt=client_secret))
"""
from __future__ import annotations

import base64
import threading
import time

import bcrypt
import requests

from .errors import AuthError, NetworkError

BASE_URL = "https://api.commerce.naver.com"
TOKEN_PATH = "/external/v1/oauth2/token"

# 만료 직전 토큰 사용을 피하기 위한 여유 시간(초)
_EXPIRY_MARGIN_SEC = 300
# 로컬 시계가 서버보다 약간 빠를 때 '미래 timestamp' 오류를 피하기 위한 보정(ms)
_TIMESTAMP_SKEW_MS = 3000


def make_client_secret_sign(client_id: str, client_secret: str, timestamp_ms: int) -> str:
    password = f"{client_id}_{timestamp_ms}".encode("utf-8")
    try:
        hashed = bcrypt.hashpw(password, client_secret.encode("utf-8"))
    except ValueError as e:
        # 시크릿이 bcrypt salt 형식($2a$..)이 아니면 여기서 실패
        raise ValueError("애플리케이션 시크릿 형식이 올바르지 않습니다. 복사한 값을 다시 확인하세요.") from e
    return base64.standard_b64encode(hashed).decode("utf-8")


class TokenProvider:
    """토큰을 발급받아 만료 전까지 재사용한다. 여러 스레드에서 안전하게 호출 가능."""

    def __init__(self, client_id: str, client_secret: str,
                 session: requests.Session | None = None, base_url: str = BASE_URL,
                 timeout: float = 10.0):
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._session = session or requests.Session()
        self._base_url = base_url
        self._timeout = timeout
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if force_refresh or not self._token or time.time() >= self._expires_at - _EXPIRY_MARGIN_SEC:
                self._issue()
            return self._token  # type: ignore[return-value]

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _issue(self) -> None:
        timestamp = int(time.time() * 1000) - _TIMESTAMP_SKEW_MS
        data = {
            "client_id": self._client_id,
            "timestamp": str(timestamp),
            "client_secret_sign": make_client_secret_sign(self._client_id, self._client_secret, timestamp),
            "grant_type": "client_credentials",
            "type": "SELF",
        }
        try:
            resp = self._session.post(self._base_url + TOKEN_PATH, data=data, timeout=self._timeout)
        except requests.RequestException as e:
            raise NetworkError(f"토큰 발급 서버에 연결하지 못했습니다: {e}") from e

        if resp.status_code != 200:
            raise AuthError.from_response(resp)

        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise AuthError(resp.status_code, None, "응답에 access_token이 없습니다.")
        self._token = token
        self._expires_at = time.time() + int(body.get("expires_in", 0))
