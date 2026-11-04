"""6단계: 카카오톡 알림(나에게 보내기)과 Windows 작업 스케줄러 예약 — 외부 호출은 모두 대역."""
import json
import subprocess
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

from services import scheduler
from services.automation import config, runner
from services.notify import kakao
from services.notify.dispatch import notify_error, notify_run
from services.notify.summary import run_summary
from tests.test_automation_register import FakeClient, FakeServices, add_rows, env, photos, row, sheet  # noqa: F401


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body or {}, text or json.dumps(body or {})

    def json(self):
        return self._body


class KakaoSession:
    def __init__(self, token_responses=(), send_status=200):
        self.token_responses = list(token_responses)
        self.send_status = send_status
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append((url, data, headers))
        if url == kakao.TOKEN_URL:
            return self.token_responses.pop(0)
        return Resp(self.send_status, {"result_code": 0}, "" if self.send_status == 200 else '{"msg":"fail"}')


def connected(session=None):
    kakao.save_app_keys("REST", "SECRET")
    session = session or KakaoSession([Resp(body={"access_token": "A1", "refresh_token": "R1", "expires_in": 21599})])
    kakao.connect("https://localhost:3000/?code=CODE123", session=session)
    return session


# --- 카카오 ---
def test_authorize_url_and_extract_code():
    with pytest.raises(kakao.KakaoError):
        kakao.authorize_url()
    kakao.save_app_keys("REST", "")
    url = kakao.authorize_url("https://localhost:3000")
    assert "client_id=REST" in url and "scope=talk_message" in url and "redirect_uri=https%3A%2F%2Flocalhost%3A3000" in url
    assert kakao.extract_code("https://localhost:3000/?code=abc-123&state=x") == "abc-123"
    assert kakao.extract_code("  abc123  ") == "abc123"
    with pytest.raises(kakao.KakaoError):
        kakao.extract_code("https://localhost:3000/?error=access_denied")
    with pytest.raises(kakao.KakaoError):
        kakao.extract_code("주소 없음")


def test_connect_stores_tokens_in_keyring_and_sends():
    session = connected()
    url, data, _ = session.calls[0]
    assert data == {"grant_type": "authorization_code", "redirect_uri": "https://localhost:3000", "code": "CODE123",
                    "client_id": "REST", "client_secret": "SECRET"}
    assert kakao.is_connected() and kakao._get("access_token") == "A1"

    session = KakaoSession([Resp(body={"access_token": "A2", "refresh_token": "R2"})])
    kakao.send_to_me("가" * 250, "https://sell.smartstore.naver.com/", session=session)
    assert session.calls[0][1]["refresh_token"] == "R1" and kakao._get("refresh_token") == "R2"   # 새 refresh 저장
    url, data, headers = session.calls[1]
    template = json.loads(data["template_object"])
    assert url == kakao.SEND_URL and headers == {"Authorization": "Bearer A2"}
    assert len(template["text"]) == 200 and template["text"].endswith("…")                        # 200자 제한
    assert template["link"]["web_url"] == "https://sell.smartstore.naver.com/" and template["button_title"]


def test_refresh_keeps_old_refresh_token_when_not_rotated():
    connected()
    kakao.refresh_access_token(KakaoSession([Resp(body={"access_token": "A9"})]))
    assert kakao._get("refresh_token") == "R1" and kakao._get("access_token") == "A9"


def test_expired_refresh_requires_reconnect_and_send_failure():
    connected()
    with pytest.raises(kakao.KakaoReconnectRequired):
        kakao.refresh_access_token(KakaoSession([Resp(400, text='{"error":"invalid_grant"}')]))
    with pytest.raises(kakao.KakaoError, match="전송 실패"):
        kakao.send_to_me("x", "https://a", session=KakaoSession([Resp(body={"access_token": "A"})], send_status=403))
    kakao.disconnect()
    with pytest.raises(kakao.KakaoReconnectRequired):
        kakao.refresh_access_token()


# --- 알림 문구 ---
def fake_report(results, prepared=1, excel_written=True):
    items = {code: SimpleNamespace(status=s, message=m, registered=s.startswith("등록완료")) for code, s, m in results}
    return SimpleNamespace(results=items, prepared=[0] * prepared, excel_written=excel_written, dry_run=False,
                           run_id="R", registered_count=sum(r.registered for r in items.values()))


