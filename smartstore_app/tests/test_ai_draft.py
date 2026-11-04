"""8단계: AI 초안 (Claude API는 가짜 클라이언트) — 요청 형태, 빈 칸만 채우기, 키 없음·실패 시 건너뛰기, 보관·재사용."""
import base64
import io
import json
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook
from PIL import Image

import cli
from services import ai_draft
from services.automation import config, runner
from services.automation.input_sheet import HEADERS, ResultUpdate, read_items, write_results
from services.notify.summary import run_summary
from tests.test_automation_register import FakeClient, FakeServices, add_rows, env, photos, row  # noqa: F401

AI_NAME = "남녀공용 코튼 라운드넥 반팔 티셔츠"
ANSWER = {
    "names": [AI_NAME, "최저가 코튼 반팔티", "베이직 데일리 반팔 티셔츠", "네번째 후보"],
    "features": ["군더더기 없는 라운드넥 디자인입니다.", "어깨선이 자연스럽게 떨어지는 핏입니다.", "  "],
    "tags": ["#반팔티", "코튼 티셔츠", "반팔티", "데일리룩"],
    "colors": ["화이트", "블랙"],
}


class FakeClaude:
    """anthropic.Anthropic 대역. beta.messages.create 호출을 기록한다."""

    def __init__(self, answer=None, stop_reason="end_turn", error=None):
        self.answer, self.stop_reason, self.error = answer or ANSWER, stop_reason, error
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(retrieve=lambda model: SimpleNamespace(display_name="Claude Opus 5.5"))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop_reason, model=kwargs["model"],
                               content=[SimpleNamespace(type="thinking", thinking=""),
                                        SimpleNamespace(type="text", text=json.dumps(self.answer, ensure_ascii=False))])


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(ai_draft, "_client", lambda api_key=None: fake)
    ai_draft.save_api_key("sk-ant-test")
    yield fake
    ai_draft.delete_api_key()


def cells(excel, *names):
    ws = load_workbook(excel)["상품"]
    col = {h: i for i, h in enumerate(HEADERS)}
    return {r[col["상품코드"]]: tuple(r[col[n]] for n in names) for r in ws.iter_rows(min_row=2, values_only=True) if r[0]}


def item_of(excel, code):
    return next(i for i in read_items(excel) if i.code == code)


# --- 요청·응답 ---
def test_generate_request_and_cleanup(env, claude):
    db, root, excel = env
    photos(root, "A001")
    for i in range(3, 7):
        Image.new("RGB", (2000, 1500), (200, 40, 40)).save(root / "img" / "A001" / "원본" / f"0{i}.jpg")
    add_rows(excel, [row("A001", 소재="면 100%", 옵션="색상:화이트,블랙")])
    item = item_of(excel, "A001")
    from services.images import product_images
    images = product_images(root / "img", "A001")
    draft = ai_draft.generate(item, ai_draft.pick_images(images), ["최저가", "정품"])

    call = claude.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["betas"] == [ai_draft.FALLBACK_BETA]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "지어내" in call["system"] and "원산지" in call["system"]
    content = call["messages"][0]["content"]
    blocks = [b for b in content if b["type"] == "image"]
    assert len(blocks) == 4                                                  # 사진은 최대 4장
    img = Image.open(io.BytesIO(base64.b64decode(blocks[-1]["source"]["data"])))
    assert max(img.size) <= 1024                                             # 줄여서 보냄
    prompt = content[-1]["text"]
    assert "소재: 면 100%" in prompt and "옵션: 색상:화이트,블랙" in prompt and "금지어: 최저가, 정품" in prompt
    assert "세탁" not in prompt                                              # 빈 칸은 보내지 않음

    assert len(draft.names) == 3 and draft.features == ANSWER["features"][:2]
    assert draft.tags == ["반팔티", "코튼티셔츠", "데일리룩"]                    # # 과 공백 제거, 중복 제거
    assert draft.usable_names(["최저가"]) == [AI_NAME, "베이직 데일리 반팔 티셔츠"]
    assert ai_draft.AiDraft(names=["가" * 51]).usable_names([]) == []        # 50자 초과 제외


