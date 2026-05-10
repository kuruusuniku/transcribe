# YouTube動画 自動文字起こしツール

武術稽古指導の録画（YouTube限定公開）をローカルでGPU文字起こしするツール。

## 前提条件

限定公開動画を扱うため、以下が必要です:

- **Firefox のインストール**（Chrome 127以降は yt-dlp の Cookie 取得が
  App-Bound Encryption により不安定なため、Firefox を推奨）
- Firefox で対象のYouTubeアカウントにログイン済みであること
- 実行時はFirefoxを完全に終了させること（Cookieファイルのロック回避）

## セットアップ

### 1. uv のインストール（未導入の場合）

```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

### 2. 依存関係のインストール

```powershell
uv sync
```

### 3. CUDA 確認

```powershell
# NVIDIA ドライバ / CUDA バージョン確認
nvidia-smi

# ctranslate2 の GPU 認識確認（1以上が出ればOK、0なら CPU フォールバック）
uv run python -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"
```

### 4. 設定ファイルの作成

```powershell
# テンプレートを複製
Copy-Item config.example.yaml config.yaml
```

`config.yaml` を環境に合わせて編集:

- `youtube.cookies_from_browser`:
  - **Windows**: `firefox` を推奨（Chrome 127以降は yt-dlp で Cookie 取得不可）
  - **macOS/Linux**: `chrome` `firefox` どちらも可
- `transcription.compute_type`: GPU 環境に応じて調整
  - GTX 1050 Ti などの古いGPU（Pascal/CC6.1 等、Tensor Core 非対応）: `int8`
  - RTX 20/30/40系（VRAM 4-6GB）: `int8_float16`
  - VRAM 8GB以上: `float16`
  - VRAM 12GB以上: `float32`

`glossary.yaml` は同梱のものをそのまま使用できます。運用しながら用語辞書を育てていく形です。

### 5. URLリストファイルを作成

```powershell
New-Item urls.txt
```

## 使い方

### URL を登録して実行

```powershell
# urls.txt に URL を追記
Add-Content urls.txt "https://www.youtube.com/watch?v=xxxxxxxxxxx"

# 実行（夜間バッチ想定）
uv run transcribe run
```

### ジョブ状態確認

```powershell
uv run transcribe status
```

`done` ジョブの出力ディレクトリも併せて表示されます。

### 失敗ジョブを手動リトライ

```powershell
uv run transcribe retry 3   # ID=3 のジョブを再実行
```

### 完了ジョブの再実行（検証用）

設定変更や用語辞書更新の効果を確認したいとき、`done` ステータスのジョブを再実行できます。

```powershell
# URL または video_id を指定
uv run transcribe rerun "https://youtu.be/xxxxxxxxxxx"
uv run transcribe rerun xxxxxxxxxxx

# 確認プロンプトをスキップ
uv run transcribe rerun xxxxxxxxxxx --yes

