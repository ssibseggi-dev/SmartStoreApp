"""스마트스토어 상품 등록 앱 진입점.

--self-test: 창을 만들고 리소스·keyring·DB를 점검한 뒤 결과를 데이터 폴더의 selftest.txt에 쓰고 종료(빌드 검증용)
             --api를 함께 주면 카테고리 조회(읽기 전용)로 HTTPS·토큰 발급도 확인
"""
from __future__ import annotations

import faulthandler
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from services.connection import AppServices
from storage.db import Database, app_data_dir
from ui.main_window import MainWindow
from ui.theme import apply_theme, resource_path
from version import APP_NAME, APP_VERSION


def setup_logging() -> None:
    handler = RotatingFileHandler(app_data_dir() / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handlers: list[logging.Handler] = [handler]
    if sys.stderr is not None:  # 창 모드 exe에는 콘솔이 없다
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    # 처리되지 않은 예외와 네이티브 크래시도 로그에 남긴다
    sys.excepthook = lambda *exc: logging.getLogger("main").critical("처리되지 않은 예외", exc_info=exc)
    threading.excepthook = lambda a: logging.getLogger("main").critical(
        "스레드 예외", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    faulthandler.enable(open(app_data_dir() / "crash.log", "a", encoding="utf-8"))


def self_test(app: QApplication, window: MainWindow) -> int:
    import keyring
    from storage import credentials

    checks = {
        "version": APP_VERSION,
        "frozen": getattr(sys, "frozen", False),
        "style.qss": resource_path("style.qss").is_file(),
        "app.ico": resource_path("app.ico").is_file(),
        "stylesheet_applied": len(app.styleSheet()) > 1000,
        "font": app.font().family(),
        "keyring_backend": f"{type(keyring.get_keyring()).__module__}.{type(keyring.get_keyring()).__name__}",
        "credentials_readable": credentials.load() is not None,
        "pages": window.pages.count(),
        "korean_qt": app.translate("QMessageBox", "Show Details...") != "Show Details...",
    }
    if "--api" in sys.argv:  # 읽기 전용 카테고리 조회로 HTTPS·토큰 발급까지 확인
        try:
            checks["api_categories"] = len(window.settings_page.services.client().get("/external/v1/categories"))
        except Exception as e:
            checks["api_categories"] = f"ERROR {e!r}"
    app.processEvents()
    ok = (checks["style.qss"] and checks["app.ico"] and checks["stylesheet_applied"]
          and "keyring.backends.Windows" in checks["keyring_backend"] and checks["pages"] == 9 and checks["korean_qt"]
          and not str(checks.get("api_categories", "")).startswith("ERROR"))
    lines = [f"{k}={v}" for k, v in checks.items()] + [f"RESULT={'OK' if ok else 'FAIL'}"]
    (app_data_dir() / "selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    return 0 if ok else 1


def main() -> int:
    setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    apply_theme(app)
    app.setWindowIcon(QIcon(str(resource_path("app.ico"))))
    db = Database()
    window = MainWindow(AppServices(db))
    window.setWindowTitle(f"{window.windowTitle()}  v{APP_VERSION}")
    if "--self-test" in sys.argv:
        code = self_test(app, window)
    else:
        logging.getLogger("main").info("앱 시작 v%s", APP_VERSION)
        window.show()
        code = app.exec()
    db.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
