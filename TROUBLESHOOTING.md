# 困ったとき

症状から探してください。まずは設定の診断を実行すると、原因がすぐわかることが多いです。

```
uv run transcribe doctor --test
```

（Web UI では右上の「設定状況」→「接続テスト」）

---

## 処理に失敗した（「要対応」に「処理に失敗しました」）

ジョブを開き、表示されているエラーを確認してから **「失敗したところから再開」** を押します。
文字起こしまで終わっていれば、文字起こしはやり直さずに続きから実行されます。

| エラーに含まれる言葉 | 原因 | 対処 |
|---|---|---|
| `cookies` / `browser` / `Sign in to confirm` | Firefox が起動中で Cookie を読めない、または未ログイン | Firefox を完全に終了（タスクトレイも確認）してから再開 |
| `CUDA out of memory` | GPU のメモリ不足 | 下の「GPU のメモリが足りない」を参照 |
| `not compiled with CUDA support` | Mac で `device: cuda` のまま | README の「Mac で使う場合」のとおり `device: cpu` にする |
| `No such file` / `見つかりません` | 元の音声ファイルが移動・削除された | ファイルを戻すか、もう一度追加し直す |
| `HTTP Error 403` / `Unsupported URL` | 非公開動画・URL の誤り | URL と公開設定を確認 |

同じエラーで何度も失敗する場合は「その他 → 最初からやり直す」を試し、それでも駄目なら「処理の記録」タブのエラー詳細とログ（`logs/` フォルダ）を確認してください。

CLI の場合:

```
uv run transcribe status --id <id>   # どの段階で失敗したか
uv run transcribe retry <id>         # 失敗したところから再開
uv run transcribe rerun <id>         # 最初からやり直す
```

---

## 文字起こしはできたが、まとめ・Notion・Docs が失敗した

「要対応」に「◯◯に失敗しました」と出ます。ジョブを開いて **「後処理をやり直す」** を押すと、失敗・未実行の処理だけをやり直します（文字起こしはやり直しません）。

```
uv run transcribe resume-post <id>
```

### まとめ（AI）が失敗する

- **503 / 混雑**: Gemini 無料枠の混雑時に起きます。自動で数回リトライします。時間をおいて「後処理をやり直す」
- **出力が途中で切れた（max_output_tokens）**: 長い動画でまとめが上限を超えました。`config.yaml` の `summarize.max_output_tokens` を増やしてから「後処理をやり直す」。途中までの出力は `summary.truncated.md` に残ります
- **API キー**: 「設定状況」の接続テストで確認。頻発する場合は `summarize.provider: claude` に切り替え

### Notion 同期が失敗する

順に確認してください:

1. `notion.token` が正しい Internal Integration Secret か
2. `notion.database_id` が 32 桁英数字（データベース URL の末尾）か
3. Notion データベースの「…」→「接続」でインテグレーションを接続しているか
4. DB に「名前（タイトル）」プロパティがあるか。そのほかのプロパティ（日付 / タグ / まとめ進捗 / URL / ソース種別 / 動画時間・音声時間）は、DB にあるものだけ書き込まれます

叡智講義（録音ファイル）は `notion.local_database_id` の DB（叡智まとめDB）に登録されます。この DB にもインテグレーションの接続が必要です。

### Google Docs 同期が失敗する / 初回認証

`credentials.json` をプロジェクト直下に置き、CLI で一度同期を実行するとブラウザで認証画面が開きます。認証後に `token.json` が作られます。

```
uv run transcribe sync
```

GUI のない環境では、別のマシンで作った `token.json` をコピーしてください。

---

## 書き起こしの見出しや区切り方を変えたい

`config.yaml` の `output` を変更したあと、既存ジョブに反映するには作り直しを実行します（再文字起こしはしません）。

```
uv run transcribe reformat <id>
```

Web UI ではジョブ詳細の「その他 → 書き起こしの表示を作り直す」です。まとめにも反映するには、そのあと「まとめを作り直す」を実行します。

| 設定 | 既定 | 説明 |
|---|---|---|
| `timestamp_interval_seconds` | 30 | 1 区切りの最大の長さ（秒） |
| `paragraph_gap_seconds` | 1.0 | この長さ以上の無音があれば区切る |
| `segment_timestamps` | false | true で各発言の先頭にも時刻を付ける |

---

## 誤認識が多い

- 文字起こし画面で間違っている言葉を選択 →「用語辞書に登録」。次回から自動で直ります
- 用語辞書の「話題のヒント」に頻出する専門用語を書くと、最初から正しく聞き取られやすくなります
- 音声分離（`audio_separation.enabled`）は精度が下がることが多いので `false` を推奨します

---

## GPU のメモリが足りない（CUDA out of memory）

`config.yaml` の `transcription.compute_type` を下げます。

| GPU VRAM | 推奨設定 |
|---|---|
| 4GB 前後（旧世代） | `int8` |
| 4–6GB（Tensor Core 対応） | `int8_float16` |
| 8GB+ | `float16` |
| 12GB+ | `float32` |

`audio_separation.enabled: false`（デフォルト）も確認してください。Web UI を起動したままだと Whisper モデルを最大 10 分間保持するため、ほかの GPU アプリと併用する場合は Web UI を止めてください。

---

## 同じ動画が二重に処理された / 一覧に重複がある

URL は `https://www.youtube.com/watch?v=ID` の形にそろえて登録されるため、短縮 URL（youtu.be）や `&t=` 付きでも同じ動画として扱われます。以前のバージョンで登録したジョブが重複している場合は、不要な方をジョブ詳細の「その他 → 削除」で消してください。

---

## ディスクがいっぱい

ダウンロードした音声は再開用に `data/work/` に、Web UI からアップロードした録音ファイルは `data/uploads/` に残ります。
Web UI の「ツール → 一時ファイルを削除」（CLI: `uv run transcribe clean`）で、作業フォルダと処理が完了したジョブのアップロードファイルを削除できます（処理中は実行されません）。

---

## Web UI に別の PC からアクセスしたい

`config.yaml` の `web.token` を設定し、`web.host: "0.0.0.0"` で起動します。初回は `http://<PCのIP>:8000/?token=<token>` で開いてください。トークンを設定しないと 127.0.0.1 以外では起動しません。

---

## スマホから使いたい（Tailscale 経由）

インターネットには公開せず、自分の端末だけから使う方法です。ポート開放は不要です。

1. **PC とスマホに [Tailscale](https://tailscale.com/) を入れて、同じアカウントでログインする**（個人利用は無料）
2. PC の Tailscale アドレスを確認する（`100.x.y.z` の形式。Tailscale アプリに表示されます）
3. `config.yaml` の `web.token` にトークンを設定する（未設定だと外部アクセス用の起動が拒否されます）
4. PC で次のように起動する

```
uv run transcribe web --host 0.0.0.0
```

5. スマホのブラウザで `http://100.x.y.z:8000/?token=<トークン>` を開く
   - 初回だけトークン付きで開けば、以降は Cookie で認証されます
   - ホーム画面に追加すると、アプリのように全画面で使えます

> `--host 0.0.0.0` は同じ Wi-Fi の端末からも見える状態になります。トークンがあれば操作はできませんが、
> より厳密にするなら `--host 100.x.y.z`（Tailscale アドレス）を指定してください。

**注意**: PC がスリープすると使えません。処理中にスリープしないよう設定してください（`powercfg /change standby-timeout-ac 0`）。
