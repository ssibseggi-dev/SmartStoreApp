"""AI 초안 — 보정 사진 2~4장과 작업 엑셀 입력값을 Claude API에 보내 상품명 후보·특징·검색 태그·색상을 JSON으로 받는다.

규칙
- 엑셀 상품명(최종)·설명(최종)이 비어 있을 때만 채운다. 사람이 입력한 값은 절대 덮어쓰지 않는다.
- 사진·입력값으로 확인할 수 없는 사실(소재, 효능, 원산지 등)은 지어내지 않도록 프롬프트에 명시한다.
- Claude API 키는 keyring에만 저장한다. 키가 없으면 이 단계를 건너뛴다.
- 같은 상품에 API를 다시 부르지 않도록 결과를 DB(ai_drafts)에 보관한다 (다시 만들기는 force).
"""
from __future__ import annotations

import base64
import io
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

import keyring
from PIL import Image, ImageOps

from services.automation.input_sheet import STATUS_FAILED, STATUS_WAITING, ResultUpdate, read_items, write_results
from services.automation.naming import find_banned
from services.images import ImageSet
from services.images.processing import register_heif

log = logging.getLogger(__name__)

SERVICE = "smartstore_app"
_KEY_NAME = "claude_api_key"
DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"   # 안전 분류기가 거절하면 서버가 다른 모델로 이어서 답함
NAME_LIMIT = 50
MAX_TAGS = 10
MAX_IMAGES = 4
_IMAGE_SIDE = 1024           # 사진은 이 크기로 줄여 보낸다 (비용·속도)
AI_MARK = "AI 초안 - 검수 필요"

# 엑셀에서 프롬프트에 넣는 칸 (사람이 적은 사실만 근거로 쓰게 한다)
_INPUT_FIELDS = ["원본상품명", "분류", "브랜드", "키워드", "옵션", "실측", "소재", "세탁"]

_SCHEMA = {
    "type": "object",
    "properties": {
        "names": {"type": "array", "items": {"type": "string"},
                  "description": "상품명 후보 3개"},
        "features": {"type": "array", "items": {"type": "string"},
                     "description": "상세페이지 특징 문장 3~5줄"},
        "tags": {"type": "array", "items": {"type": "string"},
                 "description": "검색 태그 최대 10개"},
        "colors": {"type": "array", "items": {"type": "string"},
                   "description": "사진에서 보이는 상품 색상"},
    },
    "required": ["names", "features", "tags", "colors"],
    "additionalProperties": False,
}

_SYSTEM = """당신은 네이버 스마트스토어 의류·잡화 상품 등록을 돕는 카피라이터입니다.
판매자가 보낸 상품 사진과 입력값만 근거로 등록 초안을 만듭니다. 판매자가 검수한 뒤 등록하므로, 확인된 사실만 담백하게 씁니다.

상품명 후보 3개
- 네이버 상품명 가이드를 따릅니다: [브랜드] + 상품 종류·핵심 특징(핏, 디자인, 색상 등) + 상품명 순서로, 검색에 쓰이는 일반 명사 위주.
- 각 50자 이내, 공백 포함. 특수문자·이모지·홍보 문구(무료배송, 할인, 특가, 최저가, 1위, 정품, 인기 등)는 넣지 않습니다.
- 같은 단어를 반복하지 않습니다. 브랜드가 입력되지 않았으면 브랜드를 지어내지 않습니다.
- 세 후보는 서로 다른 강조점(핏/디자인/용도 등)을 갖게 합니다. 가장 무난한 후보를 첫 번째에 둡니다.

특징 3~5줄
- 상세페이지 설명에 그대로 들어갈 한 문장씩, 각 40자 안팎. 존댓말(~입니다, ~해요 체 중 하나로 통일).
- 사진에서 보이는 디자인·형태·디테일, 그리고 입력값(소재, 세탁, 실측, 옵션)에 적힌 내용만 씁니다.

검색 태그 최대 10개
- 고객이 검색할 만한 짧은 명사구 (예: 린넨셔츠, 오버핏셔츠). 띄어쓰기·# 없이, 상품명과 겹치지 않는 표현 위주.

색상
- 사진에서 보이는 상품 색상을 한국어 색 이름으로 (예: 네이비, 아이보리). 옵션에 색상이 있으면 그 이름을 따릅니다.

지어내면 안 되는 것
- 사진과 입력값으로 확인할 수 없는 사실: 소재·혼용률, 두께, 신축성, 원산지, 제조사, 기능·효능(보온, 냉감, 항균 등), 인증, 수상, 판매량.
- 입력값에 소재가 없으면 소재를 추측하지 말고 소재 언급을 빼세요."""


class AiDraftError(Exception):
    pass


@dataclass
class AiDraft:
    names: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    colors: list[str] = field(default_factory=list)
    model: str = ""

    @property
    def description(self) -> str:
        return "\n".join(self.features)

    def usable_names(self, banned_words: list[str]) -> list[str]:
        """금지어가 없고 50자 이내인 후보."""
        return [n for n in self.names if len(n) <= NAME_LIMIT and not find_banned(n, banned_words)]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "AiDraft":
        data = json.loads(text)
        return cls(names=data.get("names", []), features=data.get("features", []), tags=data.get("tags", []),
                   colors=data.get("colors", []), model=data.get("model", ""))


