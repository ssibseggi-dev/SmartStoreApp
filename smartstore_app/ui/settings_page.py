"""설정 화면 — 애플리케이션 ID/시크릿 저장(keyring), 연결 테스트."""
from __future__ import annotations

import requests
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from services.connection import AppServices, ConnectionResult
from storage import credentials
from storage.credentials import Credentials
from storage.db import app_data_dir
from ui.messages import describe_error
from ui.workers import run_async
from ui.theme import set_role

_SECRET_PLACEHOLDER = "저장된 시크릿 사용 (변경할 때만 입력)"


class SettingsPage(QWidget):
    credentials_changed = Signal()
    categories_updated = Signal()

    def __init__(self, services: AppServices, parent=None):
        super().__init__(parent)
        self.services = services

        self.id_edit = QLineEdit()
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_secret = QCheckBox("시크릿 표시")
        self.show_secret.toggled.connect(
            lambda on: self.secret_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))

        form = QFormLayout()
        form.addRow("애플리케이션 ID", self.id_edit)
        form.addRow("애플리케이션 시크릿", self.secret_edit)
        form.addRow("", self.show_secret)

        self.save_btn = QPushButton("저장")
        self.test_btn = set_role(QPushButton("연결 테스트"), "primary")
        self.clear_btn = set_role(QPushButton("저장된 정보 삭제"), "danger")
        self.save_btn.clicked.connect(self.on_save)
        self.test_btn.clicked.connect(self.on_test)
        self.clear_btn.clicked.connect(self.on_clear)
        btns = QHBoxLayout()
        for b in (self.save_btn, self.test_btn, self.clear_btn):
            btns.addWidget(b)
        btns.addStretch()

        self.status = QLabel()
        self.status.setWordWrap(True)

        cred_box = QGroupBox("커머스API 인증 정보")
        cred_layout = QVBoxLayout(cred_box)
        cred_layout.addLayout(form)
        cred_layout.addLayout(btns)
        cred_layout.addWidget(self.status)
        cred_layout.addWidget(QLabel("ID·시크릿은 Windows 자격 증명 관리자에 암호화되어 저장됩니다."))

        # 호출 IP 등록 도우미
        self.ip_label = QLabel("-")
        self.ip_btn = QPushButton("내 공인 IP 확인")
        self.ip_btn.clicked.connect(self.on_check_ip)
        ip_box = QGroupBox("API 호출 IP")
        ip_layout = QVBoxLayout(ip_box)
        ip_row = QHBoxLayout()
        ip_row.addWidget(self.ip_btn)
        ip_row.addWidget(self.ip_label)
        ip_row.addStretch()
        ip_layout.addLayout(ip_row)
        ip_layout.addWidget(QLabel(
            "커머스API센터 > 애플리케이션 > API 호출 IP에 이 PC의 공인 IP를 등록해야 호출이 가능합니다.\n"
            "(IP 확인은 외부 서비스 api.ipify.org를 사용합니다.)"))

        data_label = QLabel(f"DB·로그 폴더: {app_data_dir()}")
        data_label.setObjectName("hint")
        data_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        data_box = QGroupBox("데이터 위치")
        QVBoxLayout(data_box).addWidget(data_label)

        layout = QVBoxLayout(self)
        layout.addWidget(cred_box)
        layout.addWidget(ip_box)
        layout.addWidget(data_box)
        layout.addStretch()

        self._load()

    def _load(self) -> None:
        creds = credentials.load()
        self._has_saved_secret = creds is not None
        self.id_edit.setText(creds.client_id if creds else "")
        self.secret_edit.clear()
        self.secret_edit.setPlaceholderText(_SECRET_PLACEHOLDER if creds else "")
        self.status.setText("저장된 인증 정보가 있습니다." if creds else "인증 정보를 입력하세요.")

    def _form_credentials(self) -> Credentials | None:
        client_id = self.id_edit.text().strip()
        secret = self.secret_edit.text().strip()
        if not secret and self._has_saved_secret:
            saved = credentials.load()
            secret = saved.client_secret if saved else ""
        if not client_id or not secret:
            QMessageBox.warning(self, "입력 확인", "애플리케이션 ID와 시크릿을 모두 입력하세요.")
            return None
        return Credentials(client_id, secret)

    def _set_busy(self, busy: bool) -> None:
        for b in (self.save_btn, self.test_btn, self.clear_btn):
            b.setEnabled(not busy)

    def on_save(self) -> None:
        creds = self._form_credentials()
        if not creds:
            return
        credentials.save(creds)
        self.services.reset_client()
        self._load()
        self.status.setText("저장했습니다. '연결 테스트'로 확인하세요.")
        self.credentials_changed.emit()

    def on_test(self) -> None:
        creds = self._form_credentials()
        if not creds:
            return
        self._set_busy(True)
        self.status.setText("토큰 발급 및 카테고리 조회 중...")
        run_async(lambda: self.services.test_connection(creds), self._on_test_ok, self._on_test_fail)

    def _on_test_ok(self, result: ConnectionResult) -> None:
        self._set_busy(False)
        self.status.setText(
            f"연결 성공 — 카테고리 {result.category_count:,}개(리프 {result.leaf_count:,}개)를 받아 저장했습니다.")
        self.categories_updated.emit()
        saved = credentials.load()
        current = self._form_credentials()
        if current and current != saved:
            if QMessageBox.question(self, "저장", "연결에 성공했습니다. 이 인증 정보를 저장할까요?") \
                    == QMessageBox.StandardButton.Yes:
                self.on_save()
                self.status.setText("연결 성공 및 저장 완료.")

    def _on_test_fail(self, exc: Exception) -> None:
        self._set_busy(False)
        self.status.setText("연결 실패")
        QMessageBox.critical(self, "연결 실패", describe_error(exc))

    def on_clear(self) -> None:
        if QMessageBox.question(self, "삭제", "저장된 ID·시크릿을 삭제할까요?") != QMessageBox.StandardButton.Yes:
            return
        credentials.clear()
        self.services.reset_client()
        self._load()
        self.credentials_changed.emit()

    def on_check_ip(self) -> None:
        self.ip_btn.setEnabled(False)
        self.ip_label.setText("확인 중...")

        def fetch() -> str:
            resp = requests.get("https://api.ipify.org", timeout=10)
            resp.raise_for_status()
            return resp.text.strip()

        def done(ip: str) -> None:
            self.ip_btn.setEnabled(True)
            self.ip_label.setText(ip)
            self.ip_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        def fail(exc: Exception) -> None:
            self.ip_btn.setEnabled(True)
            self.ip_label.setText(f"확인 실패: {exc}")

        run_async(fetch, done, fail)
