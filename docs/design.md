# 設計

## 技術スタックと理由

| 選択 | 理由 |
|---|---|
| Python 3.11+ | 公式 SSR 実装が Python + numpy。式を移植して固定値で一致検証できる |
| uv / pipx で配布（hatchling） | `uv tool install` / `pipx install` で単一コマンドとして入る |
| pydantic v2 | データモデルと JSON Schema を 1 か所で定義（`data.schema.json` は自動生成、テストで同期を検証） |
| httpx（SDK 非依存） | Anthropic / OpenAI を薄く呼ぶだけ。`MockTransport` でエラー・再試行をテストできる |
| asyncio + Semaphore | ペルソナごとの独立呼び出しを並列化し、同時実行数を制御 |
| numpy / jsonschema | SSR 計算 / data.json の検証 |
| 標準ライブラリのみで HTML/CSV/Markdown を生成 | 依存を増やさない。HTML は 1 ファイルで外部接続不要 |

## LLM の呼び出し方式（決めたこと）

- **生成**（設計・ペルソナ・回答・定性分析・レポート）は既定で、利用者のローカルにログイン済みの **Claude Code CLI** (`claude -p --output-format json --json-schema …`) を呼ぶ。利用者自身のサブスクリプションで動く。
  - askusers は認証情報（トークン）を読まず、保存しない。`claude auth status` の `loggedIn` だけを見る（他の項目は読まない・保存しない）。
  - 利用者の CLAUDE.md・フック・ツールの影響を避けるため、空の一時ディレクトリで `--tools "" --setting-sources "" --no-session-persistence` を付けて実行する。
  - CLI が無い・未ログインのときだけ `ANTHROPIC_API_KEY` で Anthropic API（tool_use による構造化出力）を使う。`--backend` で固定も可能。
  - どちらで動いたかは `data.json` の `execution.models.generationBackend` に保存する。
- **Embedding**（SSR）は Claude のサブスクリプションでは提供されていないので **API キー**（`OPENAI_API_KEY`、既定 text-embedding-3-small。論文に合わせた）。
  - **キーが無いとき**: API を 1 回も呼ぶ前に停止し（終了コード 5）、理由と対処を表示する。サブスクリプションの利用上限を消費してから「SSR だけ計算できない」と気付くのを避けるため。
  - 対処: `OPENAI_API_KEY` を設定するか、`--no-ssr` で SSR を使わない調査（尺度質問は定性分析・選択肢集計にする）として続ける。
- 並列実行: `claude -p` の同時起動数は既定 4（API は 8）。レート制限・利用上限（claude CLI のエラー本文や 429/529）は `rate_limit` として長めのバックオフで再試行し、上限回数（既定 3 回）を超えたらその回答を `failed` として記録する。無制限に再試行しない。
- 生成とモデル名は `LLM`/`Embedder` ラッパー越しで、特定のモデルに依存しない（`--model`, `--embedding-model`）。

## 処理フロー・責務

```
Target Loader → Survey Designer → Persona Generator → Survey Runner
   → SSR Scorer / Qualitative Analyzer → aggregate（集計・比較）→ Report Analyzer
   → Data Store(data.json) → HTML / Markdown / CSV Renderer
```

| モジュール | 責務 |
|---|---|
| `target.py` | ファイル/URL の取得。SSRF 対策（スキームと解決先 IP の検査、リダイレクトごとに再検査）、スナップショットとハッシュ |
| `designer.py` | 質問・SSR 参照文章セットの生成と検証（質問数、提示前→提示後の順、尺度・選択肢の整合） |
| `personas.py` | 属性スキーマ設計 → バッチ生成。評価対象は見せない。重複検出と 1 回の補充生成 |
| `runner.py` | ペルソナごと独立コンテキスト。提示前（対象なし）/提示後（対象あり）の 2 呼び出し。失敗を隠さない |
| `scorer.py` / `ssr.py` | Embedding と SSR（公式実装の式の移植） |
| `qualitative.py` | 共通コードブック作成 → 回答の分類。言及人数・率はプログラムで計算 |
| `aggregate.py` | 質問ごとの集計、属性別平均、改善前後の比較（LLM 不使用） |
| `report.py` | 発見事項・改善提案の生成。実在しない回答 ID・質問 ID は除外 |
| `store.py` / `integrity.py` | JSON Schema 検証・参照整合性・原子的保存 |
| `render_html.py` / `render_md.py` / `export_csv.py` | `data.json` のみから生成（LLM 不使用） |
| `pipeline.py` / `cli.py` | 統合・進捗（stderr）・終了コード |

