# USAGE — コマンドリファレンス

## 日常操作チートシート

```
# 一括処理（urls.txt → 文字起こし → まとめ → Docs 同期）
uv run transcribe run

# ローカル音声ファイルを処理
uv run transcribe file path/to/audio.mp3

# ジョブ状況確認
uv run transcribe status

# 失敗ジョブをリトライ
uv run transcribe retry <id>

# Web UI 起動
uv run transcribe web
```

---

## コマンド詳細

### `run`

`urls.txt` に記載された URL/ファイルパスをまとめて処理する。文字起こし → まとめ生成 → Google Docs 同期 → Notion 同期を順に実行。

```
uv run transcribe run
```

- `urls.txt` に YouTube URL またはローカルファイルパスを 1 行ずつ記載する
- 処理済みの URL はスキップされる（`--force` なし時）
- `config.yaml` の `summarize.enabled` / `google_docs.enabled` / `notion.enabled` で各ステップの自動実行を制御

---

### `file`

ローカル音声ファイルを直接文字起こしする。

```
uv run transcribe file <path>
```

| 引数 | 説明 |
|---|---|
| `<path>` | mp3 または m4a ファイルのパス（絶対パス・相対パス両可） |

**例**

```
uv run transcribe file C:\recordings\keiko.mp3
uv run transcribe file ./data/audio/session.m4a
```

---

### `convert`

m4a ファイルを mp3 に変換する。

```
uv run transcribe convert <path> [--force]
```

| オプション | 説明 |
|---|---|
| `--force` | 出力先に同名 mp3 が存在しても上書きする |

**例**

```
uv run transcribe convert C:\recordings\session.m4a
uv run transcribe convert C:\recordings\session.m4a --force
```

---

### `summarize`

文字起こし済みジョブに対して LLM でまとめを生成する。

```
uv run transcribe summarize [--all | --id N]
```

| オプション | 説明 |
|---|---|
| （なし） | まだまとめがない `done` ジョブのみ対象 |
| `--all` | すべての `done` ジョブを対象（既存まとめを上書き） |
| `--id N` | ジョブ ID を指定して単体実行 |

**例**

```
uv run transcribe summarize
uv run transcribe summarize --id 13
uv run transcribe summarize --all
```

---

### `sync`

Google Docs にまとめを同期する。

```
uv run transcribe sync [--all | --id N]
```

| オプション | 説明 |
|---|---|
| （なし） | 未同期のジョブのみ対象 |
| `--all` | すべての `done` ジョブを対象（既存ドキュメントを更新） |
| `--id N` | ジョブ ID を指定して単体実行 |

事前に `config.yaml` の `google_docs.enabled: true` とフォルダ ID の設定が必要。

---

### `sync-notion`

Notion データベースにまとめを同期する。`summary.md` が存在するジョブのみ対象。

```
uv run transcribe sync-notion [--all | --id N]
```

| オプション | 説明 |
|---|---|
| （なし） | 未同期のジョブのみ対象 |
| `--all` | すべての `done` ジョブを対象（既存ページを更新） |
| `--id N` | ジョブ ID を指定して単体実行 |

事前に `config.yaml` の `notion.token` / `notion.database_id` の設定と `notion.enabled: true` が必要。

---

### `web`

ブラウザから操作できる Web UI を起動する。

```
uv run transcribe web [--host HOST] [--port PORT]
```

| オプション | 説明 |
|---|---|
| `--host HOST` | バインドアドレス（デフォルト: `127.0.0.1`） |
| `--port PORT` | ポート番号（デフォルト: `8000`） |

起動後 http://localhost:8000 をブラウザで開く。ログのリアルタイム確認、mp3/m4a アップロード、transcript.md / summary.md のインライン閲覧が可能。

**例**

```
uv run transcribe web
uv run transcribe web --port 9000
uv run transcribe web --host 0.0.0.0 --port 8000   # LAN 内に公開（注意）
```

---

### `status`

全ジョブの状態一覧を表示する。

```
uv run transcribe status
```

各行に ID / ステータス / タイトル / 更新日時が表示される。ステータスは `queued` / `downloading` / `separating` / `transcribing` / `formatting` / `done` / `failed` のいずれか。

---

### `retry`

失敗したジョブを手動でリトライする。`failed` 状態のジョブを `pending` に戻して再キューに積む。

```
uv run transcribe retry <id>
```

| 引数 | 説明 |
|---|---|
| `<id>` | `status` で確認したジョブ ID |

**例**

```
uv run transcribe retry 7
```

---

### `rerun`

