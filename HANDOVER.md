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