# 既存の出力ディレクトリをバックアップせず削除
uv run transcribe rerun xxxxxxxxxxx --no-backup
```

デフォルトでは既存の `data/output/{YYYY-MM-DD}_{video_id}/` を `{...}_backup_{タイムスタンプ}/` にリネームしてから再実行します。

### 一時ファイル掃除

```powershell
uv run transcribe clean
```

## 出力形式

処理完了後、`data/output/{YYYY-MM-DD}_{video_id}/` に以下が生成されます：

| ファイル | 内容 |
|---|---|
| `transcript.md` | タイムスタンプ付き Markdown（YouTube リンク付き） |
| `segments.json` | 全セグメント（start/end/text/信頼度/original_text）の JSON |
| `meta.json` | 処理メタデータ（設定スナップショット等） |

低信頼度セグメントには `⚠️[要確認: 低信頼]` マークが付きます。
連続する同一テキスト（ハルシネーションの典型）は自動圧縮され、`⚠️[同一フレーズ N回繰り返しを検出（自動圧縮）]` マークが付与されます。圧縮前のテキストは `segments.json` の `original_text` フィールドに保持されています。

## 設定

### config.yaml 主要項目

| 項目 | デフォルト | 説明 |
|---|---|---|
| `youtube.cookies_from_browser` | `firefox` | Cookie取得元ブラウザ（firefox推奨、詳細はファイル内コメント参照） |
| `audio_separation.enabled` | `false` | Demucs による BGM 除去（時間がかかる） |
| `transcription.model` | `large-v3` | Whisper モデル |
| `transcription.compute_type` | `int8_float16` | 量子化設定（GPU環境に応じて調整、ファイル内コメント参照） |
| `transcription.condition_on_previous_text` | `false` | 直前テキストへの引きずりを防ぐ（ハルシネーション対策） |
| `transcription.vad_filter` | `true` | 無音区間スキップで幻覚低減 |
| `output.confidence_threshold` | `-1.0` | これ未満の avg_logprob に ⚠️ を付ける |

### glossary.yaml

- `context`: Whisper の `initial_prompt` に注入するコンテキスト文（語彙ヒント）
- `substitutions`: 誤認識パターンの置換ルール（運用しながら追加）
- `important_terms`: 信頼度チェックを強化する重要語リスト

## 技術仕様

- **モデル**: faster-whisper large-v3
- **GPU**: CUDA 12.4 対応 (torch 2.6.0+cu124)
- **量子化**: 環境に応じて選択（GTX 1050 Ti では `int8`、その他は `int8_float16` 推奨）
- **VRAM 制約**: Demucs と Whisper の同時ロード禁止（VRAM 4GB環境では特に重要）
- **レジューム**: SQLite (`data/state.db`) で状態管理。中断しても次回起動で再開
- **リトライ**: 失敗ジョブは最大3回、指数バックオフ（1分→5分→15分）で再試行
- **後処理**: 連続する同一テキストの自動圧縮（ハルシネーション対策）

## 実運用での知見

### Demucs（ボーカル分離）について

本プロジェクトの稽古指導動画では、Demucs による BGM 除去は
**精度向上に寄与しない**ことが検証で判明しました（2026-05-10）。

- 1時間動画で約50分の処理時間が追加で必要
- GPU 温度が長時間高負荷状態（VRAM 4GB 環境では 89℃ まで上昇）
- ボーカル分離が人声の微細な音響特徴を削るため、認識精度がむしろ低下
  - 既知の用語パターン（「おパイセン」「朝活」等）の認識率が低下
  - 数字や子音の認識精度が劣化（例: 「9割9分」→「9割キューブ」）

そのため、`audio_separation.enabled: false` を推奨します。
コードと依存関係は将来の選択肢として残してあります。BGM が大きい
動画ジャンルを扱う場合は、選択的に有効化して使用することも可能です。

## 動作確認チェックリスト

1. 空の urls.txt で実行 → 「処理対象URLなし」でエラーなく終了
2. 存在しないURLで実行 → 3回リトライ後 `failed` ステータス
3. 短い公開動画（30秒程度）で実行 → Markdown が正しく生成
4. 同じURLで再実行 → `done` ならスキップ
5. `transcribe rerun <url>` で再実行 → バックアップ作成後に再処理開始
6. `audio_separation.enabled: true` で実行 → Demucs → VRAM解放 → Whisper の順で動作
7. 失敗ジョブを `transcribe retry <id>` → 再実行

## ファイル管理

以下のファイル/ディレクトリは Git 管理対象外です（`.gitignore` で除外）:

- `config.yaml` — 環境固有の設定（テンプレートは `config.example.yaml`）
- `urls.txt` — 個人の処理対象URLリスト
- `data/output/` — 文字起こし結果（個人データを含む）
- `data/work/` — 一時ファイル
- `data/state.db` — ジョブ管理DB
- `logs/` — 実行ログ

gemini test