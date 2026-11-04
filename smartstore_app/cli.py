"""스마트스토어 자동화 명령줄 도구 — 화면 없이 services/를 그대로 호출한다.

    ..\\.venv\\Scripts\\python.exe cli.py run --dry-run          대기 상품 검증·미리보기 (API 호출 없음)
    ..\\.venv\\Scripts\\python.exe cli.py run                    대기 상품 등록 (기본: 판매중지로 등록 → 검수)
    ..\\.venv\\Scripts\\python.exe cli.py init-template --out 상품입력.xlsx
    ..\\.venv\\Scripts\\python.exe cli.py qr-cards --open        촬영할 상품의 QR 카드 A4 PDF
    ..\\.venv\\Scripts\\python.exe cli.py sort-photos            동기화 폴더의 촬영 사진을 상품 폴더로 분리(복사)
    ..\\.venv\\Scripts\\python.exe cli.py process-images       대기 상품 사진 보정 (--code A001 / --all)
    ..\\.venv\\Scripts\\python.exe cli.py preview --open         대기 상품 상세페이지 미리보기 (사진 보정 포함)
    ..\\.venv\\Scripts\\python.exe cli.py draft --code A001       AI 초안 (빈 상품명(최종)·설명(최종)만 채움, --set-key로 키 저장)
    ..\\.venv\\Scripts\\python.exe cli.py publish              검수 대기 목록 / --codes A001,A002 판매 시작
    ..\\.venv\\Scripts\\python.exe cli.py schedule --install     예약 실행 등록 (--times 10:00,18:00 / --remove)
    ..\\.venv\\Scripts\\python.exe cli.py kakao-login            카카오톡 알림 연결 (최초 1회), kakao-test로 확인
    ..\\.venv\\Scripts\\python.exe cli.py log                    최근 실행 내역

종료 코드: 0 정상 / 1 실패한 상품 있음 / 2 실행 오류
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from services import scheduler
from services.automation.config import load_mappings, load_settings, save_settings
from services.automation.input_sheet import STATUS_FAILED, STATUS_WAITING, read_items, write_template
from services.automation.review import pending_review, publish
from services.automation.run_lock import AlreadyRunningError, RunLock
from services import ai_draft
from services.automation.runner import (
    format_drafts, format_report, lock_path, make_drafts, preview_pages, process_images, run_waiting,
)
from services.images.photo_sort import sort_photos
from services.images.qr_cards import make_pdf
from services.connection import AppServices, NotConfiguredError
from services.notify import kakao
from services.notify.dispatch import notify_error, notify_run
from storage.db import Database, app_data_dir

def setup_logging(verbose: bool) -> None:
    handler = RotatingFileHandler(app_data_dir() / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handlers: list[logging.Handler] = [handler]
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers)


def _out(text: str = "") -> None:
    print(text, flush=True)


def cmd_run(args, db: Database) -> int:
    codes = [c.strip() for c in args.codes.split(",")] if args.codes else None
    progress = None
    if not args.dry_run:
        progress = lambda code, i, n: _out(f"  ({i}/{n}) {code} 처리 중...")  # noqa: E731
    try:
        report = run_waiting(AppServices(db), Path(args.excel) if args.excel else None, dry_run=args.dry_run,
                             codes=codes, on_progress=progress)
    except Exception as e:
        if args.notify and not args.dry_run and not isinstance(e, AlreadyRunningError):
            status = notify_error(db, str(e))
            if status:
                _out(status)
        raise
    _out(format_report(report))
    if args.notify:
        status = notify_run(db, report, "schedule")
        if status:
            _out(status)
    failed = report.failed_items if args.dry_run else [r for r in report.results.values() if r.status == STATUS_FAILED]
    return 1 if failed else 0


def cmd_kakao_login(args, db: Database) -> int:
    settings = load_settings(db)
    if args.rest_key:
        kakao.save_app_keys(args.rest_key, args.client_secret or "")
    code = args.code
    if not code:
        _out("아래 주소를 브라우저에서 열어 카카오 로그인 후 '동의하고 계속하기'를 누르세요.")
        _out("이동된 주소창의 주소 전체를 복사해 붙여넣으세요. (페이지가 안 열려도 주소만 있으면 됩니다)\n")
        _out(kakao.authorize_url(settings.kakao_redirect_uri) + "\n")
        code = input("이동된 주소: ")
    kakao.connect(code, settings.kakao_redirect_uri)
    _out("카카오톡 연결을 완료했습니다. 'cli.py kakao-test'로 확인하세요.")
    return 0


def cmd_kakao_test(args, db: Database) -> int:
    kakao.send_to_me("[스마트스토어] 알림 테스트입니다.\n자동 등록 결과가 이렇게 전송됩니다.",
                     load_settings(db).kakao_link_url)
    _out("카카오톡으로 테스트 메시지를 보냈습니다.")
    return 0


def cmd_schedule(args, db: Database) -> int:
    settings = load_settings(db)
    if args.times:
        settings.schedule_times = scheduler.normalize_times(args.times.split(","))
        save_settings(db, settings)
    if args.export:
        path = scheduler.export_script(Path(args.export), settings.schedule_times)
        _out(f"등록 스크립트를 저장했습니다: {path}")
        return 0
    if args.install:
        scheduler.register(settings.schedule_times)
        _out(f"예약 실행을 등록했습니다: 매일 {', '.join(settings.schedule_times)}")
    elif args.remove:
        scheduler.unregister()
        _out("예약 실행을 해제했습니다.")
    elif args.run_now:
        scheduler.run_now()
        _out("예약 작업을 지금 실행했습니다. 결과는 'cli.py log'로 확인하세요.")
    _out(scheduler.status().describe())
    return 0


def cmd_init_template(args, db: Database) -> int:
    out = Path(args.out) if args.out else load_settings(db).work_excel_path()
    if out.exists() and not args.force:
        _out(f"이미 파일이 있습니다: {out}\n덮어쓰려면 --force를 붙이세요. (입력한 내용이 사라집니다)")
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    names = list(load_mappings(db))
    write_template(out, names)
    _out(f"양식을 만들었습니다: {out}  (분류 {len(names)}개: {', '.join(names) or '없음 — 자동화 설정에서 추가'})")
    return 0


def cmd_process_images(args, db: Database) -> int:
    codes = [c.strip() for c in args.code.split(",") if c.strip()] if args.code else None
    results = process_images(db, codes=codes, all_folders=args.all)
    if not results:
        _out("보정할 상품이 없습니다.")
        return 0
    _out(f"사진 폴더: {load_settings(db).image_root_path()}")
    for r in results:
        _out(f"■ {r.code}  {r.summary()}")
        for e in r.errors:
            _out(f"    [오류] {e}")
    failed = [r for r in results if r.errors and not r.processed and not r.skipped]
    _out(f"\n결과: {len(results)}개 상품 · 보정 {sum(len(r.processed) for r in results)}장 · "
         f"건너뜀 {sum(r.skipped for r in results)}장 · 실패 상품 {len(failed)}개")
    return 1 if failed else 0


def cmd_preview(args, db: Database) -> int:
    codes = [c.strip() for c in args.code.split(",") if c.strip()] if args.code else None
    results = preview_pages(db, codes=codes, process=not args.no_process)
    if not results:
        _out("미리볼 상품이 없습니다.")
        return 0
    for p in results:
        _out(f"■ {p.code}  {p.preview or '미리보기 없음'}")
        for e in p.errors:
            _out(f"    [오류] {e}")
    made = [p.preview for p in results if p.preview]
    if made and args.open:
        os.startfile(made[0])  # 기본 브라우저로 열기
    _out(f"\n결과: {len(made)}/{len(results)}개 미리보기 생성 · 폴더: {made[0].parent if made else '-'}")
    return 0 if len(made) == len(results) else 1


def cmd_publish(args, db: Database) -> int:
    pending = pending_review(db)
    if not args.codes and not args.all:
        _out(f"검수 대기(등록완료·판매중지) {len(pending)}건")
        for item in pending:
            price = f"{item.sale_price:,}원" if item.sale_price else "-"
            ai = "  [AI 초안 - 검수 필요]" if item.ai_draft else ""
            _out(f"  {item.code}  {item.name}  {price}  원상품번호 {item.origin_product_no}{ai}")
        _out("판매 시작: cli.py publish --codes A001,A002  (전부: --all)")
        return 0
    codes = [p.code for p in pending] if args.all else [c.strip() for c in args.codes.split(",") if c.strip()]
    if not codes:
        _out("판매 시작할 상품이 없습니다.")
        return 0
    results, excel_written = publish(AppServices(db), codes)
    for r in results:
        _out(f"■ {r.code}  [{'판매 시작' if r.ok else '실패'}]  {r.message}")
    ok = sum(r.ok for r in results)
    _out(f"\n결과: 판매 시작 {ok}건 / 실패 {len(results) - ok}건"
         + ("" if excel_written else " · 작업 엑셀이 열려 있어 결과는 DB에 보관(다음 실행 때 반영)"))
    return 0 if ok == len(results) else 1


def cmd_sort_photos(args, db: Database) -> int:
    settings = load_settings(db)
    inbox = Path(args.inbox) if args.inbox else (Path(settings.photo_inbox) if settings.photo_inbox else None)
    if inbox is None:
        _out("촬영 사진 폴더가 설정되지 않았습니다. --inbox로 지정하거나 앱의 '촬영 사진' 화면에서 설정하세요.")
        return 2
    with RunLock(lock_path(), "sort-photos"):
        known = {i.code for i in read_items(settings.work_excel_path())} if settings.work_excel_path().is_file() else None
        report = sort_photos(db, inbox, settings.image_root_path(), settings.photo_max_gap_hours, known)
    _out(f"촬영 사진 폴더: {inbox}")
    for code, files in sorted(report.assigned.items()):
        _out(f"■ {code}  {len(files)}장 → {files[0].parent}")
    if report.unsorted:
        _out(f"[주의] 미분류 {len(report.unsorted)}장 → {report.unsorted[0].parent}")
    for w in report.warnings:
        _out(f"[주의] {w}")
    _out(f"\n결과: {report.summary()} · 이전에 처리한 사진 {report.already}장")
    return 1 if report.unsorted else 0


def cmd_qr_cards(args, db: Database) -> int:
    settings = load_settings(db)
    items = read_items(settings.work_excel_path())
    names = {i.code: i.get("원본상품명") for i in items}
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = [i.code for i in items if i.status == STATUS_WAITING and i.get("입력방식") == "촬영" and i.code]
    if not codes:
        _out("QR 카드를 만들 상품이 없습니다. (작업 엑셀에 '대기'·'촬영' 상품이 없거나 --codes를 지정하세요)")
        return 2
    out = Path(args.out) if args.out else settings.work_excel_path().with_name("QR카드.pdf")
    make_pdf([(c, names.get(c, "")) for c in codes], out)
    _out(f"QR 카드 {len(codes)}장을 만들었습니다: {out}  (A4 {-(-len(codes) // 8)}장)")
    if args.open:
        os.startfile(out)
    return 0


def cmd_draft(args, db: Database) -> int:
    if args.set_key:
        import getpass
        key = getpass.getpass("Claude API 키 (입력 내용은 보이지 않습니다): ").strip()
        if not key:
            _out("입력이 없어 그대로 둡니다.")
            return 0
        model = load_settings(db).ai_model
        _out(f"키 확인: {ai_draft.check_api_key(key, model)} 사용 가능")
        ai_draft.save_api_key(key)
        _out("Claude API 키를 Windows 자격 증명 관리자에 저장했습니다.")
        return 0
    codes = [c.strip() for c in args.code.split(",") if c.strip()] if args.code else None
    results, excel_error = make_drafts(db, codes, force=args.force,
                                       on_progress=lambda code, i, n: _out(f"  ({i}/{n}) {code} AI 초안 만드는 중..."))
    if not results:
        _out("AI 초안을 만들 상품이 없습니다. ('대기' 상품 중 상품명(최종)·설명(최종)이 빈 상품이 대상)")
        return 0
    _out(format_drafts(results, load_settings(db).banned_words, excel_error))
    return 1 if any(r.error for r in results) else 0


def cmd_log(args, db: Database) -> int:
    rows = db.list_run_log(limit=args.limit)
    for r in reversed(rows):
        line = f"{r['created_at']}  {r['run_id']}  {r['action']:<9} {r['result']:<4} {r['code'] or '':<8} {r['summary'] or ''}"
        _out(line + (f"\n    [오류] {r['error']}" if r["error"] else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli", description="스마트스토어 상품 등록 자동화")
    parser.add_argument("-v", "--verbose", action="store_true", help="로그를 화면에도 출력")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="작업 엑셀의 대기 상품 등록 (기본: 판매중지로 등록)")
    run.add_argument("--dry-run", action="store_true", help="API 호출 없이 검증·미리보기만")
    run.add_argument("--excel", help="작업 엑셀 경로 (기본: 자동화 설정의 경로)")
    run.add_argument("--codes", help="특정 상품코드만 (쉼표 구분)")
    run.add_argument("--notify", action="store_true", help="끝나면 카카오톡 알림 (예약 실행에서 사용)")

    p = sub.add_parser("kakao-login", help="카카오톡 알림 연결 (최초 1회)")
    p.add_argument("--rest-key", help="카카오 REST API 키 (처음 한 번)")
    p.add_argument("--client-secret", help="카카오 Client Secret (사용하는 경우)")
    p.add_argument("--code", help="인가코드 또는 이동된 주소 (없으면 입력받음)")
    sub.add_parser("kakao-test", help="카카오톡 테스트 메시지 보내기")

    p = sub.add_parser("schedule", help="예약 실행(Windows 작업 스케줄러) 조회·등록·해제")
    p.add_argument("--times", help="실행 시각 (예: 10:00,18:00) — 설정에 저장")
    p.add_argument("--install", action="store_true", help="등록(이미 있으면 갱신)")
    p.add_argument("--remove", action="store_true", help="해제")
    p.add_argument("--run-now", action="store_true", help="지금 한 번 실행")
    p.add_argument("--export", help="등록용 PowerShell 스크립트(.ps1) 저장 경로")

    init = sub.add_parser("init-template", help="작업 엑셀 양식 만들기")
    init.add_argument("--out", help="저장 경로 (기본: 자동화 설정의 작업 엑셀 경로)")
    init.add_argument("--force", action="store_true", help="이미 있으면 덮어쓰기")

    log = sub.add_parser("log", help="최근 실행 내역")
    log.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("sort-photos", help="동기화 폴더의 촬영 사진을 QR 카드 기준으로 상품 폴더에 분리 (복사)")
    p.add_argument("--inbox", help="촬영 사진 폴더 (기본: 설정의 폴더)")
    p = sub.add_parser("qr-cards", help="상품코드 QR 카드 A4 PDF 만들기")
    p.add_argument("--codes", help="상품코드 (쉼표 구분). 비우면 작업 엑셀의 '대기'·'촬영' 상품")
    p.add_argument("--out", help="저장할 PDF 경로 (기본: 작업 엑셀 폴더의 QR카드.pdf)")
    p.add_argument("--open", action="store_true", help="만든 PDF 열기")
    p = sub.add_parser("process-images", help="사진 보정 (원본/ → 보정/, API 호출 없음)")
    p.add_argument("--code", help="상품코드 (쉼표로 여러 개). 비우면 작업 엑셀의 '대기' 상품")
    p.add_argument("--all", action="store_true", help="사진 폴더의 모든 상품")
    p = sub.add_parser("preview", help="상세페이지 미리보기 HTML 만들기 (사진 보정 포함, API 호출 없음)")
    p.add_argument("--code", help="상품코드 (쉼표로 여러 개). 비우면 작업 엑셀의 '대기' 상품")
    p.add_argument("--no-process", action="store_true", help="사진 보정 없이 지금 있는 사진으로")
    p.add_argument("--open", action="store_true", help="첫 미리보기를 브라우저로 열기")
    p = sub.add_parser("draft", help="AI 초안 생성 (Claude API, 빈 상품명(최종)·설명(최종)만 채움)")
    p.add_argument("--code", help="상품코드 (쉼표로 여러 개). 비우면 '대기' 상품 중 빈 칸이 있는 상품")
    p.add_argument("--force", action="store_true", help="보관된 초안을 버리고 다시 만들기 (API 다시 호출)")
    p.add_argument("--set-key", action="store_true", help="Claude API 키 저장 (keyring)")
    p = sub.add_parser("publish", help="검수 끝난 상품 판매 시작 (판매중 + 전시중). 옵션 없이 실행하면 검수 대기 목록")
    p.add_argument("--codes", help="판매 시작할 상품코드 (쉼표 구분)")
    p.add_argument("--all", action="store_true", help="검수 대기 상품 전부")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # 콘솔 코드페이지에 없는 문자로 죽지 않도록
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    db = Database()
    try:
        handler = {"run": cmd_run, "init-template": cmd_init_template, "log": cmd_log,
                   "process-images": cmd_process_images, "preview": cmd_preview,
                   "publish": cmd_publish, "kakao-login": cmd_kakao_login, "kakao-test": cmd_kakao_test,
                   "schedule": cmd_schedule, "sort-photos": cmd_sort_photos, "qr-cards": cmd_qr_cards,
                   "draft": cmd_draft}[args.command]
        return handler(args, db)
    except (AlreadyRunningError, FileNotFoundError, ValueError, NotImplementedError, PermissionError,
            NotConfiguredError, kakao.KakaoError, scheduler.SchedulerError, ai_draft.AiDraftError) as e:
        _out(f"오류: {e}")
        return 2
    except Exception:
        logging.getLogger("cli").exception("실행 오류")
        _out(f"예상하지 못한 오류가 발생했습니다. 로그를 확인하세요: {app_data_dir() / 'app.log'}")
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