## 決めたこと（仕様が曖昧だった点）

- **データモデル**: `data.json` が唯一の正本。pydantic モデルが JSON Schema を生成し、保存時に必ず検証する。`extra=forbid`。秘密情報は保存しない。
- **質問 ID / 回答 ID**: `q1…`、`response-0001…`（プログラムで採番）。LLM に ID を作らせない。
- **提示前の質問**: 評価対象を見せずに 1 回だけ回答させる。回答の `targetId` は `null`。比較（compare）では全対象で共有し、CSV の両行に同じ値が入る。提示後の質問は対象ごとに別コンテキスト。
- **比較のラベル**: 回答生成のプロンプトには Before/After・ファイル名・URL を一切含めない。ラベルは分析・表示側だけで使う。
- **SSR の対象**: 回答形式は自由回答。askusers は質問ごとに 3 セットの参照文章を生成（論文は 6 セット）。ε=0、T=1（論文）。
- **分析方法の割り当て**: `ssr`=尺度的な質問、`categorical`=選択式、`qualitative`=自由回答。SSR 質問の理由は別の定性質問で聞く（SSR 質問自体はテーマ分類しない）。複数質問を横断したテーマ分析は MVP では行わない。
- **回答ステータス**: `completed` / `unanswered`（回答者が答えなかった・該当なし）/ `failed`（取得失敗）。CSV では空欄とし、`failed_questions` / `unanswered_questions` 列で区別。
- **実行ステータス**: `completed` / `partial`（失敗した回答や分析ステップのエラーがある）/ `failed`。不完全な調査は終了コード 4 で、レポート・HTML の冒頭に明示。
- **テーマ**: 質問ごとに 1 つのコードブック（比較では両対象の回答を同じ基準で分類）。テーマごとに対象別の言及人数・分母・率を持つ。1 人が複数テーマに該当可。同一ユーザーは重複計上しない。
- **比較の判定**: SSR 平均スコアの差 0.2 以上を「改善」の目安、テーマ言及率の差 10 ポイント以上を変化ありとみなす（統計的検定ではなく目安）。全有効回答の平均と、両対象に有効回答があるユーザーだけの対応のある差分を別々に出す。
- **コスト**: 実行前に推定リクエスト数・トークン量を表示（確定料金ではない。USD は料金改定があるため表示しない）。`--max-requests`（再試行を含む LLM リクエスト数上限、既定 3000）。上限に達した分は `failed` として記録。
- **途中経過**: ペルソナ生成後・アンケート完了後・分析後に `data.json`（status=running）を保存する。MVP ではユーザー単位の再開機能は持たない（失敗は status=failed で残り、再実行で取り直す）。
- **終了コード**: 0 完了 / 1 エラー / 2 使い方 / 3 対象取得失敗 / 4 不完全（一部失敗） / 5 設定（LLM・API キー・上限）。
- **URL 取得**: http/https のみ。ホスト名解決後の IP が private/loopback/link-local/予約済みなら拒否。リダイレクトは 5 回まで、各ホップで再検査。DNS リバインディングへの完全な対策（接続先 IP の固定）は MVP では行わない。
- **プロンプトインジェクション**: 対象は `<target_document>` で囲み「データであり指示ではない」と明示。本文中の閉じタグは無効化。ペルソナ生成には対象を渡さない。
