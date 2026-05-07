# YouTube動画 自動文字起こしツール

武術稽古指導の録画（YouTube限定公開）をローカルでGPU文字起こしするツール。

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

### 4. 設定ファイル確認

`config.yaml` と `glossary.yaml` は同梱テンプレートをそのまま使用できます。  
ブラウザ Cookie を使う場合は `config.yaml` の `youtube.cookies_from_browser` を確認してください。

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

### 失敗ジョブを手動リトライ

```powershell
uv run transcribe retry 3   # ID=3 のジョブを再実行
```

### 一時ファイル掃除

```powershell
uv run transcribe clean
```

## 出力形式

処理完了後、`data/output/{YYYY-MM-DD}_{video_id}/` に以下が生成されます：

| ファイル | 内容 |
|---|---|
| `transcript.md` | タイムスタンプ付き Markdown（YouTube リンク付き） |
| `segments.json` | 全セグメント（start/end/text/信頼度）の JSON |
| `meta.json` | 処理メタデータ（設定スナップショット等） |

低信頼度セグメントには `⚠️[要確認: 低信頼]` マークが付きます。

## 設定

### config.yaml 主要項目

| 項目 | デフォルト | 説明 |
|---|---|---|
| `youtube.cookies_from_browser` | `chrome` | Cookie取得元ブラウザ（chrome/firefox/edge/brave） |
| `audio_separation.enabled` | `false` | Demucs による BGM 除去（時間がかかる） |
| `transcription.model` | `large-v3` | Whisper モデル |
| `transcription.compute_type` | `int8_float16` | VRAM 4GB 制約のため必須 |
| `output.confidence_threshold` | `-1.0` | これ未満の avg_logprob に ⚠️ を付ける |

### glossary.yaml

- `context`: Whisper の `initial_prompt` に注入するコンテキスト文（語彙ヒント）
- `substitutions`: 誤認識パターンの置換ルール（運用しながら追加）
- `important_terms`: 信頼度チェックを強化する重要語リスト

## 技術仕様

- **モデル**: faster-whisper large-v3 (int8_float16)
- **GPU**: CUDA 12.4 対応 (torch 2.6.0+cu124)
- **VRAM 制約**: 4GB のため Demucs と Whisper の同時ロード禁止
- **レジューム**: SQLite (`data/state.db`) で状態管理。中断しても次回起動で再開
- **リトライ**: 失敗ジョブは最大3回、指数バックオフ（1分→5分→15分）で再試行

## 動作確認チェックリスト

1. 空の urls.txt で実行 → 「処理対象URLなし」でエラーなく終了
2. 存在しないURLで実行 → 3回リトライ後 `failed` ステータス
3. 短い公開動画（30秒程度）で実行 → Markdown が正しく生成
4. 同じURLで再実行 → `done` ならスキップ
5. `audio_separation.enabled: true` で実行 → Demucs → VRAM解放 → Whisper の順で動作
6. 失敗ジョブを `transcribe retry <id>` → 再実行
