"""Windows 작업 스케줄러 — 정해진 시각에 'cli run --notify'를 실행하는 작업을 등록·해제·조회한다.

PowerShell의 Register-ScheduledTask를 쓴다 (하루 여러 시각 = 트리거 여러 개).
- PC가 꺼져 있어 놓친 실행은 켜진 뒤 한 번 실행 (StartWhenAvailable)
- 이미 실행 중이면 새로 시작하지 않음 (MultipleInstances IgnoreNew) + 앱의 실행 잠금
- 현재 사용자로 로그인해 있을 때 실행 (keyring의 인증 정보를 쓰기 위해)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

TASK_NAME = "SmartStoreApp_AutoRun"
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_NEVER_RAN = 267011   # SCHED_S_TASK_HAS_NOT_RUN


class SchedulerError(Exception):
    pass


@dataclass
class TaskStatus:
    registered: bool
    state: str = ""
    next_run: str = ""
    last_run: str = ""
    last_result: int | None = None
    times: list[str] | None = None

    def describe(self) -> str:
        if not self.registered:
            return "예약 실행이 등록되어 있지 않습니다."
        last = "아직 실행 안 됨" if self.last_result in (None, _NEVER_RAN) else \
            f"{self.last_run} ({'성공' if self.last_result == 0 else f'종료 코드 {self.last_result}'})"
        return f"등록됨 · 시각 {', '.join(self.times or [])} · 다음 실행 {self.next_run or '-'} · 마지막 실행 {last}"


def normalize_times(times: list[str]) -> list[str]:
    result = set()
    for t in times:
        t = t.strip()
        if len(t) == 4 and t[1] == ":":
            t = "0" + t
        if not _TIME.match(t):
            raise SchedulerError(f"시각은 HH:MM 형식이어야 합니다: {t}")
        result.add(t)
    if not result:
        raise SchedulerError("예약 시각을 하나 이상 입력하세요.")
    return sorted(result)


def run_command() -> tuple[str, str, str]:
    """(실행 파일, 인자, 작업 폴더). 콘솔 창이 뜨지 않게 pythonw 또는 SmartStoreAuto.exe를 쓴다."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name("SmartStoreAuto.exe")   # 창 없는 명령줄 exe (같은 폴더)
        return str(exe), "run --notify", str(exe.parent)
    app_dir = Path(__file__).resolve().parent.parent
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else python), f'"{app_dir / "cli.py"}" run --notify', str(app_dir)


def _ps(value: str) -> str:
    """PowerShell 작은따옴표 문자열."""
    return "'" + value.replace("'", "''") + "'"


def register_script(times: list[str], command: tuple[str, str, str] | None = None) -> str:
    exe, args, workdir = command or run_command()
    triggers = ", ".join(f"(New-ScheduledTaskTrigger -Daily -At {_ps(t)})" for t in normalize_times(times))
    return "\n".join([
        f"$action = New-ScheduledTaskAction -Execute {_ps(exe)} -Argument {_ps(args)} -WorkingDirectory {_ps(workdir)}",
        f"$triggers = @({triggers})",
        "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)",
        f"Register-ScheduledTask -TaskName {_ps(TASK_NAME)} -Action $action -Trigger $triggers -Settings $settings "
        f"-Description {_ps('스마트스토어 대기 상품 자동 등록 (SmartStoreApp)')} -Force | Out-Null",
        "'ok'",
    ])


UNREGISTER_SCRIPT = (f"Unregister-ScheduledTask -TaskName {_ps(TASK_NAME)} -Confirm:$false -ErrorAction SilentlyContinue; "
                     "'ok'")
RUN_NOW_SCRIPT = f"Start-ScheduledTask -TaskName {_ps(TASK_NAME)}; 'ok'"
STATUS_SCRIPT = f"""
$t = Get-ScheduledTask -TaskName {_ps(TASK_NAME)} -ErrorAction SilentlyContinue
if (-not $t) {{ 'null' }} else {{
  $i = Get-ScheduledTaskInfo -TaskName {_ps(TASK_NAME)}
  $fmt = {{ param($d) if ($d -and $d.Year -gt 2000) {{ $d.ToString('yyyy-MM-dd HH:mm') }} else {{ '' }} }}
  [pscustomobject]@{{
    state = $t.State.ToString(); next = (& $fmt $i.NextRunTime); last = (& $fmt $i.LastRunTime)
    result = $i.LastTaskResult; times = @($t.Triggers | ForEach-Object {{ ([datetime]$_.StartBoundary).ToString('HH:mm') }})
  }} | ConvertTo-Json -Compress
}}"""


Runner = Callable[[str], str]


def run_powershell(script: str) -> str:
    full = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n" + script
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-Command", full], capture_output=True, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip() or proc.stderr.decode("cp949", "replace").strip()
        raise SchedulerError(f"작업 스케줄러 명령 실패: {err or out}")
    return out


def register(times: list[str], runner: Runner = run_powershell) -> None:
    if runner(register_script(times)).splitlines()[-1:] != ["ok"]:
        raise SchedulerError("예약 등록 결과를 확인하지 못했습니다.")


def unregister(runner: Runner = run_powershell) -> None:
    runner(UNREGISTER_SCRIPT)


def run_now(runner: Runner = run_powershell) -> None:
    runner(RUN_NOW_SCRIPT)


def status(runner: Runner = run_powershell) -> TaskStatus:
    out = runner(STATUS_SCRIPT).strip()
    if not out or out == "null":
        return TaskStatus(False)
    data = json.loads(out.splitlines()[-1])
    times = data.get("times") or []
    return TaskStatus(True, data.get("state", ""), data.get("next", ""), data.get("last", ""), data.get("result"),
                      times if isinstance(times, list) else [times])


def export_script(path: Path, times: list[str]) -> Path:
    """사용자가 직접 실행할 수 있는 등록 스크립트(.ps1). PowerShell 5.1이 한글을 읽도록 BOM을 붙인다."""
    body = "\n".join([
        "# 스마트스토어 자동 등록 예약 (Windows 작업 스케줄러)",
        "# 실행: 이 파일을 마우스 오른쪽 클릭 → 'PowerShell에서 실행'",
        f"# 해제: Unregister-ScheduledTask -TaskName {_ps(TASK_NAME)} -Confirm:$false",
        register_script(times),
        "Write-Host '예약을 등록했습니다:' (Get-ScheduledTask -TaskName " + _ps(TASK_NAME) + ").TaskName",
    ])
    path.write_text(body, encoding="utf-8-sig")
    return path
