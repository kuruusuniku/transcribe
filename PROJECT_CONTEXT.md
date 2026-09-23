# PROJECT_CONTEXT

**最終更新日**: 2026-09-23

体育指導の YouTube 限定公開動画と、叡智講義の録音ファイルをローカル GPU で自動文字起こしし、
まとめ生成・Notion / Google Docs 同期まで一気通貫で行うツール。

| 素材 | 扱い | まとめプロンプト | Notion 登録先 |
|---|---|---|---|
| YouTube URL | 体育指導の動画 | `SYSTEM_PROMPT_BASE` | 体育動画まとめDB（`notion.database_id`） |
| 録音ファイル（mp3 / m4a） | 叡智講義 | `LECTURE_SYSTEM_PROMPT` | 叡智まとめDB（`notion.local_database_id`） |

---

## 1. 概要

- 1日1本、約1時間の動画・講義を処理（Web UI から随時、または夜間バッチ）→ 翌朝確認
- ローカル GPU（GTX 1050 Ti Max-Q, VRAM 4GB）で faster-whisper large-v3 を動かす
- BGM が乗った状態でも稽古指導音声を正確に書き起こす
- 武術独自用語（中足靭帯、臍下丹田、八つの心得 など）に対応
- **完走最優先**。途中で落ちず、全ジョブを処理しきることが最重要

---

## 2. 実行環境

- OS: Windows 11
- Python: 3.13.12
- uv: 0.11.8
- GPU: GTX 1050 Ti Max-Q, VRAM 4GB, CUDA 12.2
- torch 2.6.0+cu124（cu121は Python 3.13 ホイールなし → cu124）
- ffmpeg 8.0.1
- PowerShell 実行時は `$env:PYTHONIOENCODING="utf-8"` 必須（文字化け防止）

---

## 3. 技術スタック

| 用途 | ライブラリ |
|---|---|
| 音声DL | yt-dlp |
| 音声分離（オプション） | demucs 4.0.1 |
| 文字起こし | faster-whisper |
| GPU推論 | torch 2.6.0+cu124, ctranslate2 |
| 設定 | PyYAML |
| 状態管理 | SQLite (stdlib) |
| ロギング | logging + Rich |
| CLI | Typer |
| Google Docs 同期 | google-api-python-client >= 2.0 |
| OAuth 認証 | google-auth-httplib2, google-auth-oauthlib |
| LLM まとめ（プライマリ） | google-genai >= 1.0（Gemini 2.5 Flash） |
| LLM まとめ（フォールバック） | anthropic >= 0.40（Claude Haiku） |
| Web UI サーバー | fastapi >= 0.115, uvicorn[standard] >= 0.30 |
| ファイルアップロード | python-multipart >= 0.0.9 |

---

## 4. プロジェクト構造

