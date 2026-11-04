"""SQLite 로컬 저장소 (%LOCALAPPDATA%\\SmartStoreApp\\app.db). 시크릿은 저장하지 않는다."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from api.categories import Category

APP_DIR_NAME = "SmartStoreApp"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS categories (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    whole_name TEXT NOT NULL,
    last       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_categories_whole_name ON categories(whole_name);
CREATE TABLE IF NOT EXISTS products (
    origin_product_no  INTEGER PRIMARY KEY,
    channel_product_no INTEGER,
    name               TEXT NOT NULL,
    sale_price         INTEGER NOT NULL,
    stock_quantity     INTEGER NOT NULL,
    status             TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    data       TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(kind, name)
);
CREATE TABLE IF NOT EXISTS drafts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
-- 자동화: 분류 매핑 (data는 CategoryMapping JSON)
CREATE TABLE IF NOT EXISTS category_mappings (
    name       TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
-- 자동화: 실행 내역
CREATE TABLE IF NOT EXISTS run_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    code       TEXT,
    action     TEXT NOT NULL,
    summary    TEXT,
    result     TEXT NOT NULL,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_log_run ON run_log(run_id);
-- 자동화: 상품코드별 처리 상태 (엑셀에 못 쓴 결과는 excel_synced=0으로 남겨 다음 실행 때 반영)
CREATE TABLE IF NOT EXISTS automation_items (
    code              TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    origin_product_no INTEGER,
    message           TEXT,
    excel_synced      INTEGER NOT NULL DEFAULT 1,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
-- 자동화: 촬영 사진 분리 기록 (동기화 폴더 사진은 복사만 하고 원본은 그대로 둔다)
CREATE TABLE IF NOT EXISTS sorted_photos (
    source      TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    captured_at TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- qr | product | unsorted
    code        TEXT,
    target      TEXT,
    sorted_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (source, size, mtime)
);
-- 자동화: AI 초안 (Claude가 만든 결과. 같은 상품에 API를 다시 부르지 않도록 보관)
CREATE TABLE IF NOT EXISTS ai_drafts (
    code        TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,          -- JSON: 상품명 후보·특징·태그·색상
    filled_name TEXT,                   -- 엑셀 상품명(최종)에 채운 값 (사람이 고치면 달라짐)
    filled_desc TEXT,                   -- 엑셀 설명(최종)에 채운 값
    model       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def app_data_dir() -> Path:
    # SMARTSTORE_DATA_DIR: 테스트·빌드 검증 시 실제 데이터와 분리할 때 사용
    override = os.environ.get("SMARTSTORE_DATA_DIR")
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    path = Path(override) if override else Path(base) / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


class Database:
    def __init__(self, path: Path | str | None = None):
        self.path = str(path or app_data_dir() / "app.db")
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """예전 DB에 없는 열을 추가한다 (데이터는 그대로)."""
        added = {"automation_items": [("name", "TEXT"), ("sale_price", "INTEGER"), ("ai_draft", "INTEGER NOT NULL DEFAULT 0"),
                                      ("registered_at", "TEXT")]}
        for table, columns in added.items():
            existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns:
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def close(self) -> None:
        self._conn.close()

    # --- 설정 (비밀 아닌 값만) ---
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- 등록한 상품 기록 ---
    def add_product(self, origin_no: int, channel_no: int | None, name: str,
                    sale_price: int, stock: int, status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO products(origin_product_no, channel_product_no, name, "
                "sale_price, stock_quantity, status) VALUES(?, ?, ?, ?, ?, ?)",
                (origin_no, channel_no, name, sale_price, stock, status),
            )

    def update_product(self, origin_no: int, sale_price: int | None = None, stock: int | None = None,
                       status: str | None = None) -> None:
        """앱에서 등록한 상품 기록이 있으면 변경값을 반영한다(없으면 무시)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE products SET sale_price = COALESCE(?, sale_price), "
                "stock_quantity = COALESCE(?, stock_quantity), status = COALESCE(?, status) "
                "WHERE origin_product_no = ?",
                (sale_price, stock, status, origin_no),
            )

    def list_products(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM products ORDER BY created_at DESC").fetchall()

    # --- 템플릿 (data는 JSON) ---
    def list_templates(self, kind: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM templates WHERE kind = ? ORDER BY is_default DESC, name", (kind,)).fetchall()

    def save_template(self, kind: str, name: str, data: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO templates(kind, name, data) VALUES(?, ?, ?) "
                "ON CONFLICT(kind, name) DO UPDATE SET data = excluded.data, "
                "updated_at = datetime('now', 'localtime')",
                (kind, name, json.dumps(data, ensure_ascii=False)),
            )

    def delete_template(self, template_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))

    def set_default_template(self, kind: str, template_id: int | None) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE templates SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END WHERE kind = ?", (template_id, kind))

    # --- 임시저장 (data는 ProductDraft JSON) ---
    def list_drafts(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM drafts ORDER BY updated_at DESC, id DESC").fetchall()

    def get_draft(self, draft_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()

    def save_draft(self, draft_id: int | None, name: str, data: dict) -> int:
        payload = json.dumps(data, ensure_ascii=False)
        with self._lock, self._conn:
            if draft_id is not None:
                cur = self._conn.execute(
                    "UPDATE drafts SET name = ?, data = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
                    (name, payload, draft_id))
                if cur.rowcount:
                    return draft_id
            return self._conn.execute("INSERT INTO drafts(name, data) VALUES(?, ?)", (name, payload)).lastrowid

    def delete_draft(self, draft_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))

    # --- 자동화: 분류 매핑 ---
    def list_mappings(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM category_mappings ORDER BY name").fetchall()
        return [json.loads(r["data"]) for r in rows]

    def replace_mappings(self, mappings: list[dict]) -> None:
        """화면에서 편집한 표 전체로 교체한다."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM category_mappings")
            self._conn.executemany(
                "INSERT INTO category_mappings(name, data) VALUES(?, ?)",
                [(m["name"], json.dumps(m, ensure_ascii=False)) for m in mappings],
            )

    # --- 자동화: 실행 내역 ---
    def add_run_log(self, run_id: str, action: str, result: str, code: str | None = None,
                    summary: str | None = None, error: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO run_log(run_id, code, action, summary, result, error) VALUES(?, ?, ?, ?, ?, ?)",
                (run_id, code, action, summary, result, error),
            )

    def count_registered_today(self) -> int:
        """오늘 자동화로 등록에 성공한 건수 (하루 최대 등록 수 확인용)."""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM run_log WHERE action = 'register' AND result = '성공' "
                "AND date(created_at) = date('now', 'localtime')").fetchone()[0]

    def list_run_log(self, run_id: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
        with self._lock:
            if run_id:
                return self._conn.execute("SELECT * FROM run_log WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
            return self._conn.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # --- 자동화: 상품코드별 상태 ---
    def get_automation_item(self, code: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM automation_items WHERE code = ?", (code,)).fetchone()

    def update_automation_display(self, code: str, name: str, sale_price: int) -> None:
        """검수 화면에서 상품을 고친 뒤 목록에 보이는 상품명·판매가를 맞춘다 (상태·엑셀 동기화는 그대로)."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE automation_items SET name = ?, sale_price = ?, "
                               "updated_at = datetime('now', 'localtime') WHERE code = ?", (name, sale_price, code))

    def upsert_automation_item(self, code: str, status: str, origin_product_no: int | None = None,
                               message: str | None = None, excel_synced: bool = True, name: str | None = None,
                               sale_price: int | None = None, ai_draft: bool | None = None,
                               registered: bool = False) -> None:
        """name·sale_price·ai_draft는 주어질 때만 바꾼다. registered=True면 등록 시각을 기록한다."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO automation_items(code, status, origin_product_no, message, excel_synced, name, sale_price, "
                "ai_draft, registered_at) VALUES(?, ?, ?, ?, ?, ?, ?, COALESCE(?, 0), "
                "CASE WHEN ? THEN datetime('now', 'localtime') END) "
                "ON CONFLICT(code) DO UPDATE SET status = excluded.status, "
                "origin_product_no = COALESCE(excluded.origin_product_no, origin_product_no), "
                "message = excluded.message, excel_synced = excluded.excel_synced, "
                "name = COALESCE(excluded.name, name), sale_price = COALESCE(excluded.sale_price, sale_price), "
                "ai_draft = CASE WHEN ? IS NULL THEN ai_draft ELSE excluded.ai_draft END, "
                "registered_at = COALESCE(excluded.registered_at, registered_at), "
                "updated_at = datetime('now', 'localtime')",
                (code, status, origin_product_no, message, int(excel_synced), name, sale_price,
                 None if ai_draft is None else int(ai_draft), int(registered), ai_draft),
            )

    def automation_items_by_status(self, status: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM automation_items WHERE status = ? ORDER BY registered_at DESC, "
                                      "code", (status,)).fetchall()

    def remove_automation_item(self, code: str) -> None:
        """자동화 기록을 지운다 (삭제 내역에서 치울 때). 스토어·엑셀에는 영향 없음."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM automation_items WHERE code = ?", (code,))

    def unsynced_automation_items(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM automation_items WHERE excel_synced = 0").fetchall()

    # --- 자동화: 촬영 사진 분리 ---
    def sorted_photo_keys(self) -> set[tuple[str, int, float]]:
        with self._lock:
            return {(r["source"], r["size"], r["mtime"])
                    for r in self._conn.execute("SELECT source, size, mtime FROM sorted_photos")}

    def add_sorted_photo(self, source: str, size: int, mtime: float, captured_at: str, kind: str,
                         code: str | None, target: str | None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO sorted_photos(source, size, mtime, captured_at, kind, code, target) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)", (source, size, mtime, captured_at, kind, code, target))

    def qr_card_timeline(self) -> list[tuple[str, str]]:
        """지금까지 찍힌 QR 카드 (촬영 시각, 상품코드), 시각순."""
        with self._lock:
            return [(r["captured_at"], r["code"]) for r in self._conn.execute(
                "SELECT captured_at, code FROM sorted_photos WHERE kind = 'qr' ORDER BY captured_at")]

    # --- AI 초안 ---
    def get_ai_draft(self, code: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM ai_drafts WHERE code = ?", (code,)).fetchone()

    def list_ai_drafts(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM ai_drafts ORDER BY created_at DESC, code").fetchall()

    def save_ai_draft(self, code: str, payload: str, model: str) -> None:
        """새 초안을 저장한다 (다시 만들면 이전에 채운 값 기록은 지운다)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO ai_drafts(code, payload, model, filled_name, filled_desc) "
                "VALUES(?, ?, ?, NULL, NULL)", (code, payload, model))

    def set_ai_filled(self, code: str, name: str | None, desc: str | None) -> None:
        """엑셀에 채운 값을 기록한다. None인 칸은 기존 기록을 유지한다."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE ai_drafts SET filled_name = COALESCE(?, filled_name), "
                "filled_desc = COALESCE(?, filled_desc) WHERE code = ?", (name, desc, code))

    # --- 카테고리 캐시 ---
    def get_category(self, category_id: str) -> Category | None:
        with self._lock:
            r = self._conn.execute("SELECT id, name, whole_name, last FROM categories WHERE id = ?",
                                   (category_id,)).fetchone()
        return Category(r["id"], r["name"], r["whole_name"], bool(r["last"])) if r else None

    def replace_categories(self, categories: list[Category]) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM categories")
            self._conn.executemany(
                "INSERT INTO categories(id, name, whole_name, last) VALUES(?, ?, ?, ?)",
                [(c.id, c.name, c.whole_name, int(c.last)) for c in categories],
            )

    def category_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]

    def search_categories(self, keyword: str, leaf_only: bool = True, limit: int = 500) -> list[Category]:
        """공백으로 구분한 모든 단어가 전체 카테고리명 또는 카테고리 ID에 포함된 항목을 찾는다.
        ID와 정확히 일치하는 항목을 맨 위에 둔다."""
        words = [w for w in keyword.split() if w]
        sql = "SELECT id, name, whole_name, last FROM categories WHERE 1=1"
        args: list = []
        for w in words:
            pattern = "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            sql += " AND (whole_name LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')"
            args += [pattern, pattern]
        if leaf_only:
            sql += " AND last = 1"
        exact = ",".join("?" * len(words)) or "NULL"
        sql += f" ORDER BY CASE WHEN id IN ({exact}) THEN 0 ELSE 1 END, whole_name LIMIT ?"
        args += words + [limit]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [Category(r["id"], r["name"], r["whole_name"], bool(r["last"])) for r in rows]
