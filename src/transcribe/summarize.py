from __future__ import annotations

import logging
import time
from pathlib import Path

from .config import SummarizeConfig

logger = logging.getLogger(__name__)


class SummaryTruncatedError(RuntimeError):
    """出力トークン上限に達し、まとめが途中で切れた。"""

    def __init__(self, provider: str, partial_text: str) -> None:
        super().__init__(
            f"{provider} の出力が max_output_tokens に達し途中で切れました。"
            "config.yaml の summarize.max_output_tokens を増やしてください。"
        )
        self.partial_text = partial_text


_GEMINI_503_RETRY_BACKOFFS_SEC = (5, 15, 30)
_CLAUDE_503_RETRY_BACKOFFS_SEC = (5, 10, 20)


def _call_with_retry(fn, provider_name: str, backoffs: tuple) -> str:
    """503 Service Unavailable を自動リトライするヘルパー。

    fn を呼び出し、503 系エラー（.code==503 または .status_code==503）なら
    backoffs に従ってリトライする。それ以外のエラーおよびリトライ上限超過は即座に伝播する。
    """
    max_retries = len(backoffs)
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            is_503 = getattr(e, "code", None) == 503 or getattr(e, "status_code", None) == 503
            if not is_503 or attempt >= max_retries:
                raise
            wait = backoffs[attempt]
            logger.warning(
                f"{provider_name} 503, retry {attempt + 1}/{max_retries} (wait {wait}s)"
            )
            time.sleep(wait)
    raise RuntimeError("到達不能: 503 リトライループが想定外に終了しました")


SYSTEM_PROMPT_BASE = """\
あなたは KYOTA 式体育指導の動画書き起こしを構造化まとめにする専門家である。
このまとめは「身体知の図書館」のインデックスとして機能する内部資料となる。

# 厳格なルール

## A. 厳格な入力依存（推測・補完の禁止）

最重要ルール: まとめの内容は、ユーザーから提供される「書き起こし」テキストのみを唯一の情報源とする。

- 禁止: 外部知識や一般常識で書き起こしに存在しない情報を「推測」「補完」すること。
- 例: 書き起こしに「霊格」という単語がなければ、たとえ類似動画にあっても今回のまとめに含めない。
- 理由: 各動画と1対1で対応する正確性が求められる。

## B. 言語と文体

- 言語: 日本語
- 文体: 客観的かつ簡潔な「である調」または「体言止め」を使用。丁寧語（です・ます調）は不要。

## C. アクセシビリティの確保（動画インデックス機能）

- 「メタデータ・タグ」「タイムスタンプ付きまとめ」「マイクロラーニング用チャプター分割表」の各セクションで、書き起こしのタイムスタンプ情報を参照し、必ず時刻表記を含めること。
- YouTube 動画の場合: タイムスタンプは `[[HH:MM:SS](URL?t=秒数)]` 形式でリンクを生成する。URL は提供される video_url に `&t={秒数}s` を付与する。Google 検索の URL を余計に付与してはいけない。
- ローカルファイルの場合: リンクなしの `[HH:MM:SS]` のみで表記する。

## D. 用語統一

- 後述の「用語集」を参照し、書き起こし内の表記ゆれや音声認識ミスと思われる箇所を正式名称に統一すること。

# 必須出力フォーマット

以下の構造と見出しを厳密に遵守すること。Markdown で出力する。

```
動画タイトル: {動画タイトル}
URL: {動画URL}
総時間: {書き起こしから推定した総時間 HH:MM:SS}

# {書き起こし内容を元に作成した、動画の主題を表すタイトル}

## 1. 動画の全体要約

{動画全体の目的、流れ、主要なテーマを約200字程度で簡潔に要約}

## 2. メタデータ・タグ

### 主要キーワード
{書き起こし中に出現する核心的キーワードをリスト形式で抽出}
* キーワード1
* キーワード2

### 指導対象の身体部位
{書き起こし中で言及される身体部位をリスト形式で抽出}
* 部位1

### 対象者 (特に取り組んだほうが良い課題のある人)
* 対象者1

### メソッド種別
{動画内で紹介される指導の種類をカテゴリ分けしてリスト形式で記述}
* 種別1

### 推奨難易度
{動画の内容を考慮し、推奨される学習者のレベルを記述。例: 中級者〜上級者}

## 3. タイムスタンプ付きまとめ（動画インデックス）

{動画の内容を時系列に沿って論理的セクションに分割する}

### {セクション1のタイトル}

#### {メソッド1の名称} [[HH:MM:SS](URL?t=秒数)]
* **概要**: {このメソッドの目的や内容を1〜2文で要約}
* **ポイント**:
  * {具体的な手順や注意点}
  * {指導者が強調しているコツや意識すべき点}

#### {メソッド2の名称} [[HH:MM:SS](URL?t=秒数)]
* **概要**: ...
* **ポイント**:
  * ...

## 4. マイクロラーニング用チャプター分割表

| チャプター番号 | 開始時間 | 終了時間 | チャプター | タイトル内容の要約 |
| :------------- | :------- | :------- | :------------- | :--------------------------------- |
| 1              | HH:MM    | HH:MM    | {タイトル1}    | {要約1}                            |
| 2              | HH:MM    | HH:MM    | {タイトル2}    | {要約2}                            |
```
"""


def _build_glossary_section(glossary_entries: list[dict]) -> str:
    """glossary.yaml の literal タイプのエントリのみを用語集セクションに整形する。"""
    literal_entries = [
        e for e in glossary_entries
        if isinstance(e, dict) and e.get("type", "literal") == "literal"
    ]
    if not literal_entries:
        return ""

    lines = [
        "",
        "# 用語集（表記統一用）",
        "",
        "以下の表記ゆれや音声認識ミスは正式名称に統一すること。",
        "",
    ]
    for entry in literal_entries:
        pattern = entry.get("pattern", "")
        replacement = entry.get("replacement", "")
        if pattern and replacement:
            lines.append(f"- {pattern} → {replacement}")
    return "\n".join(lines)