def test_run_summary_format_and_limit():
    now = datetime(2026, 11, 2, 10, 0)
    report = fake_report([("A001", "등록완료(판매중지)", "ok"), ("A005", "실패", "사진이 없습니다. 'C:\\...'에 사진을 넣거나"),
                          ("A006", "대기", "하루 최대 등록 수")])
    text = run_summary(report, pending_review=12, now=now)
    assert text.splitlines()[:2] == ["[스마트스토어] 11/02 10:00 자동 등록", "등록 1건 / 실패 1건 / 검수 대기 12건"]
    assert "하루 한도로 다음에 등록: 1건" in text and "실패: A005 사진이 없습니다." in text

    many = fake_report([(f"B{i:03}", "실패", "분류 '아주아주긴분류이름'가 자동화 설정의 분류 매핑에 없습니다") for i in range(20)],
                       excel_written=False)
    text = run_summary(many, 0, now)
    assert len(text) <= 200 and "외 17건" in text
    assert "대기' 상품이 없었습니다" in run_summary(fake_report([], prepared=0), 3, now)


def test_notify_run_respects_settings_and_never_raises(env, monkeypatch):
    db, root, excel = env
    sent = []
    monkeypatch.setattr(kakao, "send_to_me", lambda text, link, **kw: sent.append((text, link)))
    report = fake_report([("A001", "등록완료(판매중지)", "ok")])
    assert notify_run(db, report, "schedule") is None and sent == []           # 기본: 꺼짐

    s = config.load_settings(db)
    s.kakao_enabled, s.kakao_notify_app = True, False
    config.save_settings(db, s)
    assert notify_run(db, report, "app") is None                               # 앱 실행 알림 꺼짐
    assert notify_run(db, report, "schedule") == "카카오톡 알림을 보냈습니다." and len(sent) == 1
    assert sent[0][1] == "https://sell.smartstore.naver.com/"
    assert notify_run(db, SimpleNamespace(dry_run=True), "schedule") is None    # dry-run은 보내지 않음

    def expired(*a, **k):
        raise kakao.KakaoReconnectRequired("만료")
    monkeypatch.setattr(kakao, "send_to_me", expired)
    assert "재연결 필요" in notify_run(db, report, "schedule")
    assert "재연결 필요" in notify_error(db, "작업 엑셀이 없습니다")
    logs = [(r["action"], r["result"]) for r in db.list_run_log()]
    assert ("notify", "성공") in logs and ("notify", "실패") in logs


def test_cli_run_notify(env, monkeypatch, capsys):
    import cli
    db, root, excel = env
    s = config.load_settings(db)
    s.kakao_enabled = True
    config.save_settings(db, s)
    sent = []
    monkeypatch.setattr(kakao, "send_to_me", lambda text, link, **kw: sent.append(text))
    photos(root, "A001")
    add_rows(excel, [row("A001"), row("A002", 원가="abc")])
    client = FakeClient()
    monkeypatch.setattr(cli, "AppServices", lambda d: FakeServices(d, client))
    assert cli.main(["run", "--notify"]) == 1
    assert "카카오톡 알림을 보냈습니다." in capsys.readouterr().out
    assert "등록 1건 / 실패 1건 / 검수 대기 1건" in sent[0] and "A002 원가는 숫자로 입력하세요" in sent[0]

    assert cli.main(["run", "--dry-run", "--notify"]) == 0 and len(sent) == 1   # dry-run은 알림 없음
    s.work_excel = str(root / "없는파일.xlsx")
    config.save_settings(db, s)
    assert cli.main(["run", "--notify"]) == 2                                    # 실행 자체가 실패해도 알림
    assert "자동 등록 실패" in sent[-1] and "작업 엑셀이 없습니다" in sent[-1]


# --- 작업 스케줄러 ---
def test_normalize_times():
    assert scheduler.normalize_times(["18:00", "9:30", "10:00", "18:00"]) == ["09:30", "10:00", "18:00"]
    for bad in (["25:00"], ["10-00"], []):
        with pytest.raises(scheduler.SchedulerError):
            scheduler.normalize_times(bad)


def test_register_script_and_command():
    exe, args, workdir = scheduler.run_command()
    assert exe.lower().endswith(("pythonw.exe", "python.exe")) and args.endswith("cli.py\" run --notify")
    script = scheduler.register_script(["10:00", "18:00"], ("C:\\it's\\py.exe", "run --notify", "C:\\app"))
    assert "-At '10:00'" in script and "-At '18:00'" in script
    assert "-StartWhenAvailable" in script and "-MultipleInstances IgnoreNew" in script
    assert "'C:\\it''s\\py.exe'" in script and f"-TaskName '{scheduler.TASK_NAME}'" in script and "-Force" in script


