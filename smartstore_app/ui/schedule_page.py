"""예약 · 알림 — Windows 작업 스케줄러 예약 실행과 카카오톡 '나에게 보내기' 알림 설정."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTime, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPushButton, QTimeEdit, QVBoxLayout, QWidget,
)

from services import scheduler
from services.automation.config import load_settings, save_settings
from services.connection import AppServices
from services.notify import kakao
from ui.theme import set_role
from ui.workers import run_async


class SchedulePage(QWidget):
    def __init__(self, services: AppServices, parent=None):
        super().__init__(parent)
        self.services = services
        self.db = services.db

        # --- 예약 실행 ---
        self.times = QListWidget()
        self.times.setMaximumHeight(84)
        self.time_edit = QTimeEdit(QTime(10, 0))
        self.time_edit.setDisplayFormat("HH:mm")
        add_btn = QPushButton("시각 추가")
        remove_btn = QPushButton("선택 시각 삭제")
        add_btn.clicked.connect(self.on_add_time)
        remove_btn.clicked.connect(self.on_remove_time)
        time_row = QHBoxLayout()
        time_row.addWidget(self.time_edit)
        time_row.addWidget(add_btn)
        time_row.addWidget(remove_btn)
        time_row.addStretch()

        self.task_status = QLabel("예약 상태 확인 중...")
        self.task_status.setWordWrap(True)
        self.install_btn = set_role(QPushButton("예약 등록·갱신"), "primary")
        self.uninstall_btn = set_role(QPushButton("예약 해제"), "danger")
        self.run_now_btn = QPushButton("지금 한 번 실행")
        export_btn = QPushButton("등록 스크립트 저장")
        self.install_btn.clicked.connect(self.on_install)
        self.uninstall_btn.clicked.connect(self.on_uninstall)
        self.run_now_btn.clicked.connect(self.on_run_now)
        export_btn.clicked.connect(self.on_export)
        task_buttons = QHBoxLayout()
        for b in (self.install_btn, self.uninstall_btn, self.run_now_btn, export_btn):
            task_buttons.addWidget(b)
        task_buttons.addStretch()
        schedule_hint = QLabel("정한 시각마다 작업 엑셀의 '대기' 상품을 자동으로 등록합니다(판매중지 → 검수). "
                               "PC가 켜져 있고 로그인한 상태에서 실행되며, 꺼져 있어 놓친 실행은 켜진 뒤 한 번 실행합니다.")
        schedule_hint.setObjectName("hint")
        schedule_hint.setWordWrap(True)

        schedule = QGroupBox("예약 실행 (Windows 작업 스케줄러)")
        v = QVBoxLayout(schedule)
        v.addWidget(QLabel("매일 실행 시각"))
        v.addWidget(self.times)
        v.addLayout(time_row)
        v.addWidget(self.task_status)
        v.addLayout(task_buttons)
        v.addWidget(schedule_hint)

        # --- 카카오톡 알림 ---
        self.rest_key_edit = QLineEdit()
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.redirect_edit = QLineEdit()
        self.link_edit = QLineEdit()
        save_keys_btn = QPushButton("키 저장")
        save_keys_btn.clicked.connect(self.on_save_keys)
        self.kakao_status = QLabel()
        self.connect_btn = set_role(QPushButton("카카오 로그인 연결"), "primary")
        self.test_btn = QPushButton("테스트 메시지 보내기")
        self.disconnect_btn = set_role(QPushButton("연결 해제"), "danger")
        self.connect_btn.clicked.connect(self.on_connect)
        self.test_btn.clicked.connect(self.on_test)
        self.disconnect_btn.clicked.connect(self.on_disconnect)
        self.enabled_check = QCheckBox("예약 실행 결과를 카카오톡으로 받기")
        self.app_check = QCheckBox("앱에서 [대기 상품 처리]를 눌렀을 때도 받기")
        self.enabled_check.toggled.connect(self._save_flags)
        self.app_check.toggled.connect(self._save_flags)

        kakao_buttons = QHBoxLayout()
        for b in (self.connect_btn, self.test_btn, self.disconnect_btn):
            kakao_buttons.addWidget(b)
        kakao_buttons.addStretch()
        key_row = QHBoxLayout()
        key_row.addWidget(self.rest_key_edit, 1)
        key_row.addWidget(save_keys_btn)
        kakao_hint = QLabel(
            "카카오 개발자 콘솔(developers.kakao.com) 준비: ① 카카오 로그인 켜기 ② Redirect URI에 위 주소 등록 "
            "③ 동의항목에서 '카카오톡 메시지 전송' 설정 ④ 플랫폼 > Web 사이트 도메인에 버튼 링크 주소의 도메인 등록 "
            "(등록하지 않으면 알림의 버튼이 열리지 않습니다). 금시세 알림에 쓰던 카카오 앱을 그대로 써도 됩니다. "
            "키와 토큰은 Windows 자격 증명 관리자에만 저장됩니다.")
        kakao_hint.setObjectName("hint")
        kakao_hint.setWordWrap(True)

        alert = QGroupBox("카카오톡 알림 (나에게 보내기)")
        f = QFormLayout(alert)
        f.addRow("REST API 키", key_row)
        f.addRow("Client Secret", self.secret_edit)
        f.addRow("Redirect URI", self.redirect_edit)
        f.addRow("버튼 링크", self.link_edit)
        f.addRow("연결 상태", self.kakao_status)
        f.addRow(kakao_buttons)
        f.addRow(self.enabled_check)
        f.addRow(self.app_check)
        f.addRow(kakao_hint)

        layout = QVBoxLayout(self)
        layout.addWidget(schedule)
        layout.addWidget(alert)
        layout.addStretch()
        self.load()

    # --- 불러오기 ---
    def load(self) -> None:
        s = load_settings(self.db)
        self.times.clear()
        self.times.addItems(s.schedule_times)
        self.redirect_edit.setText(s.kakao_redirect_uri)
        self.link_edit.setText(s.kakao_link_url)
        for box, value in ((self.enabled_check, s.kakao_enabled), (self.app_check, s.kakao_notify_app)):
            box.blockSignals(True)
            box.setChecked(value)
            box.blockSignals(False)
        self.rest_key_edit.clear()
        self.rest_key_edit.setPlaceholderText("저장됨 (바꿀 때만 입력)" if kakao.has_app_keys() else "카카오 앱의 REST API 키")
        self.secret_edit.setPlaceholderText("저장됨 또는 사용 안 함 (바꿀 때만 입력)" if kakao.has_app_keys() else
                                            "카카오 앱에서 Client Secret을 켠 경우 입력")
        self._update_kakao_status()
        self.refresh_task_status()

    def _update_kakao_status(self) -> None:
        if kakao.is_connected():
            self.kakao_status.setText("<span style='color:#008040'>●</span> 연결됨")
        elif kakao.has_app_keys():
            self.kakao_status.setText("<span style='color:#B54708'>●</span> 키 저장됨 · 카카오 로그인 연결 필요")
        else:
            self.kakao_status.setText("<span style='color:#667085'>●</span> 연결 안 됨")
        self.test_btn.setEnabled(kakao.is_connected())
        self.disconnect_btn.setEnabled(kakao.is_connected())
        self.connect_btn.setEnabled(kakao.has_app_keys())

    def _current_times(self) -> list[str]:
        return [self.times.item(i).text() for i in range(self.times.count())]

    def _save(self, **changes) -> None:
        s = load_settings(self.db)
        for key, value in changes.items():
            setattr(s, key, value)
        save_settings(self.db, s)

    # --- 예약 실행 ---
    def on_add_time(self) -> None:
        times = scheduler.normalize_times(self._current_times() + [self.time_edit.time().toString("HH:mm")])
        self.times.clear()
        self.times.addItems(times)
        self._save(schedule_times=times)

    def on_remove_time(self) -> None:
        for item in self.times.selectedItems():
            self.times.takeItem(self.times.row(item))
        self._save(schedule_times=self._current_times())

    def refresh_task_status(self) -> None:
        run_async(scheduler.status, lambda st: self.task_status.setText(st.describe()),
                  lambda e: self.task_status.setText(f"예약 상태를 확인하지 못했습니다: {e}"))

    def _task_action(self, title: str, action, done_message: str) -> None:
        for b in (self.install_btn, self.uninstall_btn, self.run_now_btn):
            b.setEnabled(False)

        def finish(_):
            for b in (self.install_btn, self.uninstall_btn, self.run_now_btn):
                b.setEnabled(True)
            self.refresh_task_status()
            QMessageBox.information(self, title, done_message)

        def fail(e):
            for b in (self.install_btn, self.uninstall_btn, self.run_now_btn):
                b.setEnabled(True)
            QMessageBox.critical(self, title, str(e))

        run_async(action, finish, fail)

    def on_install(self) -> None:
        try:
            times = scheduler.normalize_times(self._current_times())
        except scheduler.SchedulerError as e:
            QMessageBox.warning(self, "예약 등록", str(e))
            return
        if QMessageBox.question(self, "예약 등록", f"매일 {', '.join(times)}에 대기 상품 자동 등록을 실행하도록 "
                                f"Windows 작업 스케줄러에 등록할까요?") != QMessageBox.StandardButton.Yes:
            return
        self._save(schedule_times=times)
        self._task_action("예약 등록", lambda: scheduler.register(times), f"예약을 등록했습니다: 매일 {', '.join(times)}")

    def on_uninstall(self) -> None:
        if QMessageBox.question(self, "예약 해제", "예약 실행을 해제할까요?") == QMessageBox.StandardButton.Yes:
            self._task_action("예약 해제", scheduler.unregister, "예약 실행을 해제했습니다.")

    def on_run_now(self) -> None:
        if QMessageBox.question(self, "지금 실행", "예약 작업을 지금 한 번 실행할까요? (대기 상품이 실제로 등록됩니다)") \
                == QMessageBox.StandardButton.Yes:
            self._task_action("지금 실행", scheduler.run_now,
                              "실행을 시작했습니다. 결과는 작업 엑셀과 카카오톡 알림으로 확인하세요.")

    def on_export(self) -> None:
        default = str(load_settings(self.db).work_excel_path().with_name("예약실행_등록.ps1"))
        path, _ = QFileDialog.getSaveFileName(self, "등록 스크립트 저장", default, "PowerShell 스크립트 (*.ps1)")
        if path:
            try:
                scheduler.export_script(Path(path), scheduler.normalize_times(self._current_times()))
            except (OSError, scheduler.SchedulerError) as e:
                QMessageBox.critical(self, "스크립트 저장", str(e))
                return
            QMessageBox.information(self, "스크립트 저장", f"저장했습니다: {path}\n마우스 오른쪽 클릭 → 'PowerShell에서 실행'으로 등록합니다.")

    # --- 카카오톡 ---
    def on_save_keys(self) -> None:
        rest_key = self.rest_key_edit.text().strip()
        if not rest_key and not kakao.has_app_keys():
            QMessageBox.warning(self, "카카오톡 알림", "REST API 키를 입력하세요.")
            return
        kakao.save_app_keys(rest_key, self.secret_edit.text())
        self._save(kakao_redirect_uri=self.redirect_edit.text().strip() or kakao.DEFAULT_REDIRECT_URI,
                   kakao_link_url=self.link_edit.text().strip() or "https://sell.smartstore.naver.com/")
        self.secret_edit.clear()
        self.load()

    def on_connect(self) -> None:
        self.on_save_keys()
        redirect = load_settings(self.db).kakao_redirect_uri
        try:
            url = kakao.authorize_url(redirect)
        except kakao.KakaoError as e:
            QMessageBox.warning(self, "카카오 로그인", str(e))
            return
        QDesktopServices.openUrl(QUrl(url))
        pasted, ok = QInputDialog.getText(
            self, "카카오 로그인 연결",
            "브라우저에서 카카오 로그인 후 동의하면 주소가 바뀝니다(페이지가 안 열려도 괜찮습니다).\n"
            "바뀐 주소창의 주소 전체를 복사해 붙여넣으세요.")
        if not ok or not pasted.strip():
            return
        self._set_kakao_busy(True)   # 처리 중 다시 눌러도 중복 요청하지 않도록
        run_async(lambda: kakao.connect(pasted, redirect), lambda _: self._after_connect(),
                  lambda e: self._kakao_failed("카카오 로그인 연결", e))

    def _set_kakao_busy(self, busy: bool) -> None:
        if busy:
            for b in (self.connect_btn, self.test_btn, self.disconnect_btn):
                b.setEnabled(False)
        else:
            self._update_kakao_status()

    def _kakao_failed(self, title: str, exc: Exception) -> None:
        self._set_kakao_busy(False)
        QMessageBox.critical(self, title, str(exc))

    def _after_connect(self) -> None:
        self._set_kakao_busy(False)
        QMessageBox.information(self, "카카오 로그인 연결", "연결했습니다. '테스트 메시지 보내기'로 확인하세요.")

    def on_test(self) -> None:
        link = load_settings(self.db).kakao_link_url
        self._set_kakao_busy(True)   # 전송이 끝날 때까지 버튼을 막아 메시지가 두 번 가지 않게 한다
        run_async(lambda: kakao.send_to_me("[스마트스토어] 알림 테스트입니다.\n자동 등록 결과가 이렇게 전송됩니다.", link),
                  lambda _: self._after_test(), lambda e: self._kakao_failed("카카오톡 알림", e))

    def _after_test(self) -> None:
        self._set_kakao_busy(False)
        QMessageBox.information(self, "카카오톡 알림", "테스트 메시지를 보냈습니다. 카카오톡을 확인하세요.")

    def on_disconnect(self) -> None:
        if QMessageBox.question(self, "연결 해제", "카카오톡 연결(토큰)을 지울까요? 키는 남겨 둡니다.") \
                == QMessageBox.StandardButton.Yes:
            kakao.disconnect()
            self._update_kakao_status()

    def _save_flags(self) -> None:
        if self.enabled_check.isChecked() and not kakao.is_connected():
            QMessageBox.information(self, "카카오톡 알림", "카카오 로그인을 연결해야 알림이 전송됩니다.")
        self._save(kakao_enabled=self.enabled_check.isChecked(), kakao_notify_app=self.app_check.isChecked())
