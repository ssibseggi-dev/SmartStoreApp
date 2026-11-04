# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 빌드 설정 — build.bat에서 사용. 결과(폴더형, 라이브러리 공유):
#   dist\SmartStoreApp\SmartStoreApp.exe    화면 앱
#   dist\SmartStoreApp\SmartStoreCli.exe    명령줄 (터미널에서 실행, 결과를 글자로 보여줌)
#   dist\SmartStoreApp\SmartStoreAuto.exe   예약 실행용 명령줄 (창 없이 실행, 작업 스케줄러가 사용)
# 배경 제거(rembg)를 빼고 작게 빌드하려면: build.bat lite

import os

LITE = os.environ.get("SMARTSTORE_LITE") == "1"

DATAS = [("resources/style.qss", "resources"), ("resources/app.ico", "resources"),
         ("resources/templates", "resources/templates")]
HIDDEN = ["keyring.backends.Windows", "win32ctypes.core", "pillow_heif", "zxingcpp", "qrcode", "jinja2",
          "anthropic", "scipy.ndimage"]
EXCLUDES = ["tkinter", "pytest", "_pytest", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
            "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf", "PySide6.Qt3DCore", "PySide6.QtMultimedia",
            "matplotlib", "IPython", "notebook"]
if LITE:
    EXCLUDES += ["rembg", "onnxruntime", "pymatting", "numba", "llvmlite", "skimage", "pooch"]
else:
    from PyInstaller.utils.hooks import collect_data_files, copy_metadata
    HIDDEN += ["rembg", "onnxruntime"]
    DATAS += collect_data_files("rembg")
    # rembg·pymatting은 import할 때 자기 버전을 패키지 정보(metadata)에서 읽는다
    for package in ("rembg", "pymatting", "onnxruntime", "numba", "llvmlite"):
        DATAS += copy_metadata(package)


def analysis(script, excludes):
    return Analysis([script], pathex=[], binaries=[], datas=DATAS, hiddenimports=HIDDEN, hookspath=[],
                    runtime_hooks=[], excludes=excludes, noarchive=False)


a_gui = analysis("main.py", EXCLUDES)
a_cli = analysis("cli.py", EXCLUDES + ["PySide6", "shiboken6"])   # 명령줄은 Qt 없이 동작

# Qt 플러그인이 끌고 오는 미사용 라이브러리 제거 (위젯 앱이라 Quick/QML/PDF/소프트웨어 OpenGL 불필요)
_DROP = ("opengl32sw.dll", "Qt6Quick", "Qt6Qml", "Qt6Pdf", "Qt6VirtualKeyboard", "qpdf.dll",
         "qtvirtualkeyboardplugin", "qmltooling", "Qt6OpenGL.dll")
a_gui.binaries = [b for b in a_gui.binaries if not any(key in os.path.basename(b[0]) or key in b[0] for key in _DROP)]

pyz_gui = PYZ(a_gui.pure)
pyz_cli = PYZ(a_cli.pure)

common = dict(debug=False, strip=False, upx=False, icon="resources/app.ico", version="version_info.txt",
              exclude_binaries=True)
exe_gui = EXE(pyz_gui, a_gui.scripts, [], name="SmartStoreApp", console=False, **common)
exe_cli = EXE(pyz_cli, a_cli.scripts, [], name="SmartStoreCli", console=True, **common)
exe_auto = EXE(pyz_cli, a_cli.scripts, [], name="SmartStoreAuto", console=False, **common)

coll = COLLECT(exe_gui, exe_cli, exe_auto,
               a_gui.binaries, a_gui.datas, a_cli.binaries, a_cli.datas,
               strip=False, upx=False, name="SmartStoreApp")
