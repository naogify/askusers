# askusers

**Ask users. Build better.** — 合成ユーザー（AI が演じる架空の回答者）にアンケート調査を行い、アイデア・製品・教材・LP などの改善仮説を素早く得る CLI です。

```bash
askusers idea.md --who "中小企業の経営者" --ask "月額1万円で利用したいと思うか？"
```

1 コマンドで、誰に・何を聞き・どう答え・どう集計し・何が分かり・次に何を直すべきかを追跡できる 4 ファイルが出ます。

> **これは実際の顧客調査の代替ではありません。** 合成ユーザーは実在の人物ではなく、統計的な代表性は保証されません。結果は「改善仮説の生成と検証サイクルの高速化」に使い、実際のユーザーによる検証を行ってください。

## 出力

```
askusers-results/<run-id>/
├── survey.html     集計画面（概要 / ユーザー / 個別回答）。単一ファイル・オフライン・サーバー不要
├── report.md       結論・発見事項・改善提案（根拠の質問ID・回答IDつき）
├── responses.csv   1行=1ユーザー。属性と回答・SSRスコアを同じ行に。Excel/スプレッドシート用（UTF-8 BOM）
└── data.json       全データの唯一の正本（JSON Schema 検証済み）
```

`survey.html` / `report.md` / `responses.csv` はすべて `data.json` から生成されます。LLM への再問い合わせなしに再生成できます:

```bash
askusers render askusers-results/<run-id>/data.json
```

サンプル（実 LLM・架空の題材。入力は [`samples-src/`](samples-src)）: [`examples/real-survey`](examples/real-survey) / [`examples/real-compare`](examples/real-compare)。ブラウザで見られる公開版は GitHub Pages（`python3 scripts/build_pages.py` で `_site/` に生成）。
偽のモック LLM で生成した、中身に意味のないデモ: [`examples/mock-survey`](examples/mock-survey) / [`examples/mock-compare`](examples/mock-compare)

## インストール

Python 3.11+ が必要です。

```bash
uv tool install git+https://github.com/naogify/askusers    # または
pipx install git+https://github.com/naogify/askusers
```

### LLM と API キー

