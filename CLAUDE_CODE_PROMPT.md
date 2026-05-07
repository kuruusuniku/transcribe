# YouTube動画 自動文字起こしツール 構築指示書

このドキュメントはClaude Codeに渡す実装指示書です。要件定義・設計はすでに完了しており、Claude Codeは以下の仕様に従って実装を進めてください。

---

## 0. はじめに（Claude Codeへ）

実装を始める前に、以下を**必ず**実行してください。

1. このドキュメント全体を読んで全体像を把握する
2. 実行環境の確認（OS, Python, CUDA, ffmpeg, GPU VRAM）
3. **不明点・矛盾点・代替案があれば、コードを書く前にユーザーに質問する**
4. 段階的にコミット可能な単位で実装を進める（一気に全部書かない）

このプロジェクトは**夜間バッチで動かして翌朝に結果を見る**運用です。途中で落ちずに完走することが最優先です。性能・精度はその次です。

---

## 1. プロジェクト概要

### 目的

YouTube限定公開動画（武術稽古指導の録画）をローカルで自動文字起こしするツール。指導者が生徒に教える音声を、BGMが乗った状態でも正確に書き起こし、独自用語にも対応する。

### 運用シナリオ

- 1日1本、約1時間の動画を処理
- ユーザーが寝る前にURLを `urls.txt` に追記して実行 → 翌朝結果を確認
- 処理中にエラーが出ても次の動画に進む。失敗ログは翌朝の手動判断に回す

### 初期スコープ（**今回実装する範囲**）

- YouTube URL（限定公開含む）から音声をダウンロード
- ローカルで文字起こし（faster-whisper, GPU）
- タイムスタンプ + YouTube該当時刻リンク付きMarkdownを出力
- 構造化データ（JSON）も同時出力（将来の連携用）
- SQLiteで状態管理（レジューム可能）

### 将来スコープ（**今回は実装しない**）

- Google Docs同期
- Notion連携
- 自動カスタムまとめ生成
- Web UI

これらは将来追加できるよう**インターフェースだけは意識**しておく（出力アダプタ層を分離するなど）が、コード本体には含めない。

---

## 2. 実行環境

### ハードウェア

- OS: Windows 11
- CPU: Intel Core i7-8750H (6コア/12スレッド)
- メモリ: 24GB
- GPU: NVIDIA GeForce GTX 1050 Ti with Max-Q Design
- **VRAM: 4GB（最大の制約）**

### ソフトウェア

- Python: 3.13.x（実行前にバージョン確認すること）
- パッケージ管理: **uv** を使用
- ffmpeg: 別途インストール（`winget install Gyan.FFmpeg` 等を推奨。または `imageio-ffmpeg` でPython管理）

### 重要な制約

**VRAM 4GBで faster-whisper large-v3 を動かす**必要があるため、量子化設定が必須です。
- `compute_type="int8_float16"` を使用
- Demucsとwhisperの**同時GPUロードは不可**。Demucs完了→`torch.cuda.empty_cache()`→Whisperロード、の順で動かすこと

---

## 3. 技術スタック（確定済み）

| 用途 | ライブラリ | バージョン方針 |
|---|---|---|
| 音声DL | yt-dlp | 最新安定版 |
| 音声分離（オプション） | demucs | 最新安定版 |
| 文字起こし | faster-whisper | 最新安定版（large-v3対応版） |
| GPU推論 | ctranslate2 + CUDA | faster-whisperの推奨組み合わせ |
| 設定ファイル | PyYAML | - |
| 状態管理 | SQLite (stdlib) | - |
| ロギング | logging (stdlib) + Rich | Richで進捗表示 |
| CLI | Click または Typer | お任せ |

**重要**: Python 3.13系で torch/ctranslate2/faster-whisper/demucs の依存関係が解決できるか、まず `uv` で空のプロジェクト作って`uv add` で確認すること。問題があればユーザーに報告し、Python 3.12 への切り替えなど代替案を提示。

---

## 4. ディレクトリ構成

```
transcribe/
├── pyproject.toml            # uv管理
├── README.md                 # セットアップ手順・使い方
├── config.yaml               # ユーザー設定
├── glossary.yaml             # 用語辞書
├── urls.txt                  # 処理対象URL（1行1URL、#でコメント可）
├── .gitignore
├── data/
│   ├── state.db              # SQLite
│   ├── work/                 # 一時ファイル（音声、分離後音声）
│   └── output/
│       └── {YYYY-MM-DD}_{video_id}/
│           ├── transcript.md
│           ├── segments.json
│           └── meta.json
├── logs/
│   └── {YYYY-MM-DD}.log
└── src/
    └── transcribe/
        ├── __init__.py
        ├── __main__.py       # python -m transcribe で実行可能に
        ├── cli.py            # CLIエントリ
        ├── config.py         # 設定ロード・バリデーション
        ├── pipeline.py       # 全体オーケストレーション
        ├── state.py          # SQLite操作
        ├── stages/
        │   ├── __init__.py
        │   ├── downloader.py
        │   ├── separator.py
        │   ├── transcriber.py
        │   └── formatter.py
        ├── postprocess.py    # 用語置換・信頼度判定
        └── utils.py          # 時刻フォーマット等
```