完了済み（または失敗済み）のジョブを最初から再実行する。文字起こしからやり直す。

```
uv run transcribe rerun <url_or_id>
```

| 引数 | 説明 |
|---|---|
| `<url_or_id>` | YouTube URL またはジョブ ID |

**例**

```
uv run transcribe rerun 5
uv run transcribe rerun https://www.youtube.com/watch?v=abc123
```

`retry` との違い: `retry` は前回の途中状態から再開を試みる。`rerun` は出力を削除してゼロから再実行する。

---

### `delete`

ジョブを DB から削除する。

```
uv run transcribe delete <id> [--files]
```

| 引数/オプション | 説明 |
|---|---|
| `<id>` | 削除するジョブ ID |
| `--files` | DB レコードの削除に加えて `data/output/` 以下の出力ディレクトリも削除する |

`--files` を指定した場合は削除前に確認プロンプトが表示される。transcript.md / summary.md / segments.json など出力ファイルがすべて消えるため注意。

**例**

```
# DB レコードのみ削除
uv run transcribe delete 3

# 出力ファイルも含めて削除（確認あり）
uv run transcribe delete 3 --files
```

---

### `clean`

作業中の一時ファイル（`data/work/` 以下）を削除する。

```
uv run transcribe clean
```

失敗したジョブが残した中間ファイルや、変換後の wav ファイルなどを一括削除する。出力済みの transcript.md / summary.md は削除されない。

---

## よくあるトラブルと対処

### Firefox を終了し忘れた場合

`yt-dlp` は Firefox の Cookie データベースを直接読み取るため、Firefox 起動中はロックが発生しダウンロードに失敗する。

```
# Firefox を完全終了してから再実行
uv run transcribe retry <id>
```

または Web UI から該当ジョブをリトライする。

---

### Gemini 503 エラー

Gemini 無料枠の高負荷時に発生する。自動リトライ（最大 3 回、バックオフ 60s / 300s / 900s）が設定されているため、しばらく待てば自動回復することが多い。

手動で対処する場合:

```
uv run transcribe retry <id>
```

頻発する場合は `config.yaml` の `summarize.provider: claude` に切り替えて Anthropic API を使用する。

---

### CUDA out of memory

`config.yaml` の `transcription.compute_type` を下げる。

| GPU VRAM | 推奨設定 |
|---|---|
| 4GB 前後（旧世代） | `int8` |
| 4–6GB（Tensor Core 対応） | `int8_float16` |
| 8GB+ | `float16` |
| 12GB+ | `float32` |

また `audio_separation.enabled: false`（デフォルト）を確認する。Demucs は VRAM を大量消費する。

---

### Google Docs 初回認証

`credentials.json` をプロジェクトルートに配置した後、初回実行時にブラウザが自動起動して OAuth 認証画面が表示される。認証完了後 `token.json` が自動生成される。

```
# sync コマンドを実行すると認証フローが起動する
uv run transcribe sync --id <id>
```

サーバー環境など GUI のない場合は `token.json` を別マシンで生成してコピーする。

---

### ジョブが failed になった場合

1. `uv run transcribe status` でエラー内容を確認（またはログファイル `logs/` を参照）
2. 原因を解消（Firefox 終了、ネットワーク確認など）
3. `uv run transcribe retry <id>` でリトライ
4. それでも失敗する場合は `uv run transcribe rerun <id>` で最初から再実行

---

### Notion 同期が失敗する場合

以下を順に確認する:

1. `notion.token` が正しい Internal Integration Secret か
2. `notion.database_id` が 32 桁英数字（URL の末尾部分）か
3. Notion データベースページの「...」→「接続」からインテグレーションが接続済みか
4. DB に必要なプロパティ（名前 / 日付 / URL / 動画時間 / ソース種別 / まとめ進捗 / タグ）がすべて存在するか

手動で再同期:

```
uv run transcribe sync-notion --id <id>
```

---

## 設定ファイルリファレンス

### `paths`

| キー | デフォルト | 説明 |
|---|---|---|
| `work_dir` | `./data/work` | ダウンロード・変換中の一時ファイル置き場 |
| `output_dir` | `./data/output` | 最終出力（transcript.md 等）の親ディレクトリ |
| `state_db` | `./data/state.db` | ジョブ状態管理 SQLite DB |
| `log_dir` | `./logs` | ログファイルの出力先 |

---

### `youtube`

| キー | デフォルト | 説明 |
|---|---|---|
| `cookies_from_browser` | `firefox` | Cookie 取得元ブラウザ。`firefox` / `chrome` / `edge` / `brave` / `none` |

