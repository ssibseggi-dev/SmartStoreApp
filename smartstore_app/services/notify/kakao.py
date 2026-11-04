"""카카오톡 '나에게 보내기' — 로그인 연결(인가코드 → 토큰), 토큰 갱신, 메시지 전송.

API (기존 금시세 알림 gold_kakao.py와 동일)
  인가:  https://kauth.kakao.com/oauth/authorize?client_id=..&redirect_uri=..&response_type=code&scope=talk_message
  토큰:  POST https://kauth.kakao.com/oauth/token  (authorization_code / refresh_token)
  전송:  POST https://kapi.kakao.com/v2/api/talk/memo/default/send  (template_object: text 템플릿)

REST API 키·Client Secret·access/refresh 토큰은 keyring(Windows 자격 증명 관리자)에만 저장한다.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, urlencode, urlparse

import keyring
import requests

log = logging.getLogger(__name__)

SERVICE = "smartstore_app"
_KEYS = {"rest_key": "kakao_rest_key", "client_secret": "kakao_client_secret",
         "access_token": "kakao_access_token", "refresh_token": "kakao_refresh_token"}

AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
DEFAULT_REDIRECT_URI = "https://localhost:3000"
TEXT_LIMIT = 200   # 카카오 텍스트 템플릿 최대 글자 수


class KakaoError(Exception):
    pass


class KakaoReconnectRequired(KakaoError):
    """refresh_token 만료 등으로 카카오 로그인을 다시 연결해야 함."""


def _get(name: str) -> str:
    return keyring.get_password(SERVICE, _KEYS[name]) or ""


def _set(name: str, value: str) -> None:
    keyring.set_password(SERVICE, _KEYS[name], value)


def save_app_keys(rest_key: str, client_secret: str) -> None:
    """빈 값은 기존 값을 유지한다."""
    if rest_key.strip():
        _set("rest_key", rest_key.strip())
    if client_secret.strip():
        _set("client_secret", client_secret.strip())


def has_app_keys() -> bool:
    return bool(_get("rest_key"))


def is_connected() -> bool:
    return bool(_get("rest_key") and _get("refresh_token"))


def disconnect() -> None:
    for name in ("access_token", "refresh_token"):
        try:
            keyring.delete_password(SERVICE, _KEYS[name])
        except keyring.errors.PasswordDeleteError:
            pass


def authorize_url(redirect_uri: str = DEFAULT_REDIRECT_URI) -> str:
    rest_key = _get("rest_key")
    if not rest_key:
        raise KakaoError("카카오 REST API 키를 먼저 저장하세요.")
    query = urlencode({"client_id": rest_key, "redirect_uri": redirect_uri, "response_type": "code",
                       "scope": "talk_message"})
    return f"{AUTH_URL}?{query}"


def extract_code(text: str) -> str:
    """이동된 주소 전체 또는 code 값만 붙여넣어도 인가코드를 뽑는다."""
    text = text.strip()
    if "code=" in text:
        query = urlparse(text).query or text.split("?", 1)[-1]
        values = parse_qs(query).get("code")
        if values:
            return values[0]
    if "error=" in text:
        raise KakaoError(f"카카오 로그인이 취소되었거나 실패했습니다: {text}")
    if not text or " " in text or "/" in text:
        raise KakaoError("인가코드를 찾을 수 없습니다. 이동된 주소 전체를 붙여넣으세요.")
    return text


def _token_request(data: dict, session: requests.Session | None) -> dict:
    data = dict(data, client_id=_get("rest_key"))
    secret = _get("client_secret")
    if secret:
        data["client_secret"] = secret
    http = session or requests
    try:
        resp = http.post(TOKEN_URL, data=data, timeout=10)
    except requests.RequestException as e:
        raise KakaoError(f"카카오 서버에 연결하지 못했습니다: {e}") from e
    if resp.status_code != 200:
        body = resp.text[:300]
        if data["grant_type"] == "refresh_token" and ("invalid_grant" in body or resp.status_code in (400, 401)):
            raise KakaoReconnectRequired(f"카카오톡 연결이 만료되었습니다. 설정에서 다시 연결하세요. ({body})")
        raise KakaoError(f"카카오 토큰 요청 실패: {body}")
    return resp.json()


def connect(code_or_url: str, redirect_uri: str = DEFAULT_REDIRECT_URI,
            session: requests.Session | None = None) -> None:
    """인가코드로 토큰을 발급받아 keyring에 저장한다."""
    if not has_app_keys():
        raise KakaoError("카카오 REST API 키를 먼저 저장하세요.")
    token = _token_request({"grant_type": "authorization_code", "redirect_uri": redirect_uri,
                            "code": extract_code(code_or_url)}, session)
    if not token.get("refresh_token"):
        raise KakaoError("토큰 응답에 refresh_token이 없습니다.")
    _set("access_token", token["access_token"])
    _set("refresh_token", token["refresh_token"])
    log.info("카카오톡 연결 완료 (access 만료 %s초, refresh 만료 %s초)",
             token.get("expires_in"), token.get("refresh_token_expires_in"))


def refresh_access_token(session: requests.Session | None = None) -> str:
    """access_token을 갱신한다. 새 refresh_token이 오면 함께 저장한다."""
    refresh = _get("refresh_token")
    if not _get("rest_key") or not refresh:
        raise KakaoReconnectRequired("카카오톡이 연결되어 있지 않습니다. 설정에서 카카오 로그인을 연결하세요.")
    token = _token_request({"grant_type": "refresh_token", "refresh_token": refresh}, session)
    _set("access_token", token["access_token"])
    if token.get("refresh_token"):
        _set("refresh_token", token["refresh_token"])
    return token["access_token"]


def fit_text(text: str, limit: int = TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def send_to_me(text: str, link_url: str, button_title: str = "검수하러 가기",
               session: requests.Session | None = None) -> None:
    access = refresh_access_token(session)
    template = {"object_type": "text", "text": fit_text(text),
                "link": {"web_url": link_url, "mobile_web_url": link_url}, "button_title": button_title}
    http = session or requests
    try:
        resp = http.post(SEND_URL, headers={"Authorization": f"Bearer {access}"},
                         data={"template_object": json.dumps(template, ensure_ascii=False)}, timeout=10)
    except requests.RequestException as e:
        raise KakaoError(f"카카오 서버에 연결하지 못했습니다: {e}") from e
    if resp.status_code != 200:
        raise KakaoError(f"카카오톡 전송 실패: {resp.text[:300]}")
    log.info("카카오톡 알림 전송 (%d자)", len(template["text"]))
