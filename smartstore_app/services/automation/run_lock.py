"""같은 작업이 동시에 두 번 실행되지 않도록 하는 잠금 파일. 프로세스가 죽어 남은 잠금은 자동으로 정리한다."""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


class AlreadyRunningError(Exception):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class RunLock:
    def __init__(self, path: Path, name: str = "run"):
        self.path = Path(path)
        self.name = name
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().split()[0])
                except (OSError, ValueError, IndexError):
                    pid = -1
                if _pid_alive(pid):
                    raise AlreadyRunningError(f"이미 실행 중인 작업이 있습니다 (PID {pid}). 끝난 뒤 다시 실행하세요.")
                self.path.unlink(missing_ok=True)  # 비정상 종료로 남은 잠금
                continue
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {self.name}")
            self._acquired = True
            return
        raise AlreadyRunningError("잠금 파일을 만들 수 없습니다.")

    def release(self) -> None:
        if self._acquired:
            self.path.unlink(missing_ok=True)
            self._acquired = False

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