```
transcribe/
├── pyproject.toml
├── README.md
├── GETTING_STARTED.md / TROUBLESHOOTING.md / USAGE.md
├── config.yaml / config.example.yaml
├── glossary.yaml
├── urls.txt              # URL とローカルパスの混在可
├── credentials.json      # Google OAuth（gitignore）
├── token.json            # Google OAuth トークン（gitignore）
├── data/
│   ├── state.db
│   ├── work/             # ダウンロード音声・文字起こしキャッシュ（<video_id>.whisper.json）
│   ├── uploads/{uuid}/   # Web UI からアップロードした録音ファイル
│   └── output/{YYYY-MM-DD}_{video_id or filename_stem_hash}/
│       ├── transcript.md
│       ├── summary.md         # LLM 自動まとめ
│       ├── summary.truncated.md  # 上限切れで復旧できなかった場合のみ
│       ├── segments.json
│       ├── meta.json
│       └── notion.json        # 同期先 DB とページ ID
├── logs/{YYYY-MM-DD}.log
├── src/transcribe/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── pipeline.py
│   ├── state.py
│   ├── postprocess.py
│   ├── utils.py
│   ├── sync.py           # Google Docs 同期（OAuth + Drive API）
│   ├── summarize.py      # LLM 自動カスタムまとめ（体育指導 / 叡智講義の 2 プロンプト・崩れ復旧・時刻補正）
│   ├── notion_sync.py    # Notion 同期（DB プロパティ構成に合わせて送信）
│   ├── notify.py         # バッチ完了メール（Gmail API）
│   ├── health.py         # 設定・連携の診断（doctor / 設定状況）
│   ├── stages/
│   │   ├── downloader.py
│   │   ├── separator.py
│   │   ├── transcriber.py
│   │   └── formatter.py
│   └── web/              # FastAPI アプリ一式
│       ├── app.py            # FastAPI アプリ本体
│       ├── runner.py         # タスク投入・ログ配信（task_id ↔ キュー）
│       ├── worker.py         # 単一ワーカースレッドで CLI コマンドをインプロセス実行（Whisper モデル常駐）
│       ├── output_router.py  # ワーカースレッドの stdout/stderr をタスクログに振り分け
│       ├── security.py       # Origin チェック・トークン認証ミドルウェア
│       ├── deps.py           # get_config()（config.yaml の更新を自動反映）
│       ├── glossary_path.py  # glossary.yaml のパス
│       ├── routes/
│       │   ├── jobs.py       # ジョブ一覧・詳細・閲覧 API
│       │   ├── commands.py   # run/sync/summarize/rerun API
│       │   └── files.py      # ファイルアップロード・変換 API
│       ├── ws/
│       │   └── log_stream.py # WebSocket ログストリーミング
│       └── static/
│           ├── index.html    # React エントリポイント
│           ├── app.jsx       # React コンポーネント群
│           └── favicon.svg   # ファビコン（巻物×音波デザイン）
├── scripts/
│   ├── migrate_notion_pages.py  # Notion 既存ページ → DB 一括移行
│   └── patch_notion_db.py       # Notion DB レコード補完パッチ
└── tests/
    ├── conftest.py            # Notion のプロパティ取得を既定で無効化（実ネットワーク禁止）
    ├── test_clean.py          # 一時ファイル削除
    ├── test_delete.py
    ├── test_downloader.py
    ├── test_formatter.py      # 区切り方・ヘッダー・reformat
    ├── test_local_file.py
    ├── test_notify.py
    ├── test_notion_sync.py
    ├── test_pipeline_retry.py
    ├── test_postprocess.py
    ├── test_rerun.py
    ├── test_separator.py
    ├── test_stages.py         # ステージ記録・文字起こしキャッシュ
    ├── test_summarize.py
    ├── test_sync.py
    ├── test_utils.py          # URL 正規化・時刻整形
    ├── test_web.py
    └── test_worker.py         # インプロセス実行・タスク取り消し
```

**テスト総数**: 294件（2026-09-23 時点）

---

## 5. 完了済みの主要実装

### 1. 基盤（completed）
- pyproject.toml, ディレクトリ構造
- config.py / glossary.yaml ロード
- state.py（SQLite: jobs テーブル）
- ロギング（Rich + ファイル）

### 2. CLI / Pipeline（completed）
- `transcribe run` / `status` / `retry` / `clean`
- pipeline.py で全ステージオーケストレーション、リトライ
- VRAM 管理（Demucs 後に del → gc.collect → empty_cache → Whisper ロード）

### 3. ステージ実装（completed）
- downloader.py（yt-dlp、cookies_from_browser）
- separator.py（demucs、オプション）
- transcriber.py（faster-whisper, initial_prompt 注入）
- formatter.py（Markdown + JSON 出力）
- postprocess.py（用語置換・信頼度判定）

### 4. リトライ・エラーハンドリング（completed）
- ジョブ単位リトライ、指数バックオフ
- NON_RETRYABLE エラー分類（PermissionError 含む）

### 5. 用語辞書運用（completed）
- glossary.yaml に substitutions / important_terms / context
- 運用しながら誤認識パターンを追加中（5回目まで反映済み）