@pytest.mark.parametrize("fake, message", [
    (FakeClaude(stop_reason="refusal"), "답하지 않았습니다"),
    (FakeClaude(stop_reason="max_tokens"), "잘렸습니다"),
    (FakeClaude(answer={"names": [], "features": [], "tags": [], "colors": []}), "상품명·특징이 없습니다"),
])
def test_generate_errors(env, fake, message):
    db, root, excel = env
    photos(root, "A001")
    add_rows(excel, [row("A001")])
    with pytest.raises(ai_draft.AiDraftError, match=message):
        ai_draft.generate(item_of(excel, "A001"), [root / "img" / "A001" / "원본" / "01.png"], [], client=fake)
    with pytest.raises(ai_draft.AiDraftError, match="사진이 없습니다"):
        ai_draft.generate(item_of(excel, "A001"), [], [], client=fake)


def test_low_credit_message(env):
    import anthropic
    import httpx2
    db, root, excel = env
    photos(root, "A001")
    add_rows(excel, [row("A001")])
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.BadRequestError(
        "Your credit balance is too low to access the Anthropic API.", response=httpx2.Response(400, request=request),
        body={"type": "error", "error": {"type": "invalid_request_error",
                                         "message": "Your credit balance is too low to access the Anthropic API."}})
    with pytest.raises(ai_draft.AiDraftError, match="크레딧이 부족합니다"):
        ai_draft.generate(item_of(excel, "A001"), [root / "img" / "A001" / "원본" / "01.png"], [],
                          client=FakeClaude(error=error))


def test_api_key_only_in_keyring(env):
    db, _, _ = env
    assert not ai_draft.has_api_key()
    ai_draft.save_api_key("  sk-ant-secret  ")
    assert ai_draft.get_api_key() == "sk-ant-secret"
    assert "sk-ant-secret" not in json.dumps(config.load_settings(db).__dict__, ensure_ascii=False)
    ai_draft.delete_api_key()
    ai_draft.delete_api_key()                                                # 없어도 오류 없음
    assert not ai_draft.has_api_key()
    assert ai_draft.check_api_key(client=FakeClaude()) == "Claude Opus 5.5"


# --- 등록 흐름 ---
def test_run_fills_only_empty_cells_and_marks_review(env, claude):
    db, root, excel = env
    for code in ("A001", "A002", "A003"):
        photos(root, code)
    add_rows(excel, [row("A001"),
                     row("A002", **{"상품명(최종)": "사람이 정한 상품명"}),
                     row("A003", **{"상품명(최종)": "직접 입력", "설명(최종)": "직접 쓴 설명"})])
    client = FakeClient()
    report = runner.run_waiting(FakeServices(db, client), dry_run=False)

    assert len(claude.calls) == 2                                            # 빈 칸 없는 A003은 호출 안 함
    names = [p["originProduct"]["name"] for p in client.registered]
    assert names == [AI_NAME, "사람이 정한 상품명", "직접 입력"]
    result = cells(excel, "상품명(최종)", "설명(최종)", "결과메시지")
    assert result["A001"][0] == AI_NAME and result["A001"][1].startswith("군더더기 없는")
    assert result["A002"][0] == "사람이 정한 상품명" and result["A002"][1]      # 사람 값 유지, 빈 설명만 채움
    assert result["A003"][:2] == ("직접 입력", "직접 쓴 설명")
    assert result["A001"][2].startswith(ai_draft.AI_MARK) and f"상품명 AI: {AI_NAME}" in result["A001"][2]
    assert ai_draft.AI_MARK not in result["A003"][2]
    assert "군더더기 없는 라운드넥" in client.registered[0]["originProduct"]["detailContent"]
    assert [bool(db.get_automation_item(c)["ai_draft"]) for c in ("A001", "A002", "A003")] == [True, True, False]
    assert "AI 초안 2건 검수 필요" in run_summary(report, 3)
    assert "[AI 초안 - 검수 필요] 상품명(최종)·설명(최종)" in runner.format_report(report)


