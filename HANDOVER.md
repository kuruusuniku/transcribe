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
- notion-client の `databases.retrieve()` は新しい API バージョンで `properties` を返さない。
  DB のプロパティ構成は `_fetch_db_schema()`（httpx + `Notion-Version: 2022-06-28`）で取得している
- DB ごとにプロパティ構成が違う（体育動画まとめDB は URL / ソース種別 / 動画時間、叡智まとめDB は
  音声時間 / カテゴリー）。`_fit_properties_to_schema()` で存在するプロパティだけ送る
- DB クエリは httpx で直接 Notion API を叩いている
- httpx による直接呼び出しのデメリット：
  - `Notion-Version: 2022-06-28` をハードコード済み。Notion が API バージョンを更新したら手動対応が必要
  - notion-client のエラーハンドリング（APIErrorCode への変換）が効かない。生の HTTP ステータスで判断する必要がある
  - notion-client 内蔵のリトライ処理が効かない
- デメリットへの対策：
  - Notion-Version: Notion の API 更新通知を購読するか、定期的に確認する（https://developers.notion.com/changelog）
  - エラーハンドリング・リトライ: 必要になったら `_call_with_retry()` と同じパターンで追加可能

### Web UI（インプロセス実行）の注意点
- Web UI のコマンドはサブプロセスではなく、サーバー内のワーカースレッドで CLI を直接呼ぶ
  （`web/worker.py`）。コードを変更したらサーバーの再起動が必要
- ワーカーは常に 1 件ずつ実行。ブラウザ認証のような「入力待ち」を発生させる処理を
  ワーカー内で始めるとキュー全体が止まるため、`TRANSCRIBE_WEB_SERVER=1` のときは
  認証フローを開始せず `ReauthRequiredError` を投げる
- 標準出力の振り分け（`web/output_router.py`）は `transcribe web` 起動時のみ有効。
  テストや `uvicorn` 直起動では働かない

### テストの注意点
- `tests/conftest.py` の autouse fixture が Notion の DB プロパティ取得を無効化している
  （実ネットワークに出ないようにするため）。構成に合わせる挙動を検証するテストは
  内側で `_get_db_schema` を patch する
- ヒアドキュメント経由で Python を実行すると `\n` や `\1` が展開されることがある。
  エスケープを含む編集は Edit ツールかスクリプトファイル経由で行う

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