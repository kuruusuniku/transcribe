"""scripts/migrate_notion_pages.py

Notion の既存サブページ（まとめページ直下）を DB に一括移行する。

使い方:
    python scripts/migrate_notion_pages.py [--dry-run] [--limit N]

事前に MATOME_PAGE_ID を実際の page_id に書き換えること。
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

import httpx
import yaml
from notion_client import Client

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

MATOME_PAGE_ID = "27b3a9eeef9a80398ae0ed0ac3b541ed"
TARGET_DB_ID   = "3693a9eeef9a80bca8bbf2919d5bef77"

_NOTION_VERSION  = "2022-06-28"
_NOTION_BASE     = "https://api.notion.com/v1"
_RETRY_BACKOFFS  = (5, 10, 20)
_RICH_TEXT_LIMIT = 2000

# 指導対象の身体部位 は今回対象外
_TAG_SECTIONS = ("主要キーワード", "メソッド種別")

# ---------------------------------------------------------------------------
# グローバル状態（main() で初期化）
# ---------------------------------------------------------------------------

NOTION_TOKEN: str = ""
DRY_RUN: bool = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Notion API ヘルパー
# ---------------------------------------------------------------------------

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _get_with_retry(url: str, params: dict | None = None) -> dict:
    resp = None
    for attempt, backoff in enumerate((*_RETRY_BACKOFFS, None)):
        try:
            resp = httpx.get(url, headers=_headers(), params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if resp is None or resp.status_code != 503 or backoff is None:
                raise
            logger.warning(f"Notion 503, retry {attempt + 1} (wait {backoff}s)")
            time.sleep(backoff)
    raise RuntimeError("unreachable")


def _post_with_retry(url: str, body: dict) -> dict:
    resp = None
    for attempt, backoff in enumerate((*_RETRY_BACKOFFS, None)):
        try:
            resp = httpx.post(url, headers=_headers(), json=body, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if resp is None or resp.status_code != 503 or backoff is None:
                raise
            logger.warning(f"Notion 503, retry {attempt + 1} (wait {backoff}s)")
            time.sleep(backoff)
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# データ取得
# ---------------------------------------------------------------------------

def get_subpages(parent_page_id: str) -> list[dict]:
    """MATOME_PAGE_ID 直下のサブページ一覧を返す。"""
    client = Client(auth=NOTION_TOKEN)
    subpages: list[dict] = []
    start_cursor: str | None = None
    while True:
        kwargs: dict = {"block_id": parent_page_id}
        if start_cursor:
            kwargs["start_cursor"] = start_cursor
        result = client.blocks.children.list(**kwargs)
        for block in result.get("results", []):
            if block.get("type") == "child_page":
                subpages.append(block)
        has_more: bool = result.get("has_more", False)
        start_cursor = result.get("next_cursor")
        time.sleep(0.34)
        if not has_more:
            break
    return subpages


def get_page_blocks(page_id: str) -> list[dict]:
    """ページ本文のブロック一覧を返す（ページネーション対応）。"""
    client = Client(auth=NOTION_TOKEN)
    blocks: list[dict] = []
    start_cursor: str | None = None
    while True:
        kwargs: dict = {"block_id": page_id}
        if start_cursor:
            kwargs["start_cursor"] = start_cursor
        result = client.blocks.children.list(**kwargs)
        for block in result.get("results", []):
            if block.get("type") != "unsupported":
                blocks.append(block)
        has_more: bool = result.get("has_more", False)
        start_cursor = result.get("next_cursor")
        time.sleep(0.34)
        if not has_more:
            break
    return blocks


# ---------------------------------------------------------------------------
# テキスト変換
# ---------------------------------------------------------------------------

def _rich_text_to_str(rich_text: list[dict]) -> str:
    return "".join(item.get("plain_text", "") for item in rich_text)


def extract_text_from_blocks(blocks: list[dict]) -> str:
    """ブロックリストをプレーンテキスト（改行区切り）に変換する。"""
    lines: list[str] = []
    for block in blocks:
        btype = block.get("type", "")
        content = block.get(btype, {})
        rt = content.get("rich_text", []) if isinstance(content, dict) else []
        text = _rich_text_to_str(rt)
        if btype == "heading_1":
            lines.append(f"# {text}")
        elif btype == "heading_2":
            lines.append(f"## {text}")
        elif btype == "heading_3":
            lines.append(f"### {text}")
        elif btype == "bulleted_list_item":
            lines.append(f"- {text}")
        elif btype == "paragraph":
            lines.append(text)
        # その他はスキップ
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# フィールドパーサー
# ---------------------------------------------------------------------------

def parse_url(text: str) -> str | None:
    """'URL:' または 'URL：'（全角コロン）で始まる行から URL を抽出する。

    >>> parse_url("URL: https://example.com")
    'https://example.com'
    >>> parse_url("URL：https://example.com/path")
    'https://example.com/path'
    >>> parse_url("no url here") is None
    True
    """
    for line in text.splitlines():
        m = re.match(r"URL[：:]\s*(.+)", line.strip())
        if m:
            return m.group(1).strip()
    return None


def parse_duration_minutes(text: str) -> int | None:
    """'総時間:' または '総時間：' で始まる行から分単位の整数を返す。

    H:MM:SS → 時*60+分（秒切り捨て）、MM:SS → 分だけ返す。

    >>> parse_duration_minutes("総時間: 17:00")
    17
    >>> parse_duration_minutes("総時間: 1:10:00")
    70
    >>> parse_duration_minutes("総時間：0:45")
    0
    >>> parse_duration_minutes("総時間: 2:00:00")
    120
    >>> parse_duration_minutes("本文なし") is None
    True
    """
    for line in text.splitlines():
        m = re.match(r"総時間[：:]\s*(.+)", line.strip())
        if not m:
            continue
        parts = m.group(1).strip().split(":")
        try:
            if len(parts) == 3:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 2:
                return int(parts[0])
        except ValueError:
            return None
    return None


def parse_date(title: str) -> str | None:
    """タイトル先頭の YYMMDD を '20YY-MM-DD' に変換する。

    >>> parse_date("250617 火 楕円状に...")
    '2025-06-17'
    >>> parse_date("250104 土 新年会...")
    '2025-01-04'
    >>> parse_date("タイトルなし") is None
    True
    """
    m = re.match(r"(\d{6})", title.strip())
    if not m:
        return None
    ymd = m.group(1)
    return f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:6]}"


def extract_tags(text: str) -> list[str]:
    """_TAG_SECTIONS の箇条書き項目を抽出する（末尾の脚注番号は除去）。

    対象セクション: 主要キーワード、メソッド種別。

    >>> text = "\\n".join([
    ...     "### 主要キーワード",
    ...     "- サークルダンス 4",
    ...     "- 運動野のインストール",
    ...     "### 指導対象の身体部位",
    ...     "- 足先",
    ...     "### メソッド種別",
    ...     "- リズムトレーニング",
    ... ])
    >>> extract_tags(text)
    ['サークルダンス', '運動野のインストール', 'リズムトレーニング']
    """
    tags: list[str] = []
    lines = text.splitlines()
    for section in _TAG_SECTIONS:
        target = f"### {section}"
        in_section = False
        for line in lines:
            stripped = line.strip()
            if stripped == target:
                in_section = True
                continue
            if in_section:
                if stripped.startswith("##"):
                    break
                if stripped.startswith("- "):
                    item = re.sub(r"[\s　]+\d+\s*$", "", stripped[2:]).strip()
                    item = item.replace(",", "・").replace("，", "・")
                    tags.append(item)
    return [t for t in dict.fromkeys(tags) if t]


# ---------------------------------------------------------------------------
# DB 操作
# ---------------------------------------------------------------------------

def url_exists_in_db(url: str) -> bool:
    """TARGET_DB_ID を httpx で検索し、同一 URL が既にあれば True を返す。"""
    data = _post_with_retry(
        f"{_NOTION_BASE}/databases/{TARGET_DB_ID}/query",
        {"filter": {"property": "URL", "url": {"equals": url}}},
    )
    return len(data.get("results", [])) > 0


def build_db_properties(
    title: str,
    date: str | None,
    url: str | None,
    minutes: int | None,
    tags: list[str],
) -> dict:
    """Notion API 用のプロパティ dict を組み立てる。"""
    props: dict = {
        "名前": {"title": [{"text": {"content": title[:_RICH_TEXT_LIMIT]}}]},
        "タグ": {"multi_select": [{"name": t} for t in tags]},
        "まとめ進捗": {"checkbox": True},
    }
    if date:
        props["日付"] = {"date": {"start": date}}
    if url and url.startswith(("http://", "https://")):
        props["URL"] = {"url": url}
    if minutes is not None:
        props["動画時間"] = {"number": minutes}
    return props


# ---------------------------------------------------------------------------
# 移行処理
# ---------------------------------------------------------------------------

def migrate_page(page: dict) -> None:
    """1ページ分の移行処理を実行する（dry-run 対応）。"""
    page_id: str = page["id"]
    title: str = page.get("child_page", {}).get("title", "（タイトルなし）")

    logger.info(f"処理中: {title}")

    blocks = get_page_blocks(page_id)
    text = extract_text_from_blocks(blocks)

    url = parse_url(text)
    minutes = parse_duration_minutes(text)
    date = parse_date(title)
    tags = extract_tags(text)

    logger.info(f"  URL={url}, 日付={date}, 動画時間={minutes}分, タグ={tags}")

    if url is None:
        logger.info("  スキップ（URL なし）: URL が取得できませんでした")
        return

    if url_exists_in_db(url):
        logger.info("  スキップ（重複）: URL が既に DB に存在します")
        return

    props = build_db_properties(title, date, url, minutes, tags)

    if DRY_RUN:
        logger.info(f"  [DRY-RUN] DB 書き込みをスキップします（properties={props}）")
        return

    client = Client(auth=NOTION_TOKEN)
    created = client.pages.create(
        parent={"database_id": TARGET_DB_ID},
        properties=props,
    )
    logger.info(f"  DB ページ作成完了 (id={created['id']})")


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def main() -> None:
    global DRY_RUN, NOTION_TOKEN

    parser = argparse.ArgumentParser(description="Notion サブページを DB に一括移行する")
    parser.add_argument("--dry-run", action="store_true", help="DB への書き込みをスキップし変換結果をログ出力")
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="先頭 N 件だけ処理（テスト用）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    DRY_RUN = args.dry_run
    if DRY_RUN:
        logger.info("[DRY-RUN] モードで実行します。DB への書き込みはスキップされます。")

    # Notion トークン解決: 環境変数 → config.yaml
    NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
    if not NOTION_TOKEN:
        config_path = Path(__file__).parent.parent / "config.yaml"
        if config_path.exists():
            cfg_yaml = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            NOTION_TOKEN = cfg_yaml.get("notion", {}).get("token", "")
    if not NOTION_TOKEN:
        logger.error("NOTION_TOKEN が見つかりません。環境変数 NOTION_TOKEN または config.yaml の notion.token を設定してください。")
        sys.exit(1)

    pages = get_subpages(MATOME_PAGE_ID)
    if args.limit is not None:
        pages = pages[: args.limit]
        logger.info(f"--limit {args.limit} により先頭 {len(pages)} 件に絞ります。")

    logger.info(f"対象サブページ数: {len(pages)}")
    processed = 0
    error_count = 0
    for page in pages:
        try:
            migrate_page(page)
            processed += 1
        except Exception as e:
            logger.error(f"エラー（スキップ）: {e}")
            error_count += 1

    logger.info(f"完了: {processed}件処理 / {error_count}件エラー")


if __name__ == "__main__":
    main()