### 6. MP3/m4a ローカルファイル文字起こし（completed）
- `transcribe file <path>` サブコマンド
- `urls.txt` に URL とローカルパスの混在可（自動判別）
- state.db に `source_type` カラム追加（youtube / local）
- 出力: `data/output/{YYYY-MM-DD}_{filename_stem}/`
- 対応形式: `.mp3`, `.m4a`（`SUPPORTED_AUDIO_EXTENSIONS` 定数）
- rerun 時に元ファイル不在なら警告して skip
- `PermissionError` を NON_RETRYABLE に追加
- テスト 37件

### 7. transcribe convert コマンド（completed）
- `transcribe convert <path>` で m4a → mp3 変換
- ffmpeg を subprocess で呼び出し
- `--force` オプションで上書き確認スキップ

### 8. Google Docs 同期（completed）
- `transcribe sync` / `transcribe sync --all` コマンド
- OAuth 認証（初回ブラウザ、以降トークン自動更新）
- Google Drive にソース種別サブフォルダ（`youtube/` `local/`）自動作成
- `transcript.md` を Google Docs 形式に変換してアップロード
- `summary.md` が存在すれば「（まとめ）」付きで併せて同期
- パイプライン完了時に自動同期（best-effort、失敗してもジョブは done）
- state.db に `synced_at` カラム追加
- スコープ: `drive.file`（最小権限）
- テスト 19件

### 9. LLM 自動カスタムまとめ（completed）
- `transcribe summarize` / `--all` / `--id N` コマンド
- Gemini 2.5 Flash（プライマリ）+ Claude API Haiku（フォールバック）
- KYOTA式構造化まとめルールブックをシステムプロンプトに組み込み
- `glossary.yaml` を用語統一用コンテキストとして LLM に注入
- 出力: `data/output/{日付}_{id}/summary.md`
- パイプライン完了時の自動実行（best-effort）
- state.db に `summarized_at` カラム追加
- API キーは `config.yaml` または環境変数（`GEMINI_API_KEY` / `ANTHROPIC_API_KEY`）
- テスト 34件

### 10. Web UI（completed）
- `transcribe web [--host] [--port] [--reload]` で http://localhost:8000 を起動
- 全コマンドをブラウザから操作（run / file / sync / summarize / rerun / convert）
- WebSocket でリアルタイムログストリーミング
- ファイルアップロードでローカル mp3/m4a を文字起こし
- `transcript.md` / `summary.md` のインライン閲覧
- React CDN（ビルドステップ不要）、将来の Next.js 移行を視野に入れた設計
- state.db の変更なし
- テスト 18件追加（全165件パス）

### 11. Web UI 強化（completed）
- D&Dエリアを追加（.mp3 / .m4a をドロップ後に用途選択）
- m4a→mp3 変換をキュー処理化（1件ずつ順次変換・進捗表示）
- File System Access API による保存先フォルダ指定（Brave/Chrome/Edge 対応）
- 保存先フォルダを IndexedDB で永続化（再起動後も維持）
- 複数ファイル・フォルダ選択対応（`/api/convert` を複数ファイル受付に変更）
- ファビコン追加（`static/favicon.svg`、巻物×音波デザイン）
- Content-Disposition ヘッダーの日本語ファイル名エンコードエラーを修正

### 12. Notion 同期強化（completed）
- タグ自動抽出を3セクション対応に拡張（主要キーワード・メソッド種別・指導対象の身体部位）
- タグ名のカンマを `・` に自動置換（Notion multi_select のバリデーション対応）
- 重複タグを自動排除

### 13. Notion DB 移行スクリプト（completed）
- `scripts/patch_notion_db.py` を新規作成
- `--mode copy-blocks`: 元まとめサブページの本文ブロックを DB レコードに一括コピー（85件対応）
- link_preview mention → テキストリンク変換、table ブロックの children 自動付加、rich_text 100件超の自動分割など Notion API 制約を吸収