---

## 5. 設定ファイル仕様

### 5.1 `config.yaml`（初期テンプレート）

```yaml
paths:
  work_dir: ./data/work
  output_dir: ./data/output
  state_db: ./data/state.db
  log_dir: ./logs

youtube:
  # ブラウザCookieを使用（限定公開動画用）
  # chrome / firefox / edge / brave のいずれか
  cookies_from_browser: chrome

audio_separation:
  # ボーカル分離（BGM除去）。精度↑だが時間↑
  enabled: false
  model: htdemucs           # demucs のモデル名
  device: cuda

transcription:
  model: large-v3
  compute_type: int8_float16  # VRAM 4GB制約のため必須
  device: cuda
  language: ja
  beam_size: 5
  vad_filter: true            # 無音スキップ＆幻覚低減
  vad_parameters:
    min_silence_duration_ms: 500
  # initial_prompt は glossary.yaml から自動生成

output:
  timestamp_interval_seconds: 60   # 1分ごとにセクション分け
  confidence_threshold: -1.0        # avg_logprob がこれ未満なら ⚠️マーク
                                    # faster-whisperのavg_logprobは対数尤度（負値）
                                    # -1.0 が目安、調整可能

retry:
  max_attempts: 3
  backoff_seconds: [60, 300, 900]   # 1分→5分→15分

logging:
  level: INFO                       # DEBUG/INFO/WARNING/ERROR
  console: true
  file: true
```

### 5.2 `glossary.yaml`（初期テンプレート）

```yaml
# 1. Whisperのinitial_promptに注入される語彙ヒント
#    ※ initial_promptは244トークンが上限。長すぎる場合は冒頭から切り詰められる
context: |
  これは武術・身体技法の稽古指導の録画です。指導者が生徒に対して
  身体の使い方、立ち方、構え、呼吸法などを解説しています。
  以下の用語が頻出します:
  中足靭帯、臍下丹田、中丹田、八つの心得、膝ぐにゃ、足裏球体、
  スネO脚、O脚、ヒップヒンジ、鼠径部一の型、陰練、アイソレ、体癖、
  肘締めコンパス、ボディヒアリング、体癖ダンス、
  閉じ蹲踞、開き蹲踞、中蹲踞。

# 2. 後処理での置換ルール（誤認識パターン → 正しい表記）
#    type: literal は単純置換、regex は正規表現
substitutions:
  # 例: Whisperが「セイカタンデン」と書きそうな場合
  # - pattern: "せいか丹田"
  #   replacement: "臍下丹田"
  #   type: literal
  
  # 初期は空。運用しながら追加していく
  []

# 3. 重要語リスト（信頼度判定の参考に使用）
#    これらの語を含むセグメントで信頼度が低い場合、特に強く警告
important_terms:
  - 中足靭帯
  - 臍下丹田
  - 中丹田
  - 八つの心得
  - 膝ぐにゃ
  - 足裏球体
  - スネO脚
  - O脚
  - ヒップヒンジ
  - 鼠径部一の型
  - 陰練
  - アイソレ
  - 体癖
  - 肘締めコンパス
  - ボディヒアリング
  - 体癖ダンス
  - 閉じ蹲踞
  - 開き蹲踞
  - 中蹲踞
```

### 5.3 `urls.txt` フォーマット

```
# コメント行は # で始まる
# 1行1URL
https://www.youtube.com/watch?v=xxxxxxxxxxx
https://www.youtube.com/watch?v=yyyyyyyyyyy
```

---

## 6. SQLite スキーマ

```sql
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL UNIQUE,
    video_id        TEXT,
    title           TEXT,
    status          TEXT NOT NULL,    -- queued/downloading/separating/
                                      -- transcribing/formatting/done/failed
    retry_count     INTEGER DEFAULT 0,
    error_message   TEXT,
    output_dir      TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
```

ステータス遷移は `pipeline.py` で一元管理。途中で落ちた場合は次回起動時に `status NOT IN ('done', 'failed')` のジョブを拾って再開。

---

## 7. モジュール責務

### `cli.py`
- `transcribe run` … `urls.txt` を読んで未処理ジョブを処理
- `transcribe status` … 現在の状態を表示
- `transcribe retry <id>` … 失敗ジョブを手動再投入
- `transcribe clean` … `data/work` の一時ファイル掃除

