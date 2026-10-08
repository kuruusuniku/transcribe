# YouTube動画 自動文字起こしツール

## 概要

体育指導の YouTube 限定公開動画・叡智講義の録音ファイルを入れておくと、**文字起こし → LLM による構造化まとめ → Notion / Google Docs への登録**まで自動で行うツール。

| 素材 | 扱い | まとめの形式 | Notion の登録先 |
|---|---|---|---|
| YouTube URL | 体育指導の動画 | 体育指導用（メソッド・身体部位・チャプター表） | 体育動画まとめDB（`notion.database_id`） |
| 録音ファイル（mp3 / m4a） | 叡智講義 | 講義用（要旨・概念・叡智の伝統・印象的な言葉・章立て） | 叡智まとめDB（`notion.local_database_id`） |

使う人がやることは「① 入れる → ② 待つ → ③ 要対応だけ確認する」の 3 つだけ。

## ドキュメント

| 読みたいこと | ファイル |
|---|---|
| 使い方（まずはここ） | [GETTING_STARTED.md](GETTING_STARTED.md) |
| うまく動かないとき | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| Web UI・コマンド・設定の詳細 | [USAGE.md](USAGE.md) |
| 開発者向けの構成・経緯 | [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) / [HANDOVER.md](HANDOVER.md) |

## セットアップ

1. **前提**: Python 3.13+, uv, ffmpeg, Firefox
   - Windows: NVIDIA GPU (CUDA)
   - Mac: Apple Silicon（`brew install uv ffmpeg` で入れられる。Python は uv が自動で用意する）
2. **依存インストール**: `uv sync`
3. **設定ファイル作成**: `cp config.example.yaml config.yaml`（PowerShell では `Copy-Item config.example.yaml config.yaml`）
4. **config.yaml の各セクションを環境に合わせて編集**（編集後は `uv run transcribe doctor` で確認）
   - `youtube.cookies_from_browser`: Windows では `firefox`（Chrome 127+ は Cookie 暗号化で不可）
   - `transcription.compute_type`: GTX 1050 Ti 等の古い GPU は `int8`、RTX 系 VRAM 4-6GB は `int8_float16`、8GB+ は `float16`
   - `audio_separation.enabled`: `false` 推奨（後述「実運用での知見」参照）
   - Mac の場合は下の「Mac で使う場合」の設定も必要

### Mac で使う場合

文字起こしエンジン（faster-whisper）は Mac の GPU に対応していないため、CPU で動かします。
`config.yaml` を次のように変更してください（`device: cuda` のままだと文字起こしで
`This CTranslate2 package was not compiled with CUDA support` というエラーになります）。

```yaml
transcription:
  device: cpu
  compute_type: int8      # CPU で使えるのは int8 / int8_float32 / float32

audio_separation:
  device: cpu             # 音声分離を有効にする場合のみ関係する
```

- 文字起こしは GPU の環境より時間がかかります
- `youtube.cookies_from_browser`: Mac には Windows の Chrome の制限（App-Bound Encryption）がないため、
  普段使っているブラウザ（`firefox` / `chrome` / `safari` など）を指定できます

### Google Docs 同期（任意）

1. Google Cloud Console でプロジェクト作成 → Drive API 有効化
2. OAuth 同意画面設定 → クライアント ID 作成 → `credentials.json` をプロジェクトルートに配置
3. Google Drive にルートフォルダ作成 → フォルダ ID を `config.yaml` の `google_docs.root_folder_id` に設定
4. 初回実行時にブラウザ認証 → `token.json` 自動生成

### 自動まとめ（任意）

- Gemini API キー発行: https://aistudio.google.com/apikey
- `config.yaml` の `summarize.gemini_api_key` に設定（または環境変数 `GEMINI_API_KEY`）
- フォールバック用 Claude API キー（任意）: https://console.anthropic.com/

### Notion 連携（任意）

1. Notion でインテグレーション作成 → トークン取得: https://www.notion.so/my-integrations
2. Notion にデータベース作成 → インテグレーションを接続 → データベース ID を取得
3. `config.yaml` の `notion.token` と `notion.database_id` に設定
4. `notion.enabled: true` に設定

## コマンド一覧