| 用途 | 使うもの | 設定 |
|---|---|---|
| 生成（質問設計・ペルソナ・回答・定性分析・レポート） | **ログイン済みの [Claude Code](https://claude.com/claude-code) CLI**（あなたのサブスクリプション）。無い/未ログインのときだけ Anthropic API | `claude` を入れて `claude auth login`。API の場合は `ANTHROPIC_API_KEY` |
| Embedding（SSR による数値化） | OpenAI Embedding API（論文と同じ `text-embedding-3-small`） | `OPENAI_API_KEY` |

- askusers は Claude のログイン情報（トークン）を読み取らず、保存しません。`claude -p` を呼ぶだけです（`claude auth status` の「ログイン済みか」だけ確認します）。
- Claude のサブスクリプションでは Embedding を提供していないため、SSR には API キーが必要です。**`OPENAI_API_KEY` が無いと、API を呼ぶ前に停止します**（終了コード 5）。SSR を使わずに続けるなら `--no-ssr`（尺度質問を定性分析・選択肢集計に置き換えた調査になります）。
- **調査対象・入力内容は外部の LLM（Claude / Embedding API）に送信されます。** 個人情報・機密情報を含めないでください。
- サブスクリプション利用時は、Claude の利用上限（レート制限）を消費します。上限に達した回答は再試行（既定 3 回）のあと「回答取得失敗」として記録され、調査は「不完全（partial）」として扱われます。

## 使い方

```bash
askusers <target> --who <audience> --ask <objective>

askusers idea.md --who "中小企業の経営者" --ask "月額1万円で利用したいと思うか？"
askusers lesson.md --who "プログラミング未経験者" --ask "どこが分かりにくいか？"
askusers https://example.com/ --who "Webエンジニア" --ask "業務で利用したいと思うか？"
askusers idea.md                       # 最小限（対象者・目的は AI が推定し、結果に明示）
askusers idea.md --count 30            # 回答者数（既定 100）
askusers idea.md --open                # 完了後に survey.html をブラウザで開く
askusers idea.md --format json         # 標準出力は JSON のみ（進捗は標準エラー）。AI エージェント向け
askusers http://127.0.0.1:8852/ --allow-local --render --routes "#land,#spec"   # ローカルの JS アプリ（要 render extra）
askusers compare before.md after.md --who "プログラミング初心者" --ask "説明は分かりやすいか？"
```

| オプション | 説明 | 既定 |
|---|---|---|
| `--who` | 調査対象者（自然言語） | AI が推定 |
| `--ask` | 調査目的（自然言語） | 総合的な評価と改善点 |
| `--count` | 合成ユーザー数 | 100 |
| `--output` | 出力ディレクトリ | `askusers-results/<run-id>` |
| `--format` | 標準出力の形式 `text` / `json`（保存ファイルは変わらない） | text |
| `--open` | 完了後に HTML を開く | false |
| `--model` | 生成モデル（環境変数 `ASKUSERS_MODEL`） | `claude-sonnet-5-5` |
| `--backend` | `auto` / `claude-cli` / `anthropic-api` | auto |
| `--embedding-model` | Embedding モデル | `text-embedding-3-small` |
| `--concurrency` | LLM 同時実行数 | claude CLI 4 / API 8 |
| `--max-requests` | LLM リクエスト数の上限（再試行含む） | 3000 |
| `--no-ssr` | Embedding キー無しで SSR を使わず続行 | false |
| `--allow-local` | localhost（loopback）の URL を読むことを明示的に許可。private LAN 等は許可されない | false |
| `--render` | JavaScript を実行して描画後の画面を読む（Playwright / Chromium が必要） | false |
| `--route` / `--routes` | `--render` で開く画面（`#hash` か相対パス）。`--route` は繰り返し可、`--routes "#a,#b"` はカンマ区切り。省略時はトップのみ | — |
| `--mock` | オフラインのデモ（偽の LLM。結果に意味は無い） | false |

### ローカルの動くサービスや JS で描くページを調べる

既定の URL 取得は HTML を静的に読むだけで、JavaScript は実行しません（SPA は中身が空になります）。`--render` を付けると Chromium で描画してから読みます。

```bash
uv sync --extra render && uv run playwright install chromium     # 初回のみ
askusers http://127.0.0.1:8852/ --allow-local --render --routes "#land,#import,#spec"
```

- 各画面の「表示されているテキスト」と「操作できる要素（ボタン・リンク・選択肢・入力欄のラベル）」を、画面ごとの見出し付きで 1 つの調査対象にして `data.json` に保存します（スナップショット。スキーマ変更なし）。
- canvas / 3D の描画は画像なのでテキスト化されません（その旨を対象に注記します）。スクリーンショットは撮らず、画像入力もしません。
- 画面は 1 つずつ新しいタブで開きます（画面間でページ内の状態は引き継がれません）。

実行前に、推定リクエスト数とトークン量（確定料金ではありません）を標準エラーに表示します。

入力は Markdown / テキストファイル / URL（http/https。取得内容はスナップショットとして保存）。対象を取得できないときは、推測せずエラーで終了します。

終了コード: `0` 完了 / `1` エラー / `2` 使い方 / `3` 対象取得失敗 / `4` 不完全（一部の回答が失敗） / `5` 設定（LLM・API キー・上限）

## 仕組み

```
対象の取得 → アンケート設計 → ペルソナ生成 → 回答（ペルソナごとに独立）
   → SSR（定量） / 定性分析 → 集計 → 発見事項・改善提案 → data.json → HTML / Markdown / CSV
```

1. **アンケート設計**: `--ask` を目的として質問を自動生成（5〜10 問目安）。各質問は「提示前/提示後」「回答形式（自由回答/単一選択/複数選択）」「分析方法（`ssr`/`categorical`/`qualitative`）」を持つ。誘導的な質問や肯定前提を避ける。
2. **ペルソナ生成**: 対象者の定義だけから（評価対象は見せずに）属性スキーマを設計し、多様なペルソナを生成。評価対象を必要としない人や既存手段に満足している人も含める。重複を検出して補充。
3. **回答**: ペルソナごとに独立した LLM コンテキストで回答。提示前の質問は評価対象を見せずに回答させる。他のペルソナの回答は参照しない。尺度質問は数値ではなく自然言語で答えさせる。失敗は隠さず記録する。
4. **SSR（Semantic Similarity Rating）**: 自由回答と「1〜5 に対応する参照文章」の Embedding の類似度から、評価の**確率分布**と期待スコアを計算（論文と公式実装の式をそのまま実装。複数の参照文章セットの分布を平均）。
5. **定性分析**: 自由回答をテーマに分類。**言及人数・言及率は LLM ではなくプログラムが**分類結果から計算（1 人が複数テーマに該当しうる。分母を明示）。
6. **レポート**: 発見事項・改善提案は実在する質問ID・回答IDに紐づく。存在しない回答の引用は除外される。
7. **比較**（`compare`）: 同じペルソナ・質問・参照文章・モデル・条件で 2 つの対象を評価。回答は対象ごとに独立したコンテキストで生成し、プロンプトに「改善前/後」やファイル名を含めない。

### 論文との対応

論文（Maier et al., 2025, [arXiv:2510.08338](https://arxiv.org/abs/2510.08338)）と公式実装（[pymc-labs/semantic-similarity-rating](https://github.com/pymc-labs/semantic-similarity-rating)）に基づく部分と、askusers 独自の拡張を区別しています。詳細な式の対応は [docs/paper-mapping.md](docs/paper-mapping.md)。

| 論文で検証された部分 | askusers 独自の拡張（未検証） |
|---|---|
| ペルソナ条件付けの LLM が自然言語で回答 | アンケートの自動生成 |
| SSR（類似度 → PMF、min 減算、ε、温度、複数セット平均） | ペルソナの動的生成・重複検出 |
| Embedding による参照文章との比較 | **質問ごとに生成する SSR 参照文章セット**（論文のセットは購入意向用） |
| 確率分布として扱う | 定性分析、改善提案、集計画面、CSV、比較機能 |

SSR の計算は公式実装の `compute.py` から生成した固定値と 1e-12 以内で一致することをテストで確認しています（`tests/fixtures/ssr_golden.json`）。

## データ（data.json）

スキーマは [`src/askusers/data.schema.json`](src/askusers/data.schema.json)（pydantic モデルから生成、保存時に検証）。調査対象のスナップショットとハッシュ、アンケート、SSR 参照文章セット、全ペルソナ、全回答（元の自由回答）、SSR 結果、テーマと該当回答 ID、発見事項・改善提案、モデル・プロンプト・実行条件・API 利用量・エラーを保存します。API キー・ログイン情報は保存しません。同じ条件での再実行は可能ですが、LLM の非決定性・モデル更新により同じ結果になるとは限りません。

## 制約・限界

- 合成ユーザーの評価を実際の市場需要・購入率と混同しないでください。SSR の確率分布は**回答者が実際に選んだ割合ではありません**。
- 回答者数が多くても統計的な代表性は得られません。属性分布は実際の市場の分布ではありません。
- LLM の知識を、実在する初心者などの実際の理解度と同一視しないでください。
- 論文の検証はパーソナルケア製品の購入意向です。他の用途への一般化は検証されていません。生成モデルが論文（GPT-4o / Gemini）と異なる点にも注意。
- SSR の参照文章セットは質問ごとに LLM が生成した尺度で、論文で検証された尺度ではありません。
- モデル・プロンプトで結果が変わります。合成ユーザーの評価への過剰な最適化は避けてください。
- MVP の範囲外: PDF / HTML ファイル / ディレクトリ / 画像の入力、複数質問を横断するテーマ分析、ユーザー単位の中断再開、実データによるキャリブレーション。
- URL 取得は http/https のみで、既定では内部ネットワーク宛て（private/loopback/link-local）を拒否します。DNS リバインディングへの完全な対策はしていません。
- `--allow-local` で許可されるのは **loopback（127.0.0.0/8・::1・localhost）だけ**です。private LAN・link-local（クラウドのメタデータ等）は指定しても拒否します。リダイレクト先も同じ判定です。**ローカルのサービスの内容は外部の LLM に送信されます**。機密を含むものには使わないでください。
- `--render` ではブラウザが出すサブリソース要求にも同じ判定をかけ、内部アドレス宛てを遮断します（遮断件数は対象の先頭に注記）。ただし Chromium 自体の DNS 解決とは別に判定するため、DNS リバインディングへの完全な対策ではありません。取得対象のページの JavaScript を実行するため、信頼できない URL には使わないでください。
- 実 API（Anthropic API・OpenAI Embedding）での動作は、キーがある環境で確認してください（自動テストはすべてモックです）。

## 開発

```bash
uv sync --extra dev
uv run pytest          # LLM / Embedding はすべてモック（API キー不要）。実ブラウザのテスト(-m browser)は Chromium が無ければ skip
uv run ruff check .
uv run askusers examples/... --mock --count 5   # オフラインのデモ実行
```

設計ドキュメント: [docs/design.md](docs/design.md) / [docs/paper-mapping.md](docs/paper-mapping.md)。ライセンス: MIT。