### 14. 堅牢性の強化（completed / 2026-09）
- 複数プロセスの同時実行をファイルロックで直列化（同じジョブの二重処理を防止）
- YouTube URL を `watch?v=ID` に正規化して重複ジョブを防止／録音ファイルは内容ハッシュ（`jobs.content_hash`）で二重登録を防止
- Whisper の生出力を `work_dir/<video_id>.whisper.json` にキャッシュし、後段の失敗時のリトライで再文字起こしを回避
- SQLite を WAL 化・ロック待ち 30 秒
- まとめの出力上限切れ（`SummaryTruncatedError`）と出力の崩れ（同じ文字の連続・文書の繰り返し）を検出し、可能なら復旧
- Notion ページ更新を「新ブロック追加 → 成功後に旧ブロック削除」に変更

### 15. ステージ単位の状態管理（completed / 2026-09）
- `job_stages` テーブル（job_id, stage, status, attempts, error, progress, started_at, finished_at）
  - 本体: download / separate / transcribe / format
  - 後処理: summarize / docs_sync / notion_sync（done / skipped / failed）
- 文字起こしの進捗率を記録（Web UI の進捗バー・残り時間の推定に使用）
- `transcribe status --id N` と `GET /api/jobs/{id}/stages`
- `transcribe resume-post <id>`：失敗・未実行の後処理だけをやり直す

### 16. Web UI の全面再構成（completed / 2026-09）
- サブプロセス実行をやめ、Web サーバー内の単一ワーカースレッド（`web/worker.py`）で CLI コマンドをインプロセス実行
  - 同時実行は常に 1 件。Whisper モデルをタスク間で再利用し、10 分アイドルで解放
  - ワーカースレッドの stdout/stderr のみタスクログへ振り分け（`web/output_router.py`）
  - 待機中タスクの取り消しに対応
- 画面を「入れる → 待つ → 要対応だけ確認する」の流れに再構成
  - 一覧: 要対応 / 処理中 / 完了 / すべて、種別ラベル（体育動画 / 叡智講義）、進捗・残り時間
  - 詳細: 状態に応じた主ボタン、ステージ表示、まとめ / 文字起こし / 処理の記録タブ
  - 文字起こしで語句を選択 → 用語辞書に登録（その場で反映＋次回以降も自動修正）
  - 設定状況（`health.py`）・使い方ガイド・折りたたみログ
- アクセス制御（`web/security.py`）: Origin チェックと `web.token` によるトークン認証

### 17. 叡智講義対応・時刻の正確化（completed / 2026-09）
- 録音ファイルを叡智講義として講義用プロンプトでまとめ、叡智まとめDB に登録
- Notion の DB プロパティ構成を取得し、存在するプロパティだけ書き込む（DB ごとの差異を吸収）
- 書き起こしの見出しを「実際に話し始めた時刻」に変更（`group_segments`）。区切りは最大 30 秒＋無音 1 秒
- まとめ内の時刻を実在のセグメント開始時刻へ補正し、YouTube リンクの `t=` も貼り直す
- `transcribe reformat <id>`：再文字起こしなしで `segments.json` から書き起こしを作り直す

---

## 6. 本運用フェーズの状況

Web UI の「＋ 追加」に URL / 録音ファイルを入れると、以下が自動実行される（`transcribe run` でも同じ）:

1. YouTube 動画ダウンロード or 録音ファイルの読み込み
2. faster-whisper で文字起こし → postprocess → `transcript.md` / `segments.json` / `meta.json`
3. Gemini 2.5 Flash で構造化まとめ（素材に応じたプロンプト）→ `summary.md`
4. Notion（体育動画まとめDB / 叡智まとめDB）と Google Docs に同期
5. バッチ完了時に結果サマリーメール（`notification`）

各ステップは best-effort で、後段の失敗は前段の成果物を残したまま完了する。
失敗は `job_stages` に記録され、Web UI の「要対応」と「後処理をやり直す」で復旧する。

---

## 7. 次フェーズ / PENDING