| コマンド | 説明 |
|---|---|
| `transcribe run` | `urls.txt` から一括処理（文字起こし → まとめ → Docs 同期） |
| `transcribe file <path>` | 叡智講義などの録音ファイル（mp3/m4a）を文字起こし |
| `transcribe convert <path>` | m4a → mp3 変換（`--force` で上書き） |
| `transcribe summarize [--all \| --id N]` | LLM まとめ生成 |
| `transcribe sync [--all]` | Google Docs 同期 |
| `transcribe rerun <url_or_id>` | 完了済みジョブの再実行 |
| `transcribe retry <id>` | 失敗ジョブを手動でリトライ |
| `transcribe delete <id> [--files]` | ジョブを DB から削除（`--files` で出力ディレクトリも削除、確認あり） |
| `transcribe sync-notion [--all \| --id N]` | Notion データベースに同期 |
| `transcribe web [--host] [--port]` | Web UI を起動（http://localhost:8000） |
| `transcribe status [--id N]` | ジョブ一覧表示（`--id` でステージ別の状態） |
| `transcribe resume-post <id>` | 失敗・未実行の後処理（まとめ / Docs / Notion）だけやり直す |
| `transcribe doctor [--test]` | 設定と外部連携の診断 |
| `transcribe clean` | 一時ファイル削除 |

### Web UI

`uv run transcribe web` で http://localhost:8000 を起動。
「＋ 追加」で URL / 音声ファイルを入れると自動で処理され、左の「要対応」に出たものだけ確認すればよい。
文字起こし画面で誤認識した言葉を選択すると、その場で用語辞書に登録して修正できる。
詳細は [USAGE.md の Web UI](USAGE.md#web-ui) を参照。

## 運用フロー

### 日常運用

- **Web UI**: 「＋ 追加」から URL / ファイルを入れる → 翌朝「要対応」を確認 → 誤認識は文字起こし画面から用語辞書に登録
- **夜間バッチ（CLI）**: `urls.txt` に URL を追加 → `uv run transcribe run`（寝る前）→ 翌朝 Notion / Google Docs と完了メールを確認

### 叡智講義（録音ファイル）

- mp3: `uv run transcribe file path/to/audio.mp3`
- m4a: `uv run transcribe file path/to/audio.m4a`
- m4a を mp3 に変換したい場合: `uv run transcribe convert path/to/audio.m4a`

### 失敗したジョブの対処

- `uv run transcribe status --id <id>` でどの段階で失敗したかを確認
- `uv run transcribe retry <id>` で失敗したところから再開（文字起こし済みなら Whisper は再実行しない）
- まとめ・同期だけ失敗した場合は `uv run transcribe resume-post <id>`
- 解決しない場合は `uv run transcribe rerun <id>` で最初から再実行
- 症状別の対処は [TROUBLESHOOTING.md](TROUBLESHOOTING.md)

### 不要なジョブの削除

- `uv run transcribe delete <id>` で DB レコードのみ削除
- `uv run transcribe delete <id> --files` で出力ディレクトリも含めて削除（確認プロンプトあり）

## 運用上の注意点

- **Firefox 必須**（Chrome 127+ は Cookie 暗号化で使えない）
- 実行前に **Firefox を完全終了**（Cookie ロック回避）
- スリープ無効化推奨: `powercfg /change standby-timeout-ac 0`
- Gemini 無料枠は高負荷時に 503 が出ることがある（自動リトライあり、手動再実行も可）
- Google Docs 初回実行時はブラウザ認証が必要

## 実運用での知見

- **Demucs（音声分離）は本運用では OFF**: BGM 除去が認識精度を逆に悪化させる（人声の微細な音響特徴を削るため）。1 時間動画で約 50 分の処理時間追加、GTX 1050 Ti 環境では GPU 温度 89℃ まで上昇。`audio_separation.enabled: false` を推奨。
- **glossary.yaml を厚くすることで認識精度が向上**: 現在約 93 エントリ。`substitutions` に誤認識パターンを継続追加することで、要確認セグメント数が劇的に減少した（初期 11 件 → 最新 0 件）。
- **出力ディレクトリ**: `data/output/{YYYY-MM-DD}_{video_id}/` に `transcript.md` / `summary.md` / `segments.json` / `meta.json` が生成される。低信頼度セグメントには `⚠️[要確認: 低信頼]` マークが付く。
- **Notion タグ自動抽出**: `sync-notion` 実行時に `summary.md` の「主要キーワード」「メソッド種別」「指導対象の身体部位」セクションからタグを自動抽出して Notion の `タグ` プロパティに反映する。
- **Notion DB 移行スクリプト**: `scripts/patch_notion_db.py --mode copy-blocks` で既存まとめサブページの本文ブロックを DB レコードに一括コピーできる。