Windows では `firefox` を推奨。Chrome 127+ は App-Bound Encryption の影響で Cookie を読み出せない。

---

### `audio_separation`

| キー | デフォルト | 説明 |
|---|---|---|
| `enabled` | `false` | Demucs によるボーカル分離（BGM 除去）を有効化 |
| `model` | `htdemucs` | Demucs モデル名 |
| `device` | `cuda` | `cuda` または `cpu` |

BGM が小さい動画では `false` 推奨。有効化すると 1 時間動画で約 50 分の処理時間が追加される。

---

### `transcription`

| キー | デフォルト | 説明 |
|---|---|---|
| `model` | `large-v3` | Whisper モデル名 |
| `compute_type` | `int8_float16` | 量子化設定（`int8` / `int8_float16` / `float16` / `float32`） |
| `device` | `cuda` | `cuda` または `cpu` |
| `language` | `ja` | 文字起こし言語 |
| `beam_size` | `5` | ビームサーチ幅。大きいほど高精度だが遅い |
| `condition_on_previous_text` | `false` | 前テキストを文脈に使用（ハルシネーション連鎖防止のため `false` 推奨） |
| `vad_filter` | `true` | 音声区間検出フィルタを有効化 |
| `vad_parameters.min_silence_duration_ms` | `1000` | 無音とみなす最小区間（ms） |
| `vad_parameters.threshold` | `0.5` | 音声検出の閾値（0–1） |
| `vad_parameters.speech_pad_ms` | `200` | 音声区間の前後パディング（ms） |

---

### `output`

| キー | デフォルト | 説明 |
|---|---|---|
| `timestamp_interval_seconds` | `60` | transcript.md に挿入するタイムスタンプの間隔（秒） |
| `confidence_threshold` | `-1.0` | この値を下回るセグメントに `⚠️[要確認: 低信頼]` を付与。`-1.0` は事実上無効 |

---

### `retry`

| キー | デフォルト | 説明 |
|---|---|---|
| `max_attempts` | `3` | 最大リトライ回数 |
| `backoff_seconds` | `[60, 300, 900]` | リトライ間隔（秒）のリスト。回数に応じて順に適用 |

---

### `logging`

| キー | デフォルト | 説明 |
|---|---|---|
| `level` | `INFO` | ログレベル（`DEBUG` / `INFO` / `WARNING` / `ERROR`） |
| `console` | `true` | コンソールへの出力 |
| `file` | `true` | `log_dir` へのファイル出力 |

---

### `google_docs`

| キー | デフォルト | 説明 |
|---|---|---|
| `enabled` | `false` | パイプライン完了時に自動同期 |
| `credentials_path` | `credentials.json` | Google OAuth クライアント認証情報 |
| `token_path` | `token.json` | 認証トークン（自動生成、gitignore 対象） |
| `root_folder_id` | `""` | 同期先 Google Drive フォルダの ID |

---

### `web`

| キー | デフォルト | 説明 |
|---|---|---|
| `host` | `127.0.0.1` | バインドアドレス。外部公開する場合は `0.0.0.0`（セキュリティリスクに注意） |
| `port` | `8000` | ポート番号 |

---

### `notion`

| キー | デフォルト | 説明 |
|---|---|---|
| `enabled` | `false` | パイプライン完了時に自動同期 |
| `token` | `""` | Notion Internal Integration Secret |
| `database_id` | `""` | 同期先 DB の ID（URL 末尾 32 桁英数字） |

DB に必要なプロパティ: `名前 (title)` / `日付 (date)` / `URL (url)` / `動画時間 (number)` / `ソース種別 (select)` / `まとめ進捗 (checkbox)` / `タグ (multi_select)`

---

### `summarize`

| キー | デフォルト | 説明 |
|---|---|---|
| `enabled` | `false` | パイプライン完了時に自動生成 |
| `provider` | `gemini` | 使用 LLM プロバイダー（`gemini` または `claude`） |
| `gemini_api_key` | `""` | Gemini API キー（または環境変数 `GEMINI_API_KEY`） |
| `gemini_model` | `gemini-2.5-flash` | Gemini モデル名 |
| `anthropic_api_key` | `""` | Anthropic API キー（または環境変数 `ANTHROPIC_API_KEY`） |
| `anthropic_model` | `claude-haiku-4-5-20251001` | Claude モデル ID |
| `max_output_tokens` | `8192` | まとめ生成の最大トークン数 |
| `temperature` | `0.3` | 生成温度（構造化まとめには低め推奨） |
