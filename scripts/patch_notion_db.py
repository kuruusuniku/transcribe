"""scripts/patch_notion_db.py

DB レコードに本文ブロックが空のものを対象に、元まとめサブページの
ブロックをコピーするパッチスクリプト。

使い方:
    python scripts/patch_notion_db.py --mode copy-blocks [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import httpx
import yaml
from notion_client import Client

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.migrate_notion_pages import (
    MATOME_PAGE_ID,
    TARGET_DB_ID,
    _NOTION_BASE,
    _RETRY_BACKOFFS,
    _headers,
    _post_with_retry,
    get_subpages,
    get_page_blocks,
    extract_text_from_blocks,
    parse_url,
    NOTION_TOKEN as _NOTION_TOKEN,
)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

_BLOCKS_PER_REQUEST = 100

# ---------------------------------------------------------------------------
# グローバル状態（main() で初期化）
# ---------------------------------------------------------------------------

NOTION_TOKEN: str = ""

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------------------------

def _sanitize_rich_text(rich_text: list[dict]) -> list[dict]:
    """rich_text アイテムから読み取り専用・書き込み不可フィールドを除去する。"""
    result = []
    for rt in rich_text:
        rt = dict(rt)
        rt.pop("plain_text", None)
        rt.pop("href", None)
        # link_preview mention は API 書き込み不可 → URL テキストに変換
        if rt.get("type") == "mention":
            mention = rt.get("mention", {})
            if mention.get("type") == "link_preview":
                url = mention.get("link_preview", {}).get("url", "")
                rt = {
                    "type": "text",
                    "text": {"content": url, "link": {"url": url} if url else None},
                    "annotations": rt.get("annotations", {}),
                }
        result.append(rt)
    return result


def sanitize_block(block: dict) -> dict | None:
    """Notion API でコピー可能な形式にブロックを変換する。"""
    btype = block.get("type", "")
    if btype in ("unsupported", "child_page"):
        return None

    remove_top_keys = (
        "id", "created_time", "last_edited_time",
        "created_by", "last_edited_by",
        "has_children", "archived", "in_trash", "parent", "object",
    )
    cleaned = {k: v for k, v in block.items() if k not in remove_top_keys}

    content = cleaned.get(btype)
    if isinstance(content, dict):
        # icon: null は callout 以外では不正なフィールドになるため除去
        if content.get("icon") is None:
            content.pop("icon", None)
        # rich_text のクリーンアップ
        if "rich_text" in content:
            content["rich_text"] = _sanitize_rich_text(content["rich_text"])
        # rich_text アイテム数が100を超える場合は先頭100件に切り詰め、
        # 残りは別ブロックとして後続に追加するためメタ情報を付加する
        _RT_LIMIT = 100
        if len(content.get("rich_text", [])) > _RT_LIMIT:
            overflow = content["rich_text"][_RT_LIMIT:]
            content["rich_text"] = content["rich_text"][:_RT_LIMIT]
            cleaned["_overflow_rich_text"] = overflow

    # table ブロック: 事前に取得した行データを children として付加
    if btype == "table":
        table_children = cleaned.pop("_table_children", None)
        if table_children is not None:
            cleaned[btype]["children"] = table_children

    return cleaned


def _sanitize_table_row(row: dict) -> dict:
    """table_row ブロックをコピー可能な形式に変換する。"""
    cells = row.get("table_row", {}).get("cells", [])
    clean_cells = [_sanitize_rich_text(cell) for cell in cells]
    return {"type": "table_row", "table_row": {"cells": clean_cells}}


def fetch_and_attach_table_rows(blocks: list[dict]) -> list[dict]:
    """table ブロックに子の table_row を取得して children として付加する。"""
    client = Client(auth=NOTION_TOKEN)
    result = []
    for block in blocks:
        if block.get("type") == "table" and block.get("has_children"):
            block_id = block["id"]
            rows_result = client.blocks.children.list(block_id=block_id)
            rows = rows_result.get("results", [])
            clean_rows = [_sanitize_table_row(r) for r in rows]
            block = dict(block)
            block["_table_children"] = clean_rows
        result.append(block)
    return result


def get_db_page_id_by_url(url: str) -> str | None:
    """TARGET_DB_ID を検索し、URL プロパティが一致するレコードの page_id を返す。"""
    data = _post_with_retry(
        f"{_NOTION_BASE}/databases/{TARGET_DB_ID}/query",
        {"filter": {"property": "URL", "url": {"equals": url}}},
    )
    results = data.get("results", [])
    return results[0]["id"] if results else None


def db_page_has_content(page_id: str) -> bool:
    """DB レコードのページ本文にブロックが1件以上あれば True を返す。"""
    resp = httpx.get(
        f"{_NOTION_BASE}/blocks/{page_id}/children",
        headers=_headers(),
        params={"page_size": 1},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return len(results) > 0


def append_blocks(page_id: str, blocks: list[dict], dry_run: bool) -> None:
    """blocks を _BLOCKS_PER_REQUEST 件ずつ分割して追記する。"""
    total = len(blocks)
    if dry_run:
        logger.info(f"  [DRY-RUN] コピー予定: {total} ブロック → DB page_id={page_id}")
        return

    # rich_text が100件超のブロックを複数 paragraph に展開する
    expanded = []
    for b in blocks:
        overflow = b.pop("_overflow_rich_text", None)
        expanded.append(b)
        while overflow:
            chunk, overflow = overflow[:100], overflow[100:]
            expanded.append({"type": "paragraph", "paragraph": {"rich_text": chunk}})
    blocks = expanded
    total = len(blocks)

    client = Client(auth=NOTION_TOKEN)
    for i in range(0, total, _BLOCKS_PER_REQUEST):
        batch = blocks[i: i + _BLOCKS_PER_REQUEST]
        client.blocks.children.append(block_id=page_id, children=batch)
        logger.info(f"  追記完了: {i + len(batch)}/{total} ブロック")
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# --mode copy-blocks の処理
# ---------------------------------------------------------------------------

def run_copy_blocks(limit: int | None, dry_run: bool) -> None:
    subpages = get_subpages(MATOME_PAGE_ID)

    if limit is not None:
        subpages = subpages[:limit]
        logger.info(f"--limit {limit} により先頭 {len(subpages)} 件に絞ります。")

    logger.info(f"対象サブページ数: {len(subpages)}")

    copied = skipped = error_count = 0

    for page in subpages:
        subpage_id: str = page["id"]
        title: str = page.get("child_page", {}).get("title", "（タイトルなし）")
        logger.info(f"処理中: {title}")

        try:
            blocks = get_page_blocks(subpage_id)
            text = extract_text_from_blocks(blocks)
            url = parse_url(text)

            if not url:
                logger.warning(f"  URL なし、スキップ: {title}")
                skipped += 1
                continue

            db_page_id = get_db_page_id_by_url(url)
            if not db_page_id:
                logger.info(f"  DB レコードなし、スキップ: {url}")
                skipped += 1
                continue

            if db_page_has_content(db_page_id):
                logger.info(f"  既にコンテンツあり、スキップ: DB page_id={db_page_id}")
                skipped += 1
                continue

            blocks = fetch_and_attach_table_rows(blocks)
            clean_blocks = [sanitize_block(b) for b in blocks]
            clean_blocks = [b for b in clean_blocks if b is not None]

            append_blocks(db_page_id, clean_blocks, dry_run)
            copied += 1

        except Exception as e:
            logger.error(f"  エラー（スキップ）: {e}")
            error_count += 1

        time.sleep(0.34)

    logger.info(f"完了: {copied}件コピー / {skipped}件スキップ / {error_count}件エラー")


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def main() -> None:
    import scripts.migrate_notion_pages as _mig

    parser = argparse.ArgumentParser(description="Notion DB レコードに本文ブロックをコピーするパッチスクリプト")
    parser.add_argument("--mode", required=True, choices=["copy-blocks"], help="実行モード")
    parser.add_argument("--dry-run", action="store_true", help="書き込みをスキップして確認のみ")
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="先頭 N 件だけ処理（テスト用）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.dry_run:
        logger.info("[DRY-RUN] モードで実行します。DB への書き込みはスキップされます。")

    # Notion トークン解決: 環境変数 → config.yaml
    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        config_path = Path(__file__).parent.parent / "config.yaml"
        if config_path.exists():
            cfg_yaml = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            token = cfg_yaml.get("notion", {}).get("token", "")
    if not token:
        logger.error(
            "NOTION_TOKEN が見つかりません。"
            "環境変数 NOTION_TOKEN または config.yaml の notion.token を設定してください。"
        )
        sys.exit(1)

    # migrate_notion_pages.py のグローバルトークンも更新（_headers() が参照するため）
    global NOTION_TOKEN
    NOTION_TOKEN = token
    _mig.NOTION_TOKEN = token

    if args.mode == "copy-blocks":
        run_copy_blocks(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