- 叡智まとめDB の「カテゴリー」の決め方が未定（現在は空欄で登録）
- 叡智講義の日付は元データに無い。ファイル名先頭の日付（例 `250430_講義.mp3`）からのみ設定される
- 叡智まとめの「タグ」は主要キーワードを使用。`Downloads/eichi_matome_extension.sql` にある
  太陽系叡智系譜図（21 ノード）のタグ体系は仮案のため**ノータッチ**（叡智まとめの後続フェーズで検討）
- 旧形式のまま残っているジョブ（約 20 件）の `reformat` + まとめ作り直しは未実施
- 録音ファイルの Notion 重複判定は出力フォルダの `notion.json` に依存（フォルダ削除で重複作成の可能性）
- Demucs のモデルキャッシュは VRAM 4GB のため見送り
- WhisperX 対応（単語単位タイムスタンプ、優先度低）

---

## 8. config.yaml の主要セクション

`config.example.yaml` 参照。

- `paths` — work_dir / output_dir / state_db / log_dir
- `youtube` — cookies_from_browser
- `audio_separation` — enabled, model, device
- `transcription` — model, compute_type, device, language, beam_size, vad_filter
- `output` — timestamp_interval_seconds（1 区切りの最大の長さ・既定 30 秒）, paragraph_gap_seconds, segment_timestamps, confidence_threshold
- `retry` — max_attempts, backoff_seconds
- `logging` — level, console, file, retention_days
- `google_docs` — enabled, credentials_path, token_path, root_folder_id
- `summarize` — enabled, provider, gemini_api_key, gemini_model, anthropic_api_key, anthropic_model, max_output_tokens（既定 16384）, temperature
- `notion` — enabled, token, database_id（体育動画）, local_database_id（叡智講義）
- `web` — host, port, token（設定するとトークン認証。127.0.0.1 以外では必須）
- `notification` — enabled, to_email

---

## 9. gitignore 済み

- `credentials.json`
- `token.json`
- `data/`, `logs/`, `.venv/` など

---

## 10. 運用上の注意点

- VRAM 4GB 制約: `compute_type="int8"` 必須（Pascal/CC6.1 は int8_float16 の Tensor Core 非対応）、Demucs と Whisper の同時 GPU ロード不可
- yt-dlp の Cookie 認証は対象ブラウザを完全終了している必要がある
- Windows パスは全て `pathlib.Path` で扱う
- ログ書き込みは `encoding="utf-8"` 必須（日本語ファイル名対応）
- PowerShell 実行時は `$env:PYTHONIOENCODING="utf-8"` 設定が必要
- Google Docs 同期の初回実行時はブラウザ認証が必要
- Gemini 無料枠は高負荷時に 503 が出ることがある（再実行で回復）
- summarize / sync の失敗はジョブ全体を失敗にしない（best-effort）
- Web UI は `uv run transcribe web` で起動、http://localhost:8000 でアクセス
- 外部公開する場合は `web.token` の設定が必須（未設定では 127.0.0.1 以外で起動しない）
- Web サーバーのワーカー内ではブラウザ認証を開始しない（`TRANSCRIBE_WEB_SERVER=1`）。
  Google の再認証が必要になったら CLI で `uv run transcribe sync` を実行する
- Web UI を起動している間は Whisper モデルを最大 10 分保持するため、他の GPU アプリと併用しない
- 設定の確認は `uv run transcribe doctor [--test]`（Web UI では「設定状況」）

---

## 11. ドキュメント構成

| 読み手 | ファイル |
|---|---|
| 使う人（まずここ） | `GETTING_STARTED.md` |
| うまく動かないとき | `TROUBLESHOOTING.md` |
| コマンド・設定のリファレンス | `USAGE.md` |
| 開発の経緯・構成（本ファイル） | `PROJECT_CONTEXT.md` |
| 開発上の注意点 | `HANDOVER.md` |
| 初期構築時の指示書（歴史的資料） | `CLAUDE_CODE_PROMPT.md` |
