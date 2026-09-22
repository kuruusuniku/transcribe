from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import httpx
from notion_client import Client

from .config import NotionConfig

logger = logging.getLogger(__name__)

_RICH_TEXT_LIMIT = 2000
_TAG_NAME_LIMIT = 100  # Notion の multi_select オプション名の上限
_BLOCKS_PER_REQUEST = 100
_NOTION_503_RETRY_BACKOFFS_SEC = (5, 10, 20)


def _split_text(text: str, limit: int) -> list[str]:
    if not text:
        return [""]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _parse_rich_text(text: str) -> list[dict]:
    parts: list[dict] = []
    segments = re.split(r"\*\*(.+?)\*\*", text)
    for i, seg in enumerate(segments):
        bold = i % 2 == 1
        if not seg:
            continue
        for chunk in _split_text(seg, _RICH_TEXT_LIMIT):
            part: dict = {"type": "text", "text": {"content": chunk}}
            if bold:
                part["annotations"] = {"bold": True}
            parts.append(part)
    return parts or [{"type": "text", "text": {"content": ""}}]


def _md_line_to_block(line: str) -> dict | None:
    if line.startswith("# "):
        return {"type": "heading_1", "heading_1": {"rich_text": _parse_rich_text(line[2:])}}
    if line.startswith("## "):
        return {"type": "heading_2", "heading_2": {"rich_text": _parse_rich_text(line[3:])}}
    if line.startswith("### "):
        return {"type": "heading_3", "heading_3": {"rich_text": _parse_rich_text(line[4:])}}
    if line.startswith("- ") or line.startswith("* "):
        return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _parse_rich_text(line[2:])}}
    m = re.match(r"^\d+\. (.+)", line)
    if m:
        return {"type": "numbered_list_item", "numbered_list_item": {"rich_text": _parse_rich_text(m.group(1))}}
    if not line.strip():
        return None
    return {"type": "paragraph", "paragraph": {"rich_text": _parse_rich_text(line)}}


_TABLE_SEPARATOR_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# Notion の配列要素上限（100）に収めるため、ヘッダー行を含めた 1 テーブルあたりの行数を制限する
_TABLE_MAX_ROWS = 100


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _table_to_blocks(table_lines: list[str]) -> list[dict]:
    """Markdown テーブル行を Notion の table ブロックに変換する。区切り行がなければ段落として扱う。"""
    if len(table_lines) < 2 or not _TABLE_SEPARATOR_RE.match(table_lines[1].strip()):
        return [b for b in (_md_line_to_block(line) for line in table_lines) if b is not None]

    header = _split_table_row(table_lines[0])
    body = [_split_table_row(line) for line in table_lines[2:]]
    width = len(header)

    def _row(cells: list[str]) -> dict:
        cells = (cells + [""] * width)[:width]
        return {"type": "table_row", "table_row": {"cells": [_parse_rich_text(c) for c in cells]}}

    blocks: list[dict] = []
    chunk_size = _TABLE_MAX_ROWS - 1
    for i in range(0, max(len(body), 1), chunk_size):
        rows = [_row(header)] + [_row(r) for r in body[i : i + chunk_size]]
        blocks.append({
            "type": "table",
            "table": {
                "table_width": width,
                "has_column_header": True,
                "has_row_header": False,
                "children": rows,
            },
        })
    return blocks


def md_to_blocks(text: str) -> list[dict]:
    blocks: list[dict] = []
    lines = text.splitlines()
    in_code = False
    code_lines: list[str] = []
    code_lang = ""
    table_lines: list[str] = []

    for line in lines:
        if not in_code and line.lstrip().startswith("|"):
            table_lines.append(line)
            continue
        if table_lines:
            blocks.extend(_table_to_blocks(table_lines))
            table_lines = []

        if line.startswith("```"):
            if not in_code:
                in_code = True
                code_lang = line[3:].strip()
                code_lines = []
            else:
                in_code = False
                code_content = "\n".join(code_lines)
                for chunk in _split_text(code_content, _RICH_TEXT_LIMIT):
                    blocks.append({
                        "type": "code",
                        "code": {
                            "rich_text": [{"type": "text", "text": {"content": chunk}}],
                            "language": code_lang or "plain text",
                        },
                    })
                code_lines = []
                code_lang = ""
        elif in_code:
            code_lines.append(line)
        else:
            block = _md_line_to_block(line)
            if block is not None:
                blocks.append(block)

    if table_lines:
        blocks.extend(_table_to_blocks(table_lines))

    if in_code and code_lines:
        code_content = "\n".join(code_lines)
        for chunk in _split_text(code_content, _RICH_TEXT_LIMIT):
            blocks.append({
                "type": "code",
                "code": {
                    "rich_text": [{"type": "text", "text": {"content": chunk}}],
                    "language": code_lang or "plain text",
                },
            })

    return blocks