def test_register_status_unregister_with_fake_runner():
    calls = []

    def runner_ok(script):
        calls.append(script)
        return "ok"
    scheduler.register(["10:00"], runner=runner_ok)
    scheduler.unregister(runner=runner_ok)
    scheduler.run_now(runner=runner_ok)
    assert "Register-ScheduledTask" in calls[0] and "Unregister-ScheduledTask" in calls[1] and "Start-ScheduledTask" in calls[2]
    with pytest.raises(scheduler.SchedulerError):
        scheduler.register(["10:00"], runner=lambda s: "")

    assert not scheduler.status(runner=lambda s: "null").registered
    st = scheduler.status(runner=lambda s: json.dumps({"state": "Ready", "next": "2026-11-03 10:00", "last": "",
                                                       "result": 267011, "times": ["10:00", "18:00"]}))
    assert st.registered and "10:00, 18:00" in st.describe() and "아직 실행 안 됨" in st.describe()
    single = scheduler.status(runner=lambda s: json.dumps({"state": "Ready", "result": 0, "last": "2026-11-02 10:00",
                                                         "times": "10:00"}))
    assert single.times == ["10:00"] and "성공" in single.describe()


def test_export_script(tmp_path):
    path = scheduler.export_script(tmp_path / "등록.ps1", ["10:00"])
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                                      # PowerShell 5.1용 BOM
    assert "Register-ScheduledTask" in raw.decode("utf-8-sig") and "Unregister-ScheduledTask" in raw.decode("utf-8-sig")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")
def test_generated_powershell_parses(tmp_path):
    """만든 스크립트가 PowerShell 문법상 올바른지 파서로만 확인한다 (실제 등록하지 않음)."""
    scripts = {"register": scheduler.register_script(["10:00", "18:00"]), "status": scheduler.STATUS_SCRIPT,
               "unregister": scheduler.UNREGISTER_SCRIPT, "run": scheduler.RUN_NOW_SCRIPT}
    for name, body in scripts.items():
        file = tmp_path / f"{name}.ps1"
        file.write_text(body, encoding="utf-8-sig")
        check = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                 f"'{file}', [ref]$null, [ref]$e); $e.Count")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", check],
                             capture_output=True, text=True, timeout=60)
        assert out.stdout.strip() == "0", (name, out.stdout, out.stderr)


def test_schedule_page_ui(env, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTime
    from PySide6.QtWidgets import QApplication
    from ui.schedule_page import SchedulePage
    app = QApplication.instance() or QApplication([])
    db, root, excel = env
    monkeypatch.setattr(scheduler, "status", lambda: scheduler.TaskStatus(False))
    page = SchedulePage(FakeServices(db, FakeClient()))
    assert [page.times.item(i).text() for i in range(page.times.count())] == ["10:00", "18:00"]
    page.time_edit.setTime(QTime(7, 5))
    page.on_add_time()
    assert config.load_settings(db).schedule_times == ["07:05", "10:00", "18:00"]
    assert not page.connect_btn.isEnabled() and not page.test_btn.isEnabled()
    page.rest_key_edit.setText("REST")
    page.on_save_keys()
    assert kakao.has_app_keys() and page.connect_btn.isEnabled() and "로그인 연결 필요" in page.kakao_status.text()
    page.app_check.setChecked(False)
    assert config.load_settings(db).kakao_notify_app is False
    page.deleteLater()
    app.processEvents()


def test_test_message_button_sends_once_on_double_click(env, monkeypatch):
    """회귀: 전송 중 버튼을 다시 눌러도 테스트 메시지가 두 번 가지 않는다."""
    import os
    import time
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from ui.schedule_page import SchedulePage
    app = QApplication.instance() or QApplication([])
    db, root, excel = env
    connected()
    monkeypatch.setattr(scheduler, "status", lambda: scheduler.TaskStatus(False))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    sent = []

    def slow_send(text, link, **kw):
        time.sleep(0.3)
        sent.append(text)
    monkeypatch.setattr(kakao, "send_to_me", slow_send)
    page = SchedulePage(FakeServices(db, FakeClient()))
    assert page.test_btn.isEnabled()
    page.test_btn.click()
    page.test_btn.click()                     # 전송 중 두 번째 클릭
    assert not page.test_btn.isEnabled()
    deadline = time.time() + 5
    while not page.test_btn.isEnabled() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    assert len(sent) == 1 and page.test_btn.isEnabled()
    page.deleteLater()
    app.processEvents()


def test_scheduler_uses_windowless_exe_when_frozen(monkeypatch, tmp_path):
    import sys
    from services import scheduler
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "SmartStoreApp.exe"))
    exe, args, folder = scheduler.run_command()
    assert exe == str(tmp_path / "SmartStoreAuto.exe") and args == "run --notify" and folder == str(tmp_path)