def test_ai_skipped_without_key_or_on_failure(env, monkeypatch):
    db, root, excel = env
    photos(root, "A001")
    photos(root, "A002")
    add_rows(excel, [row("A001")])
    report = runner.run_waiting(FakeServices(db, FakeClient()), dry_run=False)     # 키 없음
    assert "Claude API 키가 없어 AI 초안을 건너뛰었습니다" in runner.format_report(report)
    assert cells(excel, "상품명(최종)")["A001"] == (None,)

    import anthropic
    import httpx2
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    fake = FakeClaude(error=error)
    monkeypatch.setattr(ai_draft, "_client", lambda api_key=None: fake)
    ai_draft.save_api_key("sk-ant-test")
    add_rows(excel, [row("A002")])
    client = FakeClient()
    report = runner.run_waiting(FakeServices(db, client), dry_run=False)
    assert len(fake.calls) == 1 and client.registered[0]["originProduct"]["name"] == "상품 A002"   # 규칙 상품명으로 등록
    assert "AI 초안 실패(Claude API에 연결하지 못했습니다.)" in cells(excel, "결과메시지")["A002"][0]
    assert db.get_ai_draft("A002") is None
    ai_draft.delete_api_key()


def test_ai_disabled_setting(env, claude):
    db, root, excel = env
    photos(root, "A001")
    add_rows(excel, [row("A001")])
    s = config.load_settings(db)
    s.ai_enabled = False
    config.save_settings(db, s)
    runner.run_waiting(FakeServices(db, FakeClient()), dry_run=False)
    assert not claude.calls


def test_dry_run_never_calls_api_but_uses_saved_draft(env, claude):
    db, root, excel = env
    photos(root, "A001")
    add_rows(excel, [row("A001")])
    report = runner.run_waiting(FakeServices(db, FakeClient()), dry_run=True)
    assert not claude.calls and "AI 초안은 실제 실행 때 만듭니다" in runner.format_report(report)

    results, error = runner.make_drafts(db, ["A001"])
    assert len(claude.calls) == 1 and not error and results[0].fills
    report = runner.run_waiting(FakeServices(db, FakeClient()), dry_run=True)
    assert len(claude.calls) == 1                                            # 보관된 초안 재사용
    assert report.prepared[0].draft.name == AI_NAME and report.prepared[0].ai_draft


def test_make_drafts_cache_force_and_human_edit(env, claude):
    db, root, excel = env
    photos(root, "A001")
    add_rows(excel, [row("A001"), row("A002", **{"상품명(최종)": "이름", "설명(최종)": "설명"})])
    results, _ = runner.make_drafts(db)                                      # 빈 칸 있는 대기 상품만
    assert [r.code for r in results] == ["A001"] and cells(excel, "상품명(최종)")["A001"] == (AI_NAME,)
    assert ai_draft.is_ai_origin(db, item_of(excel, "A001"))

    results, _ = runner.make_drafts(db, ["A001"])                            # 보관본 사용, 이미 채운 칸은 그대로
    assert len(claude.calls) == 1 and results[0].cached and not results[0].fills
    runner.make_drafts(db, ["A001"], force=True)
    assert len(claude.calls) == 2

    wb = load_workbook(excel)                                                # 사람이 상품명을 고침
    ws = wb["상품"]
    ws.cell(2, HEADERS.index("상품명(최종)") + 1).value = "사람이 고친 이름"
    ws.cell(2, HEADERS.index("설명(최종)") + 1).value = "사람이 고친 설명"
    wb.save(excel)
    assert not ai_draft.is_ai_origin(db, item_of(excel, "A001"))
    with pytest.raises(ValueError, match="작업 엑셀에 없는 상품코드"):
        runner.make_drafts(db, ["Z999"])


def test_write_results_fills_never_overwrite(env):
    db, root, excel = env
    add_rows(excel, [row("A001", **{"상품명(최종)": "사람이 적은 값"})])
    write_results(excel, {"A001": ResultUpdate(None, fills={"상품명(최종)": "AI 값", "설명(최종)": "AI 설명"})})
    assert cells(excel, "상품명(최종)", "설명(최종)", "상태")["A001"] == ("사람이 적은 값", "AI 설명", "대기")