### `pipeline.py`
- 全体のステージ実行を司る
- 各ステージ前後でstateを更新
- リトライロジック（指数バックオフ）
- VRAM管理（Demucs後にempty_cache）

### `stages/downloader.py`
- yt-dlp ラッパ
- `cookies_from_browser` を使用
- m4a/mp3で音声のみDL（最高品質）
- 動画タイトル・video_id・URLを返す

### `stages/separator.py`
- demucs ラッパ（オプション。`config.audio_separation.enabled=false` ならno-op）
- 入力音声 → ボーカル成分のみの音声を返す

### `stages/transcriber.py`
- faster-whisper ラッパ
- glossary.yamlの`context`を`initial_prompt`に注入
- セグメントごとに `start, end, text, avg_logprob, no_speech_prob` を取得
- ジェネレータで返してメモリ抑制

### `postprocess.py`
- glossary.yamlの`substitutions`を順次適用
- `important_terms`を含むセグメントで`avg_logprob`が低いものを警告対象としてマーク
- `confidence_threshold`未満のセグメントもマーク

### `stages/formatter.py`
- セグメント列 → Markdown / JSON
- タイムスタンプは1分ごとにセクション見出し
- YouTube該当時刻リンクは `https://www.youtube.com/watch?v={id}&t={seconds}s` 形式
- 低信頼セグメントには `⚠️[要確認]` を本文中に挿入

### `state.py`
- SQLite操作の薄いラッパ
- `get_pending_jobs()`, `update_status()`, `record_error()` 等

### `utils.py`
- `format_timestamp(seconds) -> "HH:MM:SS"` または `"MM:SS"`
- `youtube_url_with_timestamp(video_id, seconds)`
- 設定/パスのヘルパ

---

## 8. 出力仕様

### 8.1 `transcript.md`

```markdown
# {動画タイトル}

| | |
|---|---|
| 動画 | https://www.youtube.com/watch?v={video_id} |
| 録画日 | {YYYY-MM-DD}（YouTube公開日） |
| 文字起こし日 | {YYYY-MM-DD HH:MM} |
| 音声分離 | 有効 / 無効 |
| モデル | faster-whisper large-v3 (int8_float16) |
| 要確認セグメント数 | {N}箇所 |

---

## [00:00](https://www.youtube.com/watch?v={id}&t=0s)

今日はまず構えから入っていきます。…

## [01:00](https://www.youtube.com/watch?v={id}&t=60s)

腰の落とし方が大事で、ここを意識すると ⚠️[要確認: 低信頼] その後の動きが…

## [02:00](https://www.youtube.com/watch?v={id}&t=120s)

…
```

### 8.2 `segments.json`

```json
{
  "video_id": "xxx",
  "url": "https://...",
  "title": "...",
  "transcribed_at": "2026-05-05T03:42:00+09:00",
  "config": {
    "model": "large-v3",
    "audio_separation": false
  },
  "segments": [
    {
      "start": 0.0,
      "end": 4.32,
      "text": "今日はまず構えから入っていきます。",
      "avg_logprob": -0.21,
      "no_speech_prob": 0.01,
      "low_confidence": false,
      "youtube_link": "https://www.youtube.com/watch?v=xxx&t=0s"
    }
  ]
}
```

### 8.3 `meta.json`

ジョブ全体のメタデータ（処理時間、エラー、設定スナップショット）。デバッグ用。

---

## 9. 実装上の重要事項（**必読**）

### 9.1 VRAMが致命的に少ない

- Whisper large-v3 は int8_float16 でも約3GB使う
- Demucs（htdemucs）は約2GB使う
- **同時ロードは100%OOMになる**
- Demucs実行 → モデルを `del` → `gc.collect()` → `torch.cuda.empty_cache()` → Whisperロード、の順を厳守
- Whisperモデルは1ジョブ完了ごとに解放するか、次ジョブまで保持するかは**保持**でOK（複数動画連続処理時の起動コスト削減）

### 9.2 yt-dlpのCookie認証

```python
ydl_opts = {
    "cookiesfrombrowser": ("chrome",),  # tupleで指定
    # ...
}
```

注意: 対象ブラウザは**完全に終了している**必要がある（Chromeはロックを取る）。エラー時はその旨をログに明示。

### 9.3 ハルシネーション対策

- `vad_filter=True` を必ず有効化（無音区間の幻覚を削減）
- BGMだけが続く区間で「ご視聴ありがとうございました」「字幕は〇〇が作成しました」などが出やすい。`important_terms`に該当しないこれらのフレーズは**警告対象とは別に**注意喚起してもいい

### 9.4 タイムスタンプ粒度

