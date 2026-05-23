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


def md_to_blocks(text: str) -> list[dict]:
    blocks: list[dict] = []
    lines = text.splitlines()
    in_code = False
    code_lines: list[str] = []
    code_lang = ""

    for line in lines:
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


def _build_properties(
    title: str,
    date_str: str | None,
    video_url: str,
    duration_min: int | None,
    source_type: str,
) -> dict:
    props: dict = {
        "名前": {"title": [{"text": {"content": title[:_RICH_TEXT_LIMIT]}}]},
        "ソース種別": {"select": {"name": source_type}},
        "まとめ進捗": {"checkbox": True},
        "タグ": {"multi_select": []},
    }
    if date_str and date_str != "不明":
        props["日付"] = {"date": {"start": date_str}}
    if video_url.startswith(("http://", "https://")):
        props["URL"] = {"url": video_url}
    if duration_min is not None:
        props["動画時間"] = {"number": duration_min}
    return props


def _clear_page_body(client, page_id: str) -> None:
    has_more = True
    start_cursor = None
    while has_more:
        kwargs: dict = {"block_id": page_id}
        if start_cursor:
            kwargs["start_cursor"] = start_cursor
        result = client.blocks.children.list(**kwargs)
        for block in result.get("results", []):
            try:
                client.blocks.delete(block_id=block["id"])
            except Exception as e:
                logger.warning(f"ブロック削除失敗 (id={block['id']}): {e}")
        has_more = result.get("has_more", False)
        start_cursor = result.get("next_cursor")


def _append_blocks_in_batches(client, page_id: str, blocks: list[dict]) -> None:
    for i in range(0, len(blocks), _BLOCKS_PER_REQUEST):
        client.blocks.children.append(block_id=page_id, children=blocks[i : i + _BLOCKS_PER_REQUEST])


def _query_notion_db(cfg: NotionConfig, video_url: str) -> list[dict]:
    """URL で既存ページを検索する。503 は自動リトライ。"""
    backoffs = _NOTION_503_RETRY_BACKOFFS_SEC
    max_retries = len(backoffs)
    resp = None
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.post(
                f"https://api.notion.com/v1/databases/{cfg.database_id}/query",
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
            is_503 = getattr(e, "status_code", None) == 503 or (
                hasattr(resp, "status_code") and resp.status_code == 503
            )
            if not is_503 or attempt >= max_retries:
                raise
            wait = backoffs[attempt]
            logger.warning(f"Notion API 503, retry {attempt + 1}/{max_retries} (wait {wait}s)")
            time.sleep(wait)
    raise RuntimeError("到達不能: 503 リトライループが想定外に終了しました")


def sync_to_notion(output_dir: Path, video_url: str, cfg: NotionConfig) -> bool:
    summary_path = output_dir / "summary.md"
    if not summary_path.exists():
        logger.info(f"summary.md が見つかりません。Notion 同期スキップ: {output_dir}")
        return False

    summary_text = summary_path.read_text(encoding="utf-8")
    meta = _read_meta(output_dir)

    title = meta.get("title") or output_dir.stem
    recording_date = meta.get("recording_date")
    date_str = recording_date if recording_date and recording_date != "不明" else None
    source_type = meta.get("source_type", "youtube")
    duration_min = meta.get("duration_minutes")

    client = Client(auth=cfg.token)
    props = _build_properties(title, date_str, video_url, duration_min, source_type)
    blocks = md_to_blocks(summary_text)

    existing_page_id: str | None = None
    if video_url.startswith(("http://", "https://")):
        try:
            pages = _query_notion_db(cfg, video_url)
            if pages:
                existing_page_id = pages[0]["id"]
        except Exception as e:
            logger.warning(f"Notion 既存ページ検索失敗: {e}")

    if existing_page_id:
        client.pages.update(page_id=existing_page_id, properties=props)
        _clear_page_body(client, existing_page_id)
        if blocks:
            _append_blocks_in_batches(client, existing_page_id, blocks)
        logger.info(f"Notion ページを更新しました: {title} (id={existing_page_id})")
    else:
        first_batch = blocks[:_BLOCKS_PER_REQUEST]
        page = client.pages.create(
            parent={"database_id": cfg.database_id},
            properties=props,
            children=first_batch,
        )
        page_id = page["id"]
        if len(blocks) > _BLOCKS_PER_REQUEST:
            _append_blocks_in_batches(client, page_id, blocks[_BLOCKS_PER_REQUEST:])
        logger.info(f"Notion ページを作成しました: {title} (id={page_id})")

    return True