def build_prompt(
    transcript_text: str,
    glossary_entries: list[dict],
    video_url: str,
    video_title: str,
    source_type: str = "youtube",
) -> tuple[str, str]:
    """システムプロンプトとユーザーメッセージを構築する。"""
    system_prompt = SYSTEM_PROMPT_BASE + _build_glossary_section(glossary_entries)

    source_note = ""
    if source_type == "local":
        source_note = (
            "\n注: これはローカル音声ファイル由来の書き起こしであり、"
            "タイムスタンプは `[HH:MM:SS]` 形式でリンクなしで記述すること。"
        )

    user_message = (
        f"以下の書き起こしを構造化まとめにしてください。\n\n"
        f"動画タイトル: {video_title}\n"
        f"URL: {video_url}\n"
        f"{source_note}\n"
        f"---\n\n"
        f"{transcript_text}"
    )
    return system_prompt, user_message


def call_gemini(
    system_prompt: str,
    user_message: str,
    cfg: SummarizeConfig,
) -> str:
    """Gemini API を呼び出し、まとめテキストを返す。

    503 Service Unavailable は最大 3 回までリトライする
    （バックオフ: 5 秒 → 15 秒 → 30 秒）。それ以外のエラーは即座に伝播する。
    """
    from google import genai
    from google.genai import types as genai_types

    api_key = cfg.resolve_api_key()
    if not api_key:
        raise RuntimeError(
            "Gemini API キーが設定されていません。"
            "config.yaml の summarize.gemini_api_key または 環境変数 GEMINI_API_KEY を設定してください。"
        )

    client = genai.Client(api_key=api_key)

    def _invoke():
        response = client.models.generate_content(
            model=cfg.gemini_model,
            contents=user_message,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=cfg.max_output_tokens,
                temperature=cfg.temperature,
            ),
        )
        text = response.text or ""
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            reason = getattr(candidates[0], "finish_reason", None)
            if getattr(reason, "name", reason) == "MAX_TOKENS":
                raise SummaryTruncatedError("Gemini", text)
        return text

    return _call_with_retry(_invoke, "Gemini", _GEMINI_503_RETRY_BACKOFFS_SEC)


def call_claude(
    system_prompt: str,
    user_message: str,
    cfg: SummarizeConfig,
) -> str:
    """Claude API を呼び出し、まとめテキストを返す。"""
    import anthropic

    api_key = cfg.resolve_api_key()
    if not api_key:
        raise RuntimeError(
            "Anthropic API キーが設定されていません。"
            "config.yaml の summarize.anthropic_api_key または 環境変数 ANTHROPIC_API_KEY を設定してください。"
        )

    client = anthropic.Anthropic(api_key=api_key)

    def _invoke():
        message = client.messages.create(
            model=cfg.anthropic_model,
            max_tokens=cfg.max_output_tokens,
            temperature=cfg.temperature,
            # システムプロンプト（指示 + 用語集）は全ジョブ共通のため、一括まとめ時にキャッシュを効かせる。
            # モデルごとの最小キャッシュ長に満たない場合は API 側で単に無視される。
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_message}],
        )
        if not message.content:
            return ""
        texts = [b.text for b in message.content if b.type == "text"]
        text = "\n".join(texts)
        if getattr(message, "stop_reason", None) == "max_tokens":
            raise SummaryTruncatedError("Claude", text)
        return text

    return _call_with_retry(_invoke, "Claude", _CLAUDE_503_RETRY_BACKOFFS_SEC)


def generate_summary(
    output_dir: Path,
    video_url: str,
    video_title: str,
    cfg: SummarizeConfig,
    glossary_entries: list[dict],
    source_type: str = "youtube",
) -> Path | None:
    """transcript.md を読み込み、LLM でまとめを生成し、summary.md に書き出す。

    成功時は summary.md のパスを返す。transcript.md がない、または応答が空なら None。
    """
    transcript_path = output_dir / "transcript.md"
    if not transcript_path.exists():
        logger.warning(f"transcript.md が見つかりません: {transcript_path}")
        return None

    transcript_text = transcript_path.read_text(encoding="utf-8")
    system_prompt, user_message = build_prompt(
        transcript_text=transcript_text,
        glossary_entries=glossary_entries,
        video_url=video_url,
        video_title=video_title,
        source_type=source_type,
    )

    try:
        if cfg.provider == "gemini":
            summary_text = call_gemini(system_prompt, user_message, cfg)
        elif cfg.provider == "claude":
            summary_text = call_claude(system_prompt, user_message, cfg)
        else:
            raise ValueError(f"未対応の provider: {cfg.provider}")
    except SummaryTruncatedError as e:
        # 不完全なまとめを summary.md として扱わない（Notion 同期もされない）。確認用に別名で残す。
        truncated_path = output_dir / "summary.truncated.md"
        truncated_path.write_text(e.partial_text, encoding="utf-8")
        logger.error(f"{e} 途中までの出力: {truncated_path}")
        raise

    if not summary_text.strip():
        logger.warning(f"まとめ結果が空でした: {output_dir}")
        return None

    summary_path = output_dir / "summary.md"
    summary_path.write_text(summary_text, encoding="utf-8")
    (output_dir / "summary.truncated.md").unlink(missing_ok=True)
    logger.info(f"summary.md 書き出し: {summary_path}")
    return summary_path
