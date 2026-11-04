@echo off
rem 실행파일 빌드 (더블클릭 또는 터미널에서 build.bat)
rem   build.bat        dist\SmartStoreApp\ 폴더에 SmartStoreApp.exe(화면) · SmartStoreCli.exe(명령줄) · SmartStoreAuto.exe(예약 실행)
rem   build.bat lite   배경 제거(rembg) 없이 작게 빌드
chcp 65001 >nul
cd /d "%~dp0"
set PY="%~dp0..\.venv\Scripts\python.exe"

%PY% -m PyInstaller --version >nul 2>&1 || %PY% -m pip install -r requirements-build.txt || goto :fail

if /i "%1"=="lite" (set SMARTSTORE_LITE=1) else (set SMARTSTORE_LITE=)
%PY% -m PyInstaller SmartStoreApp.spec --noconfirm --clean || goto :fail
copy /y "사용법.md" "dist\SmartStoreApp\" >nul

echo.
echo 완료: dist\SmartStoreApp\SmartStoreApp.exe  (같은 폴더에 SmartStoreCli.exe, SmartStoreAuto.exe)
echo 폴더째로 옮겨서 쓰세요. exe만 따로 복사하면 실행되지 않습니다.
pause
exit /b 0

:fail
echo.
echo 빌드 실패 - 위 오류 메시지를 확인하세요.
pause
exit /b 1