def _read_meta(output_dir: Path) -> dict:
    meta_path = output_dir / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


_NOTION_STATE_FILE = "notion.json"


def _read_saved_page_id(output_dir: Path, database_id: str) -> str | None:
    """前回同期時に保存したページ ID を返す（同期先 DB が同じ場合のみ）。"""
    path = output_dir / _NOTION_STATE_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("database_id") != database_id:
        return None
    return data.get("page_id")


def _save_page_id(output_dir: Path, database_id: str, page_id: str) -> None:
    path = output_dir / _NOTION_STATE_FILE
    path.write_text(
        json.dumps({"database_id": database_id, "page_id": page_id}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _page_is_alive(client, page_id: str) -> bool:
    try:
        page = client.pages.retrieve(page_id=page_id)
    except Exception as e:
        logger.info(f"保存済み Notion ページを取得できません（新規作成します）: {e}")
        return False
    return not (page.get("archived") or page.get("in_trash"))


def _extract_tags_from_summary(summary_text: str) -> list[str]:
    """summary.md の主要キーワード・メソッド種別・指導対象の身体部位 セクションからタグを抽出する。"""
    TARGET_SECTIONS = {"### 主要キーワード", "### メソッド種別", "### 指導対象の身体部位"}
    tags: list[str] = []
    seen: set[str] = set()
    in_section = False
    for line in summary_text.splitlines():
        if line.strip() in TARGET_SECTIONS:
            in_section = True
            continue
        if in_section:
            if line.startswith("#"):
                in_section = False
                continue
            m = re.match(r"^[*\-]\s+(.+)", line.strip())
            if m:
                tag = m.group(1).strip().replace(",", "・")[:_TAG_NAME_LIMIT]
                if tag and tag not in seen:
                    seen.add(tag)
                    tags.append(tag)
    return tags


def _build_properties(
    title: str,
    date_str: str | None,
    video_url: str,
    duration_min: int | None,
    source_type: str,
    tags: list[str] | None = None,
) -> dict:
    props: dict = {
        "名前": {"title": [{"text": {"content": title[:_RICH_TEXT_LIMIT]}}]},
        "ソース種別": {"select": {"name": source_type}},
        "まとめ進捗": {"checkbox": True},
        "タグ": {"multi_select": [{"name": t} for t in (tags or [])]},
    }
    if date_str and date_str != "不明":
        props["日付"] = {"date": {"start": date_str}}
    if video_url.startswith(("http://", "https://")):
        props["URL"] = {"url": video_url}
    if duration_min is not None:
        props["動画時間"] = {"number": duration_min}
    return props


# 収録時間を入れるプロパティ名の候補（体育動画まとめDB は「動画時間」、叡智まとめDB は「音声時間」）
_DURATION_PROPERTIES = ("動画時間", "音声時間")

_schema_cache: dict[str, dict[str, str]] = {}


def _get_db_schema(client, database_id: str) -> dict[str, str] | None:
    """DB のプロパティ名 → 型 を返す。取得できない場合は None（そのまま送信する）。"""
    if database_id in _schema_cache:
        return _schema_cache[database_id]
    try:
        db = client.databases.retrieve(database_id=database_id)
        props = db.get("properties") if isinstance(db, dict) else None
        if not isinstance(props, dict):
            return None
        schema = {name: p.get("type", "") for name, p in props.items()}
    except Exception as e:
        logger.warning(f"Notion DB のプロパティ構成を取得できません（そのまま送信します）: {e}")
        return None
    _schema_cache[database_id] = schema
    return schema


def _fit_properties_to_schema(props: dict, schema: dict[str, str] | None) -> dict:
    """DB に存在しないプロパティを除き、収録時間のプロパティ名を DB に合わせる。

    DB ごとにプロパティ構成が違っても（URL・ソース種別がない等）同期できるようにする。
    """
    if schema is None:
        return props
    fitted = dict(props)
    duration = next((fitted.pop(k) for k in _DURATION_PROPERTIES if k in fitted), None)
    if duration is not None:
        target = next((k for k in _DURATION_PROPERTIES if schema.get(k) == "number"), None)
        if target:
            fitted[target] = duration
    dropped = [k for k in fitted if k not in schema]
    for k in dropped:
        fitted.pop(k)
    if dropped:
        logger.debug(f"Notion DB に存在しないプロパティを除外: {dropped}")
    return fitted


def _duration_minutes(meta: dict, output_dir: Path) -> int | None:
    """収録時間（分）。meta.json になければ segments.json の最終セグメントから求める。"""
    if meta.get("duration_minutes"):
        return int(meta["duration_minutes"])
    seg_path = output_dir / "segments.json"
    try:
        segments = json.loads(seg_path.read_text(encoding="utf-8")).get("segments", [])
    except Exception:
        return None
    if not segments:
        return None
    return round(segments[-1].get("end", 0) / 60)


_TITLE_DATE_RE = re.compile(r"^(\d{2}|\d{4})(\d{2})(\d{2})(?!\d)")


def _date_from_title(title: str) -> str | None:
    """「250430 講義名」「20250430_講義.mp3」のような先頭の日付を YYYY-MM-DD にする。"""
    m = _TITLE_DATE_RE.match(title or "")
    if not m:
        return None
    year, month, day = m.groups()
    if len(year) == 2:
        year = f"20{year}"
    if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
        return None
    return f"{year}-{month}-{day}"


def _list_child_block_ids(client, page_id: str) -> list[str]:
    """ページ直下のブロック ID を全件取得する（削除前に一覧を確定させ、ページング中の削除でカーソルがずれるのを防ぐ）。"""
    ids: list[str] = []
    start_cursor = None
    while True:
        kwargs: dict = {"block_id": page_id}
        if start_cursor:
            kwargs["start_cursor"] = start_cursor
        result = client.blocks.children.list(**kwargs)
        ids.extend(block["id"] for block in result.get("results", []))
        if not result.get("has_more", False):
            return ids
        start_cursor = result.get("next_cursor")


def _delete_blocks(client, block_ids: list[str]) -> None:
    for block_id in block_ids:
        try:
            client.blocks.delete(block_id=block_id)
        except Exception as e:
            logger.warning(f"ブロック削除失敗 (id={block_id}): {e}")


def _append_blocks_in_batches(client, page_id: str, blocks: list[dict]) -> list[str]:
    """ブロックを 100 件ずつ追加し、追加されたブロック ID を返す。

    途中で失敗した場合はそれまでに追加したブロックを削除してから例外を送出する。
    """
    appended: list[str] = []
    for i in range(0, len(blocks), _BLOCKS_PER_REQUEST):
        try:
            resp = client.blocks.children.append(block_id=page_id, children=blocks[i : i + _BLOCKS_PER_REQUEST])
        except Exception:
            end = min(i + _BLOCKS_PER_REQUEST, len(blocks)) - 1
            logger.error(
                f"blocks append 失敗 (page_id={page_id}, batch={i // _BLOCKS_PER_REQUEST + 1},"
                f" blocks {i}–{end}/{len(blocks)})"
            )
            _delete_blocks(client, appended)
            raise
        appended.extend(block["id"] for block in resp.get("results", []))
    return appended


def _replace_page_body(client, page_id: str, blocks: list[dict]) -> None:
    """本文を置き換える。新しいブロックを先に追加し、成功してから旧ブロックを削除する。

    追加に失敗した場合は旧本文がそのまま残る（空・中途半端なページにならない）。
    """
    old_ids = _list_child_block_ids(client, page_id)
    if blocks:
        _append_blocks_in_batches(client, page_id, blocks)
    _delete_blocks(client, old_ids)


def _query_notion_db(cfg: NotionConfig, video_url: str, database_id: str) -> list[dict]:
    """URL で既存ページを検索する。503 は自動リトライ。"""
    backoffs = _NOTION_503_RETRY_BACKOFFS_SEC
    max_retries = len(backoffs)
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.post(
                f"https://api.notion.com/v1/databases/{database_id}/query",
                headers={
                    "Authorization": f"Bearer {cfg.token}",
                    "Notion-Version": "2022-06-28",
                    "Content-Type": "application/json",
                },
                json={"filter": {"property": "URL", "url": {"equals": video_url}}},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json().get("results", [])
        except Exception as e:
            is_503 = isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 503
            if not is_503 or attempt >= max_retries:
                raise
            wait = backoffs[attempt]
            logger.warning(f"Notion API 503, retry {attempt + 1}/{max_retries} (wait {wait}s)")
            time.sleep(wait)
    raise RuntimeError("到達不能: 503 リトライループが想定外に終了しました")


def sync_to_notion(output_dir: Path, video_url: str, cfg: NotionConfig, source_type: str = "youtube") -> bool:
    summary_path = output_dir / "summary.md"
    if not summary_path.exists():
        logger.info(f"summary.md が見つかりません。Notion 同期スキップ: {output_dir}")
        return False

    # source_type に応じて同期先 DB を決定
    if source_type == "local" and cfg.local_database_id:
        target_db_id = cfg.local_database_id
    else:
        target_db_id = cfg.database_id

    if not target_db_id:
        logger.warning(f"同期先の database_id が未設定です (source_type={source_type}): {output_dir}")
        return False

    summary_text = summary_path.read_text(encoding="utf-8")
    meta = _read_meta(output_dir)

    title = meta.get("title") or output_dir.stem
    if source_type == "local":
        # 録音ファイルはファイル名がタイトルになるため拡張子を落とす
        title = Path(title).stem
    recording_date = meta.get("recording_date")
    date_str = recording_date if recording_date and recording_date != "不明" else None
    if date_str is None and source_type == "local":
        # 録音ファイルは公開日がないため、ファイル名先頭の日付（例: 250430_講義.mp3）を使う
        date_str = _date_from_title(title)
    duration_min = _duration_minutes(meta, output_dir)

    client = Client(auth=cfg.token)
    tags = _extract_tags_from_summary(summary_text)
    props = _build_properties(title, date_str, video_url, duration_min, source_type, tags)
    props = _fit_properties_to_schema(props, _get_db_schema(client, target_db_id))
    blocks = md_to_blocks(summary_text)

    existing_page_id: str | None = None
    if video_url.startswith(("http://", "https://")):
        try:
            pages = _query_notion_db(cfg, video_url, target_db_id)
            if pages:
                existing_page_id = pages[0]["id"]
        except Exception as e:
            logger.warning(f"Notion 既存ページ検索失敗: {e}")

    # URL で特定できない場合（ローカルファイル等）は前回作成したページを再利用する
    if existing_page_id is None:
        saved_id = _read_saved_page_id(output_dir, target_db_id)
        if saved_id and _page_is_alive(client, saved_id):
            existing_page_id = saved_id

    if existing_page_id:
        try:
            _replace_page_body(client, existing_page_id, blocks)
        except Exception:
            logger.error(
                f"Notion ページ本文の更新に失敗しました（旧本文を保持）: "
                f"https://notion.so/{existing_page_id.replace('-', '')}"
            )
            raise
        client.pages.update(page_id=existing_page_id, properties=props)
        logger.info(f"Notion ページを更新しました: {title} (id={existing_page_id})")
    else:
        first_batch = blocks[:_BLOCKS_PER_REQUEST]
        page = client.pages.create(
            parent={"database_id": target_db_id},
            properties=props,
            children=first_batch,
        )
        page_id = page["id"]
        if len(blocks) > _BLOCKS_PER_REQUEST:
            try:
                _append_blocks_in_batches(client, page_id, blocks[_BLOCKS_PER_REQUEST:])
            except Exception:
                client.pages.update(page_id=page_id, archived=True)
                raise
        logger.info(f"Notion ページを作成しました: {title} (id={page_id})")
        existing_page_id = page_id

    try:
        _save_page_id(output_dir, target_db_id, existing_page_id)
    except OSError as e:
        logger.warning(f"Notion ページ ID の保存に失敗しました: {e}")

    return True