# --- API 키 (keyring) ---
def get_api_key() -> str:
    return keyring.get_password(SERVICE, _KEY_NAME) or ""


def has_api_key() -> bool:
    return bool(get_api_key())


def save_api_key(key: str) -> None:
    if key.strip():
        keyring.set_password(SERVICE, _KEY_NAME, key.strip())


def delete_api_key() -> None:
    try:
        keyring.delete_password(SERVICE, _KEY_NAME)
    except keyring.errors.PasswordDeleteError:
        pass


def _client(api_key: str | None = None):
    import anthropic
    key = api_key or get_api_key()
    if not key:
        raise AiDraftError("Claude API 키가 없습니다. 자동화 설정에서 키를 저장하세요.")
    return anthropic.Anthropic(api_key=key, max_retries=3, timeout=180)


def check_api_key(api_key: str | None = None, model: str = DEFAULT_MODEL, client=None) -> str:
    """키로 모델 정보를 조회해 본다 (토큰을 쓰지 않음). 성공하면 모델 표시 이름."""
    import anthropic
    try:
        info = (client or _client(api_key)).models.retrieve(model)
    except anthropic.AuthenticationError:
        raise AiDraftError("Claude API 키가 올바르지 않습니다.") from None
    except anthropic.NotFoundError:
        raise AiDraftError(f"모델 '{model}'을(를) 쓸 수 없습니다.") from None
    except anthropic.APIConnectionError:
        raise AiDraftError("Claude API에 연결하지 못했습니다. 인터넷 연결을 확인하세요.") from None
    except anthropic.APIStatusError as e:
        raise AiDraftError(f"Claude API 오류 ({e.status_code})") from None
    return getattr(info, "display_name", "") or model


# --- 요청 만들기 ---
def pick_images(images: ImageSet | None) -> list[Path]:
    """대표 + 추가 사진 앞쪽, 모자라면 상세용 사진으로 최대 4장."""
    if not images or not images.found:
        return []
    return ([images.representative] + images.optional + images.detail)[:MAX_IMAGES]


def _image_block(path: Path) -> dict:
    if path.suffix.lower() in (".heic", ".heif"):
        register_heif()
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((_IMAGE_SIDE, _IMAGE_SIDE))
        buffer = io.BytesIO()
        img.save(buffer, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buffer.getvalue()).decode("ascii")}}


def build_prompt(item, banned_words: list[str]) -> str:
    lines = [f"- {name}: {item.get(name)}" for name in _INPUT_FIELDS if item.get(name)]
    text = "판매자 입력값 (이 내용과 사진만 사실로 사용):\n" + ("\n".join(lines) or "- (입력값 없음)")
    if banned_words:
        text += "\n\n상품명·특징·태그에 쓰면 안 되는 금지어: " + ", ".join(banned_words)
    return text + "\n\n사진은 같은 상품입니다. 위 규칙대로 상품명 후보 3개, 특징 3~5줄, 검색 태그, 색상을 만들어 주세요."


def _clean(values, limit: int) -> list[str]:
    out = []
    for v in values or []:
        v = " ".join(str(v).split()).strip()
        if v and v not in out:
            out.append(v)
    return out[:limit]


