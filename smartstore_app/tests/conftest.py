import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _isolate_from_real_world(tmp_path, monkeypatch):
    """모든 테스트를 실제 환경과 분리한다.

    - 네트워크 연결 차단: 실수로 실제 커머스API를 부르면 즉시 실패
    - keyring을 메모리 대역으로 교체: 실제 ID·시크릿을 읽지 않음
    - 데이터 폴더를 임시 폴더로: 실제 DB·로그를 건드리지 않음
    """
    def blocked(self, address):
        raise RuntimeError(f"테스트 중 네트워크 연결 차단: {address}")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: blocked(None, a[0] if a else None))

    secrets: dict[tuple[str, str], str] = {}
    import keyring
    monkeypatch.setattr(keyring, "get_password", lambda s, k: secrets.get((s, k)))
    monkeypatch.setattr(keyring, "set_password", lambda s, k, v: secrets.__setitem__((s, k), v))
    monkeypatch.setattr(keyring, "delete_password", lambda s, k: secrets.pop((s, k), None))

    monkeypatch.setenv("SMARTSTORE_DATA_DIR", str(tmp_path / "_appdata"))