- セグメント単位（数秒〜30秒）の細かいstart/endはJSONに全部入れる
- Markdownの**見出し**は1分ごと（`timestamp_interval_seconds`）
- 各見出し配下に、その分に属するセグメントのテキストを連結して表示
- 1分の境界をまたぐセグメントはstart時刻側の見出しに含める

### 9.5 リトライ

- ステージ単位ではなく**ジョブ単位**でリトライ
- どのステージで落ちたかは記録するが、リトライ時は最初からやり直し（中間ファイルがあれば再利用してもいい）
- バックオフは `time.sleep` で素直に

### 9.6 ログ

- コンソールはRichで進捗可視化（プログレスバー）
- ファイルログは日付別、INFO以上を記録
- 失敗時は traceback もファイルに残す

### 9.7 Windowsパス

- すべて `pathlib.Path` で扱う
- 文字列連結禁止
- 日本語ファイル名を含む可能性があるので encoding に注意（特にログ書き込み時 `encoding="utf-8"`）

---

## 10. セットアップ手順（READMEに記載すること）

```bash
# 1. uvインストール（未導入の場合）
#    PowerShell: irm https://astral.sh/uv/install.ps1 | iex

# 2. 依存関係インストール
uv sync

# 3. ffmpegインストール（未導入の場合）
winget install Gyan.FFmpeg

# 4. CUDA Toolkit確認（NVIDIA driver経由でCUDA 12.x対応のはず）
nvidia-smi

# 5. ctranslate2のCUDA対応確認
uv run python -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"
# 1以上が出ればOK。0なら CPU fallback になる

# 6. 設定ファイル準備
# config.yaml と glossary.yaml は同梱のテンプレートをそのまま使えばOK

# 7. URL追加
echo https://www.youtube.com/watch?v=xxx >> urls.txt

# 8. 実行
uv run transcribe run
```

---

## 11. 動作確認

実装完了後、以下を検証:

1. **空のurls.txtで実行** → エラーなく終了し「処理対象なし」とログ
2. **存在しないURLで実行** → 3回リトライ後 `failed` ステータスで次に進む
3. **短い公開動画（30秒程度）で実行** → 成功し、Markdownが正しく生成される
4. **同じURLで再実行** → 既に `done` ならスキップ
5. **`audio_separation.enabled: true` で実行** → Demucsが動き、Whisperと併存しない（VRAMログで確認）
6. **失敗ジョブを手動retry** → 再実行できる

---

## 12. やってはいけないこと

- ❌ 元のプロトタイプ（spreadsheet/google docs連携）のコードを残すこと → **完全に作り直し**
- ❌ openai-whisper を使うこと → **faster-whisperのみ**
- ❌ Whisperモデルを `compute_type="float16"` でロード → VRAM不足
- ❌ Demucsとwhisperを同時にGPUに載せる
- ❌ 全セグメントをメモリに展開してから書き出す → ジェネレータで流す
- ❌ Google Docs/Notion/Sheets の処理を入れる → **将来スコープ**
- ❌ `os.path.join` での文字列連結 → `pathlib.Path` を使う
- ❌ ハードコードされた設定値 → すべて `config.yaml` 経由
- ❌ try/except でエラーを握りつぶす → 必ずログに出して状態DBに記録

---

## 13. 段階的実装の推奨順序

一気に全部書かず、以下の順で実装してコミットしていくこと。各段階で動作確認できるように。

1. **基盤**: pyproject.toml, ディレクトリ作成, config/glossary読み込み, ロギング, SQLite初期化
2. **CLI骨格**: `transcribe run` がurlsを読んでstateに登録するだけ。実処理はstub
3. **Downloader**: 音声DLのみ動作。手動で動画URLを渡してテスト
4. **Transcriber**: DLした音声をWhisperで文字起こし。生のセグメントを出力
5. **Formatter**: セグメント → Markdown/JSON
6. **Postprocess**: 用語置換・信頼度マーク
7. **Pipeline統合**: 全ステージ通しで動かす
8. **Separator**: Demucsオプション追加
9. **エラーハンドリング・リトライ**: 各種異常系
10. **README完成**: セットアップ・使い方

---

## 14. 質問してほしいこと

実装中に判断に迷ったら、勝手に決めず以下のような点を質問してください:

- 依存パッケージのバージョン整合が取れない場合
- Python 3.13で動かない依存がある場合
- VRAM不足で量子化設定を変える必要がある場合
- 「どっちでもよさそう」だが影響が大きい設計判断
- このドキュメントと矛盾する要件を見つけた場合

逆に、以下は勝手に決めてOK:

- 関数名・変数名の細部
- ログメッセージの文言
- プログレスバーの見た目
- 内部的なヘルパ関数の構成
- コードコメントの書き方

---

以上です。不明点があれば質問してから着手してください。
