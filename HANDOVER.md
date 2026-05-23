## 開発上の注意点

### Claude Code の運用ルール
- Claude Code は1プロジェクトずつ実行する（並列実行禁止）
- 並列実行するとメモリ不足で VSCode が不安定になる
- バックグラウンドシェルが詰まったら即 `/stop` で止める
- テスト実行は Claude Code に任せず手動で行う（バックグラウンド実行が詰まりやすい）

### 新ライブラリを使う実装前の確認
- `dir(client.xxx)` で使えるメソッドを事前確認する
- PyPI でバージョンと最新版を確認する

### Notion 連携の注意点
- notion-client v3.1.0（最新）は `databases.query()` が未実装
- DB クエリは httpx で直接 Notion API を叩いている
- httpx による直接呼び出しのデメリット：
  - `Notion-Version: 2022-06-28` をハードコード済み。Notion が API バージョンを更新したら手動対応が必要
  - notion-client のエラーハンドリング（APIErrorCode への変換）が効かない。生の HTTP ステータスで判断する必要がある
  - notion-client 内蔵のリトライ処理が効かない
- デメリットへの対策：
  - Notion-Version: Notion の API 更新通知を購読するか、定期的に確認する（https://developers.notion.com/changelog）
  - エラーハンドリング・リトライ: 必要になったら `_call_with_retry()` と同じパターンで追加可能

### GPU依存モジュールのテストについて
- torch/torchaudio/demucs を使うテストは実行時に CUDA 初期化で固まる可能性がある
- sys.modules への MagicMock 注入だけでは不十分な場合がある（torch 自体のインポートが引き金になる）
- 新しいテストファイルは実装直後に以下を手動確認すること：
  - `uv run pytest tests/test_新ファイル.py --collect-only`
  - `uv run pytest tests/test_新ファイル.py -q --tb=short`
- 固まった場合の判断フロー：
  - collect-only で固まる → トップレベル import を疑う
  - 収集は通るが実行で固まる → GPU初期化系なら pytest-timeout 導入を検討、それ以外はモックの差し替え先を確認
- test_separator.py は現状 `--ignore=tests/test_separator.py` で除外して運用（凍結中）