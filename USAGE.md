# USAGE — リファレンス

> はじめて使う場合は [GETTING_STARTED.md](GETTING_STARTED.md)、うまく動かない場合は [TROUBLESHOOTING.md](TROUBLESHOOTING.md) を見てください。
> このファイルは Web UI・コマンド・設定の詳細なリファレンスです。

## 日常操作チートシート

```
# Web UI を起動（普段はこれだけ）
uv run transcribe web

# 設定の診断
uv run transcribe doctor [--test]

# urls.txt を一括処理（夜間バッチ向け）
uv run transcribe run

# ローカル音声ファイルを処理
uv run transcribe file path/to/audio.mp3

# ジョブ状況確認（--id でステージ別）
uv run transcribe status [--id <id>]

# 失敗したところから再開 / 後処理だけやり直す
uv run transcribe retry <id>
uv run transcribe resume-post <id>
```

---

## Web UI

`uv run transcribe web` で http://localhost:8000 を起動する。

### 起動

```
uv run transcribe web
uv run transcribe web --port 9000
uv run transcribe web --host 0.0.0.0  # LAN内に公開（web.token の設定が必須）
```

### 画面構成

| 場所 | 内容 |
|---|---|
| ヘッダー | ＋追加 / 処理中のジョブと進捗・待ち件数 / 用語辞書 / ツール / 設定状況 / 使い方 / テーマ切替 |
| 左：ジョブ一覧 | 「要対応 / 処理中 / 完了 / すべて」のタブと検索。各行に状態・まとめ/Notion/Docs の結果・要確認箇所数・進捗バー |
| 右：メイン | 追加画面、またはジョブ詳細・用語辞書・ツール・設定状況 |
| 下：詳細ログ | クリックで開閉。全タスクのログをリアルタイム表示（境界をドラッグで高さ調整） |

### 主な操作

| やりたいこと | 操作 |
|---|---|
| 動画・音声を処理する | ＋追加 → URL を貼る / mp3・m4a をドロップ →「追加して開始」 |
| 進み具合を見る | 左の「処理中」タブ、またはヘッダーの表示（クリックでそのジョブを開く） |
| 失敗したジョブを直す | 「要対応」→ ジョブを開く →「失敗したところから再開」 |
| まとめ・Notion・Docs の失敗を直す | 「要対応」→ ジョブを開く →「後処理をやり直す」 |
| まとめ・文字起こしを見る | ジョブを開く →「まとめ」/「文字起こし」タブ（タイムスタンプは元動画へのリンク） |
| 誤認識を直す | 「文字起こし」タブで言葉を選択 →「用語辞書に登録」（その場で反映＋次回から自動修正） |
| 文字起こしを手で直す | 「文字起こし」タブ →「直接編集」→「保存」 |
| まとめを作り直す | ジョブ詳細「その他 → まとめを作り直す」 |
| 最初からやり直す / 削除 | ジョブ詳細「その他」メニュー（確認あり） |
| 処理の段階ごとの結果を見る | ジョブ詳細「処理の記録」タブ |
| 用語辞書を編集 | ヘッダー「用語辞書」→ 編集 →「保存」 |
| 一括でまとめ・Notion・Docs 登録 | ヘッダー「ツール」→ 一括処理（未処理分 / 全件） |
| 一時ファイル削除 | ヘッダー「ツール」→ 一時ファイルを削除 |
| m4a → mp3 変換して手元に保存 | ヘッダー「ツール」→ ファイル変換（保存先フォルダは次回も維持） |
| 設定が正しいか確認 | ヘッダー「設定状況」（接続テストあり） |

> **m4a → mp3 変換**: File System Access API を使用するため Brave / Chrome / Edge が必要。
> `brave://flags/#file-system-access-api` を Enabled にすること。
> ダウンロードフォルダ等の保護フォルダは選択不可（専用サブフォルダを作成して選択する）。
> 文字起こしが目的なら変換は不要（m4a のまま追加できる）。

### 処理の流れ

Web UI から投入したコマンドは、サーバー内の 1 本のワーカーで**順番に**実行される（同時に 2 件は走らない）。
Whisper モデルはタスク間で再利用され、10 分間処理がなければ解放される。

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
| `--insecure` | トークン未設定のまま 127.0.0.1 以外で公開する（非推奨） |

起動後 http://localhost:8000 をブラウザで開く。ログのリアルタイム確認、mp3/m4a アップロード、transcript.md / summary.md のインライン閲覧が可能。

**アクセス制御**

- 他サイトからのリクエスト（Origin が一致しないもの）は常に拒否される。
- `config.yaml` の `web.token`（または環境変数 `TRANSCRIBE_WEB_TOKEN`）を設定するとトークン認証が有効になる。初回は `http://<host>:<port>/?token=<token>` で開くと Cookie が発行され、以降はトークンなしで使える。
- `--host` が `127.0.0.1` 以外の場合、トークン未設定だと起動しない。
- Web UI からアップロードしたファイルは `data/uploads/` に保存され、retry / rerun に使われる（自動削除されない）。

**例**

```
uv run transcribe web
uv run transcribe web --port 9000
uv run transcribe web --host 0.0.0.0 --port 8000   # LAN 内に公開（web.token の設定が必須）
```

---

### `doctor`

設定と外部連携（ffmpeg / まとめ / Google Docs / Notion / メール / 一時ファイル容量）の状態を診断する。問題があれば終了コード 1。

```
uv run transcribe doctor          # 設定内容の確認のみ
uv run transcribe doctor --test   # Notion / AI に実際に接続して確認（料金の発生しない API のみ）
```

---

### `resume-post`

文字起こし完了済みのジョブについて、失敗・未実行の後処理（まとめ → Google Docs → Notion）だけをやり直す。

```
uv run transcribe resume-post <id>
```

---

### `status`

全ジョブの状態一覧を表示する。`--id` を付けるとそのジョブのステージ別の状態（試行回数・エラー）を表示する。

```
uv run transcribe status
uv run transcribe status --id 12
```

ステージは `download` / `separate` / `transcribe` / `format`（本体）と `summarize` / `docs_sync` / `notion_sync`（後処理）。
後処理の失敗はジョブを failed にせず、ステージに `failed` として記録される（バッチ結果メールにも表示）。
文字起こし後の段階で失敗してリトライする場合は、work_dir の文字起こしキャッシュを再利用して Whisper を再実行しない。

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

[TROUBLESHOOTING.md](TROUBLESHOOTING.md) を参照。

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
