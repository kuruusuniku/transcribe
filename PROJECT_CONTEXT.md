# PROJECT_CONTEXT

**最終更新日**: 2026-05-23

YouTube限定公開動画（武術稽古指導の録画）をローカルGPUで自動文字起こしし、まとめ生成・Google Docs 同期まで一気通貫で行うツール。

---

## 1. 概要

- 1日1本、約1時間の動画を夜間バッチで処理 → 翌朝確認
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
├── config.yaml / config.example.yaml
├── glossary.yaml
├── urls.txt              # URL とローカルパスの混在可
├── credentials.json      # Google OAuth（gitignore）
├── token.json            # Google OAuth トークン（gitignore）
├── data/
│   ├── state.db
│   ├── work/
│   └── output/{YYYY-MM-DD}_{video_id or filename_stem}/
│       ├── transcript.md
│       ├── summary.md         # LLM 自動まとめ
│       ├── segments.json
│       └── meta.json
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
│   ├── summarize.py      # LLM 自動カスタムまとめ（Gemini/Claude）
│   ├── stages/
│   │   ├── downloader.py
│   │   ├── separator.py
│   │   ├── transcriber.py
│   │   └── formatter.py
│   └── web/              # FastAPI アプリ一式
│       ├── app.py            # FastAPI アプリ本体
│       ├── runner.py         # 非同期コマンド実行エンジン
│       ├── deps.py           # get_config() 依存関数
│       ├── routes/
│       │   ├── jobs.py       # ジョブ一覧・詳細・閲覧 API
│       │   ├── commands.py   # run/sync/summarize/rerun API
│       │   └── files.py      # ファイルアップロード・変換 API
│       ├── ws/
│       │   └── log_stream.py # WebSocket ログストリーミング
│       └── static/
│           ├── index.html    # React エントリポイント
│           └── app.jsx       # React コンポーネント群
└── tests/
    ├── test_local_file.py     # 37件
    ├── test_pipeline_retry.py
    ├── test_postprocess.py
    ├── test_rerun.py
    ├── test_separator.py
    ├── test_summarize.py      # 34件
    ├── test_sync.py           # 19件
    └── test_web.py            # 18件
```

**テスト総数**: 165件（全パス）

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

---

## 6. 本運用フェーズの状況

`transcribe run` 1コマンドで以下が自動実行される:

1. YouTube 動画ダウンロード or ローカルファイルコピー
2. faster-whisper で文字起こし → postprocess → `transcript.md`
3. Gemini 2.5 Flash で構造化まとめ → `summary.md`
4. Google Docs に `transcript.md` + `summary.md` を同期

各ステップは best-effort で、後段の失敗は前段の成果物を残したまま完了する設計。

---

## 7. 次フェーズ / PENDING

- glossary 6回目の確認待ち用語（約 20 件、`t-UHRZTt7Ag`）
- WhisperX 対応（単語単位タイムスタンプ、優先度低）
  - `config.yaml` の `backend: faster-whisper or whisperx` で切り替え可能にする
  - `transcriber.py` をディスパッチャ化 → `backends/` に分割

---

## 8. config.yaml の主要セクション

`config.example.yaml` 参照。

- `paths` — work_dir / output_dir / state_db / log_dir
- `youtube` — cookies_from_browser
- `audio_separation` — enabled, model, device
- `transcription` — model, compute_type, device, language, beam_size, vad_filter
- `output` — timestamp_interval_seconds, confidence_threshold
- `retry` — max_attempts, backoff_seconds
- `logging` — level, console, file
- `google_docs` — enabled, credentials_path, token_path, root_folder_id
- `summarize` — enabled, provider, gemini_api_key, gemini_model, anthropic_api_key, anthropic_model, max_output_tokens, temperature

---

## 9. gitignore 済み

- `credentials.json`
- `token.json`
- `data/`, `logs/`, `.venv/` など

---

## 10. 運用上の注意点

- VRAM 4GB 制約: `compute_type="int8_float16"` 必須、Demucs と Whisper の同時 GPU ロード不可
- yt-dlp の Cookie 認証は対象ブラウザを完全終了している必要がある
- Windows パスは全て `pathlib.Path` で扱う
- ログ書き込みは `encoding="utf-8"` 必須（日本語ファイル名対応）
- PowerShell 実行時は `$env:PYTHONIOENCODING="utf-8"` 設定が必要
- Google Docs 同期の初回実行時はブラウザ認証が必要
- Gemini 無料枠は高負荷時に 503 が出ることがある（再実行で回復）
- summarize / sync の失敗はジョブ全体を失敗にしない（best-effort）
- Web UI は `uv run transcribe web` で起動、http://localhost:8000 でアクセス
- 外部公開する場合は `--host 0.0.0.0`（セキュリティリスクあり、チーム共有時は Next.js 分離構成を推奨）

---

## 11. 直近のコミット履歴

- `feat: Web UI を追加（FastAPI + React）`
- `feat: m4a ファイルの文字起こし対応と convert コマンド追加`
- `feat: LLM による自動カスタムまとめ生成機能を追加`
- `feat: Google Docs 同期機能を追加`
- `feat: ローカル MP3 ファイルの文字起こしに対応`
- `chore: 用語辞書に新規誤認識パターンを追加（5回目）`
