import base64

import bcrypt
import pytest

from api.auth import TokenProvider, make_client_secret_sign
from api.categories import CategoryApi
from api.client import CommerceClient
from api.errors import ApiError, AuthError
from storage.db import Database

SECRET = bcrypt.gensalt(rounds=4).decode()


class FakeResp:
    def __init__(self, status=200, json_body=None, headers=None):
        self.status_code = status
        self._json = json_body
        self.headers = headers or {}
        self.content = b"x" if json_body is not None else b""
        self.text = str(json_body)
        self.reason = ""

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, data))
        return self.responses.pop(0)

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.calls.append((method, url, headers))
        return self.responses.pop(0)


def test_sign_is_base64_bcrypt_of_id_and_timestamp():
    sign = make_client_secret_sign("myid", SECRET, 1700000000000)
    hashed = base64.standard_b64decode(sign)
    assert hashed.startswith(SECRET.encode()[:29])
    assert bcrypt.checkpw(b"myid_1700000000000", hashed)


def test_sign_rejects_bad_secret():
    with pytest.raises(ValueError):
        make_client_secret_sign("id", "not-a-salt", 1)


def test_token_cached_and_form_fields():
    s = FakeSession([FakeResp(200, {"access_token": "T1", "expires_in": 10800, "token_type": "Bearer"})])
    tp = TokenProvider("id", SECRET, session=s)
    assert tp.get_token() == "T1"
    assert tp.get_token() == "T1"
    assert len(s.calls) == 1
    data = s.calls[0][2]
    assert data["type"] == "SELF" and data["grant_type"] == "client_credentials"
    assert data["client_id"] == "id" and data["timestamp"].isdigit()


def test_token_error_raises_auth_error():
    s = FakeSession([FakeResp(400, {"code": "BadRequest", "message": "잘못된 요청"})])
    with pytest.raises(AuthError) as ei:
        TokenProvider("id", SECRET, session=s).get_token()
    assert ei.value.status == 400 and "잘못된 요청" in str(ei.value)


def make_client(api_responses):
    token_session = FakeSession([FakeResp(200, {"access_token": f"T{i}", "expires_in": 10800})
                                 for i in range(5)])
    api_session = FakeSession(api_responses)
    sleeps = []
    client = CommerceClient(TokenProvider("id", SECRET, session=token_session), session=api_session,
                            max_per_sec=1000, sleep=sleeps.append)
    return client, api_session, token_session, sleeps


def test_retries_on_429_then_succeeds():
    client, api, _, sleeps = make_client([
        FakeResp(429, {"code": "GW.RATE_LIMIT", "message": "limit"}, {"Retry-After": "1"}),
        FakeResp(200, [{"id": "50000000", "name": "패션의류", "wholeCategoryName": "패션의류", "last": False}]),
    ])
    cats = CategoryApi(client).list_all()
    assert cats[0].id == "50000000" and not cats[0].last
    assert sleeps == [1.0]
    assert api.calls[0][2]["Authorization"] == "Bearer T0"


def test_401_refreshes_token_once():
    client, api, tokens, _ = make_client([
        FakeResp(401, {"code": "GW.AUTHN", "message": "expired"}),
        FakeResp(200, []),
    ])
    assert client.get("/external/v1/categories") == []
    assert api.calls[1][2]["Authorization"] == "Bearer T1"
    assert len(tokens.calls) == 2


def test_gives_up_after_max_retries():
    client, _, _, sleeps = make_client([FakeResp(500, {"message": "err"})] * 4)
    with pytest.raises(ApiError) as ei:
        client.get("/x")
    assert ei.value.status == 500 and len(sleeps) == 3


def test_db_category_search(tmp_path):
    from api.categories import Category
    db = Database(tmp_path / "t.db")
    db.replace_categories([
        Category("1", "여성의류", "패션의류>여성의류", False),
        Category("2", "원피스", "패션의류>여성의류>원피스", True),
        Category("3", "티셔츠", "패션의류>남성의류>티셔츠", True),
    ])
    assert [c.id for c in db.search_categories("여성 원피스")] == ["2"]
    assert [c.id for c in db.search_categories("여성", leaf_only=False)] == ["1", "2"]
    assert db.category_count() == 3
    db.set_setting("k", "v")
    assert db.get_setting("k") == "v"
    db.close()


def test_db_category_search_by_id(tmp_path):
    from api.categories import Category
    db = Database(tmp_path / "t.db")
    db.replace_categories([
        Category("50000807", "원피스", "패션의류>여성의류>원피스", True),
        Category("50000803", "티셔츠", "패션의류>여성의류>티셔츠", True),
        Category("50000167", "여성의류", "패션의류>여성의류", False),
        Category("50080700", "기타", "가구>기타", True),
    ])
    assert [c.id for c in db.search_categories("50000807")] == ["50000807"]
    assert [c.id for c in db.search_categories("5008")] == ["50080700"]               # ID 일부
    assert [c.id for c in db.search_categories("0807")] == ["50080700", "50000807"]  # 이름순 (가구 < 패션의류)
    assert [c.id for c in db.search_categories("여성 50000803")] == ["50000803"]     # 이름+ID 함께
    assert db.search_categories("50000167") == []                                    # 최하위만 검색
    assert [c.id for c in db.search_categories("50000167", leaf_only=False)] == ["50000167"]
    assert len(db.search_categories("")) == 3
    db.close()


def test_tests_cannot_reach_real_world():
    """conftest 안전장치: 테스트에서는 네트워크·실제 인증 정보·실제 데이터 폴더에 닿지 않는다."""
    import os
    import requests
    from storage import credentials
    from storage.db import app_data_dir
    with pytest.raises(Exception):
        requests.get("https://api.commerce.naver.com", timeout=3)
    assert credentials.load() is None
    assert "_appdata" in str(app_data_dir()) and "SmartStoreApp" not in os.environ["SMARTSTORE_DATA_DIR"]