def test_excel_locked_fills_written_next_run(env, claude, monkeypatch):
    db, root, excel = env
    photos(root, "A001")
    add_rows(excel, [row("A001")])
    real = runner.write_results
    monkeypatch.setattr(runner, "write_results", lambda *a, **k: (_ for _ in ()).throw(PermissionError("열림")))
    runner.run_waiting(FakeServices(db, FakeClient()), dry_run=False)
    assert cells(excel, "상품명(최종)")["A001"] == (None,)
    monkeypatch.setattr(runner, "write_results", real)
    runner.run_waiting(FakeServices(db, FakeClient()), dry_run=False)          # 지난 결과 반영 + AI 값 채움
    assert cells(excel, "상품명(최종)", "상태")["A001"] == (AI_NAME, "등록완료(판매중지)")


def test_cli_draft(env, claude, capsys):
    db, root, excel = env
    photos(root, "A001")
    add_rows(excel, [row("A001")])
    assert cli.main(["draft", "--code", "A001"]) == 0
    out = capsys.readouterr().out
    assert f"상품명 후보 1: {AI_NAME}" in out and "최저가 코튼 반팔티" in out
    assert "검색 태그: 반팔티, 코튼티셔츠, 데일리룩" in out and "엑셀 빈 칸 채움" in out
    ai_draft.delete_api_key()
    assert cli.main(["draft"]) == 2 and "Claude API 키가 없습니다" in capsys.readouterr().out


# --- 저장된 초안 보기·후보 고르기 ---
def test_list_saved_and_apply_choice(env, claude):
    db, root, excel = env
    photos(root, "A001")
    photos(root, "A002")
    add_rows(excel, [row("A001"), row("A002")])
    runner.make_drafts(db)
    saved = {s.code: s for s in ai_draft.list_saved(db, excel)}
    assert set(saved) == {"A001", "A002"} and saved["A001"].editable and saved["A001"].name == AI_NAME
    assert ai_draft.list_saved(db, root / "없음.xlsx")[0].in_excel is False      # 엑셀이 없어도 목록은 보임

    ai_draft.apply_choice(db, excel, "A001", name="베이직 데일리 반팔 티셔츠")       # 사람이 다른 후보를 고름
    assert cells(excel, "상품명(최종)", "상태")["A001"] == ("베이직 데일리 반팔 티셔츠", "대기")
    assert ai_draft.is_ai_origin(db, item_of(excel, "A001"))                      # 계속 '검수 필요'

    runner.run_waiting(FakeServices(db, FakeClient()), dry_run=False)            # A001·A002 등록
    with pytest.raises(ValueError, match=r"이미 '등록완료\(판매중지\)' 상태"):
        ai_draft.apply_choice(db, excel, "A001", name=AI_NAME)
    with pytest.raises(ValueError, match="없습니다"):
        ai_draft.apply_choice(db, excel, "Z999", name=AI_NAME)


def test_ai_draft_dialog_and_review_button(env, claude, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    from services.automation.review import pending_review
    from ui.ai_draft_dialog import AiDraftDialog
    from ui.review_page import ReviewPage
    app = QApplication.instance() or QApplication([])
    db, root, excel = env
    photos(root, "A001")
    add_rows(excel, [row("A001")])
    runner.make_drafts(db)

    dialog = AiDraftDialog(db, "A001")
    assert dialog.codes.count() == 1 and dialog.names.count() == 3
    assert "(금지어·50자 초과" not in dialog.names.item(1).text()               # 금지어 설정 없음
    assert not dialog.apply_name_btn.isEnabled()                                 # 지금 엑셀 값과 같은 후보
    dialog.names.setCurrentRow(2)
    assert dialog.apply_name_btn.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    dialog.on_apply_name()
    assert cells(excel, "상품명(최종)")["A001"] == ("베이직 데일리 반팔 티셔츠",)
    assert "베이직 데일리" in dialog.current_name.text()
    dialog.deleteLater()

    runner.run_waiting(FakeServices(db, FakeClient()), dry_run=False)
    assert pending_review(db)[0].ai_draft
    page = ReviewPage(FakeServices(db, FakeClient()))
    assert not page.ai_btn.isEnabled()
    page.table.setCurrentCell(0, 3)
    assert page.ai_btn.isEnabled()
    dialog = AiDraftDialog(db, "A001")
    assert not dialog.apply_name_btn.isEnabled() and "이미 등록된 상품" in dialog.meta.text()
    dialog.deleteLater()
    page.deleteLater()
    app.processEvents()
