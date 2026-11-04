"""커머스API 공통 HTTP 클라이언트 — 인증 헤더, 호출 제한, 재시도."""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

import requests

from .auth import BASE_URL, TokenProvider
from .errors import ApiError, NetworkError

log = logging.getLogger(__name__)

# 재시도 대상 상태 코드 (호출 한도 초과, 일시적 서버 오류)
_RETRY_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """초당 최대 호출 수를 넘지 않도록 호출 간격을 맞춘다."""

    def __init__(self, max_per_sec: float):
        self._interval = 1.0 / max_per_sec if max_per_sec > 0 else 0.0
        self._next_at = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            self._next_at = max(now, self._next_at) + self._interval
        if delay > 0:
            time.sleep(delay)


class CommerceClient:
    def __init__(self, token_provider: TokenProvider, session: requests.Session | None = None,
                 base_url: str = BASE_URL, max_per_sec: float = 2.0, max_retries: int = 3,
                 timeout: float = 30.0, sleep=time.sleep):
        self._tokens = token_provider
        self._session = session or requests.Session()
        self._base_url = base_url
        self._limiter = RateLimiter(max_per_sec)
        self._max_retries = max_retries
        self._timeout = timeout
        self._sleep = sleep

    def get(self, path: str, params: dict | None = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, json: Any = None, **kwargs) -> Any:
        return self.request("POST", path, json=json, **kwargs)

    def put(self, path: str, json: Any = None) -> Any:
        return self.request("PUT", path, json=json)

    def patch(self, path: str, json: Any = None) -> Any:
        return self.request("PATCH", path, json=json)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    def request(self, method: str, path: str, **kwargs) -> Any:
        url = self._base_url + path
        token_refreshed = False
        attempt = 0
        while True:
            self._limiter.wait()
            headers = {"Authorization": f"Bearer {self._tokens.get_token()}"}
            headers.update(kwargs.pop("headers", {}) or {})
            try:
                resp = self._session.request(method, url, headers=headers, timeout=self._timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < self._max_retries:
                    attempt += 1
                    self._sleep(self._backoff(attempt))
                    continue
                raise NetworkError(f"API 서버에 연결하지 못했습니다: {e}") from e

            if resp.status_code == 401 and not token_refreshed:
                # 토큰 만료·폐기 — 한 번만 재발급 후 재시도
                token_refreshed = True
                self._tokens.invalidate()
                continue

            if resp.status_code in _RETRY_STATUS and attempt < self._max_retries:
                attempt += 1
                wait = self._retry_after(resp) or self._backoff(attempt)
                log.warning("%s %s -> %s, %.1f초 후 재시도 (%d/%d)",
                            method, path, resp.status_code, wait, attempt, self._max_retries)
                self._sleep(wait)
                continue

            if resp.status_code >= 400:
                raise ApiError.from_response(resp)

            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2 ** (attempt - 1), 8) + random.uniform(0, 0.5)

    @staticmethod
    def _retry_after(resp) -> float | None:
        value = resp.headers.get("Retry-After")
        try:
            return float(value) if value else None
        except ValueError:
            return None