def generate(item, image_paths: list[Path], banned_words: list[str], model: str = DEFAULT_MODEL,
             client=None) -> AiDraft:
    """Claude API 한 번 호출. 실패하면 AiDraftError."""
    import anthropic
    if not image_paths:
        raise AiDraftError("AI 초안에 쓸 사진이 없습니다.")
    content = [_image_block(p) for p in image_paths[:MAX_IMAGES]]
    content.append({"type": "text", "text": build_prompt(item, banned_words)})
    try:
        response = (client or _client()).beta.messages.create(
            model=model,
            max_tokens=4000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            system=_SYSTEM,
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError:
        raise AiDraftError("Claude API 키가 올바르지 않습니다.") from None
    except anthropic.RateLimitError:
        raise AiDraftError("Claude API 사용 한도에 걸렸습니다. 잠시 뒤 다시 실행하세요.") from None
    except anthropic.APIConnectionError:
        raise AiDraftError("Claude API에 연결하지 못했습니다.") from None
    except anthropic.APIStatusError as e:
        if "credit balance" in str(e.message).lower():
            raise AiDraftError("Claude API 크레딧이 부족합니다. Claude Console의 Plans & Billing에서 충전하세요.") from None
        raise AiDraftError(f"Claude API 오류 ({e.status_code}): {e.message}") from None
    if response.stop_reason == "refusal":
        raise AiDraftError("Claude가 이 요청에 답하지 않았습니다.")
    if response.stop_reason == "max_tokens":
        raise AiDraftError("AI 응답이 너무 길어 잘렸습니다.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise AiDraftError("AI 응답을 읽지 못했습니다.") from None
    draft = AiDraft(names=_clean(data.get("names"), 3), features=_clean(data.get("features"), 5),
                    tags=_clean([str(t).replace("#", "").replace(" ", "") for t in data.get("tags") or []], MAX_TAGS),
                    colors=_clean(data.get("colors"), 10), model=getattr(response, "model", model) or model)
    if not draft.names and not draft.features:
        raise AiDraftError("AI 응답에 상품명·특징이 없습니다.")
    log.info("[%s] AI 초안 생성 (%s, 사진 %d장)", item.get("상품코드"), draft.model, len(image_paths))
    return draft


# --- 저장·적용 ---
def load(db, code: str) -> AiDraft | None:
    row = db.get_ai_draft(code)
    return AiDraft.from_json(row["payload"]) if row else None


def needs_draft(item) -> bool:
    return not item.get("상품명(최종)") or not item.get("설명(최종)")


def ensure(db, item, images: ImageSet | None, settings, force: bool = False, client=None) -> AiDraft:
    """보관된 초안이 있으면 그대로, 없으면(또는 force) 새로 만든다."""
    if not force:
        cached = load(db, item.code)
        if cached:
            return cached
    draft = generate(item, pick_images(images), settings.banned_words, settings.ai_model, client)
    db.save_ai_draft(item.code, draft.to_json(), draft.model)
    return draft


def fill_item(db, item, draft: AiDraft, banned_words: list[str]) -> dict[str, str]:
    """빈 칸(상품명(최종)·설명(최종))만 채운다. 채운 {칸: 값}을 돌려주고 DB에 기록한다."""
    filled: dict[str, str] = {}
    names = draft.usable_names(banned_words)
    if not item.get("상품명(최종)") and names:
        filled["상품명(최종)"] = names[0]
    if not item.get("설명(최종)") and draft.features:
        filled["설명(최종)"] = draft.description
    item.values.update(filled)
    if filled:
        db.set_ai_filled(item.code, filled.get("상품명(최종)"), filled.get("설명(최종)"))
    return filled


def is_ai_origin(db, item) -> bool:
    """엑셀의 상품명(최종)·설명(최종)이 AI가 채운 값 그대로인지 (사람이 고쳤으면 False)."""
    row = db.get_ai_draft(item.code) if item.code else None
    if not row:
        return False
    return bool((row["filled_name"] and item.get("상품명(최종)") == row["filled_name"])
                or (row["filled_desc"] and item.get("설명(최종)") == row["filled_desc"]))


# --- 저장된 초안 보기·고르기 (화면용) ---
EDITABLE_STATUSES = (STATUS_WAITING, STATUS_FAILED)   # 아직 등록 전이라 엑셀 값을 바꾸면 다음 등록에 반영됨


@dataclass
class SavedDraft:
    code: str
    draft: AiDraft
    created_at: str
    status: str = ""          # 작업 엑셀의 상태 (엑셀에 없으면 "")
    name: str = ""            # 지금 엑셀 상품명(최종)
    description: str = ""     # 지금 엑셀 설명(최종)
    in_excel: bool = False

    @property
    def editable(self) -> bool:
        return self.in_excel and self.status in EDITABLE_STATUSES


def list_saved(db, excel: Path | None) -> list[SavedDraft]:
    """보관된 AI 초안 (최근 순). 작업 엑셀을 읽을 수 있으면 지금 상태·상품명을 함께 보여준다."""
    items = {}
    if excel:
        try:
            items = {i.code: i for i in read_items(excel)}
        except (OSError, ValueError) as e:
            log.warning("AI 초안 목록: 작업 엑셀을 읽지 못함 %s", e)
    result = []
    for row in db.list_ai_drafts():
        item = items.get(row["code"])
        result.append(SavedDraft(row["code"], AiDraft.from_json(row["payload"]), row["created_at"],
                                 status=item.status if item else "", in_excel=item is not None,
                                 name=item.get("상품명(최종)") if item else "",
                                 description=item.get("설명(최종)") if item else ""))
    return result


def apply_choice(db, excel: Path, code: str, name: str | None = None, description: str | None = None) -> None:
    """사람이 화면에서 고른 후보를 작업 엑셀에 적는다 (등록 전 상품만). 엑셀이 열려 있으면 PermissionError."""
    item = next((i for i in read_items(excel) if i.code == code), None)
    if item is None:
        raise ValueError(f"작업 엑셀에 상품코드 {code}가 없습니다.")
    if item.status not in EDITABLE_STATUSES:
        raise ValueError(f"{code}는 이미 '{item.status}' 상태입니다. 등록된 상품의 상품명·설명은 스마트스토어에서 고치세요.")
    sets = {k: v for k, v in (("상품명(최종)", name), ("설명(최종)", description)) if v}
    if not sets:
        return
    write_results(excel, {code: ResultUpdate(None, row_no=item.row_no, sets=sets)})
    db.set_ai_filled(code, name or None, description or None)   # AI 후보에서 고른 값 → 계속 '검수 필요'로 표시
