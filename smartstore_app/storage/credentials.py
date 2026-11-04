"""애플리케이션 ID/시크릿을 Windows 자격 증명 관리자(keyring)에 보관한다. 파일에는 저장하지 않는다."""
from __future__ import annotations

from dataclasses import dataclass

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "smartstore_app"
_KEY_ID = "client_id"
_KEY_SECRET = "client_secret"


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str


def load() -> Credentials | None:
    client_id = keyring.get_password(SERVICE, _KEY_ID)
    client_secret = keyring.get_password(SERVICE, _KEY_SECRET)
    if not client_id or not client_secret:
        return None
    return Credentials(client_id, client_secret)


def save(creds: Credentials) -> None:
    keyring.set_password(SERVICE, _KEY_ID, creds.client_id.strip())
    keyring.set_password(SERVICE, _KEY_SECRET, creds.client_secret.strip())


def clear() -> None:
    for key in (_KEY_ID, _KEY_SECRET):
        try:
            keyring.delete_password(SERVICE, key)
        except PasswordDeleteError:
            pass
