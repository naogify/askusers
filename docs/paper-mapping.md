# 論文・公式実装との対応

askusers は次の論文と公式実装に基づく。**論文で検証された部分**と**askusers 独自の拡張**を分けて記す。

- 論文: Maier, Aslak, Fiaschi, Pappas, Wiecki (2025) *LLMs Reproduce Human Purchase Intent via Semantic Similarity Elicitation of Likert Ratings*, arXiv:2510.08338
- 公式実装: <https://github.com/pymc-labs/semantic-similarity-rating> v1.1.0（commit `86dcd25`）

## 1. 論文で検証された手法（SSR）

論文の対象は「パーソナルケア製品 57 件の**購入意向**（5 段階 Likert）」。

| 要素 | 論文 | 公式実装 | askusers |
|---|---|---|---|
| ペルソナで条件付けした LLM が自然言語で回答 | 付録 A.4（"Reply briefly…"、T_LLM=0.5） | （対象外） | `runner.py`（短い自然な回答を要求。数値で答えさせない） |
| 自由回答を Likert 分布へ | 付録 A.4.3 | `ResponseRater` | `ssr.py` |
| 類似度 γ | 式(7): cos | `cosine_similarity_matrix`: γ=(1+cos)/2 | 公式に一致（`ssr.cosine_similarity_matrix`） |
| PMF | 式(8): p(r) ∝ γ(σ_r,t) − γ(σ_ℓ,t) + ε·δ_{ℓ,r}（ℓ は最小類似度の参照文章） | `similarities_to_pmf` | `ssr.similarities_to_pmf`（ε=0 既定。論文と同じ） |
| 温度 | 式(9): p(r;T) ∝ p(r)^{1/T}（T=1 を使用） | `scale_pmfs` | `ssr.scale_pmfs`（T=1 既定） |
| 複数の参照文章セット | m=6 セットの PMF を平均 | `get_response_pmfs('mean')` | 質問ごとに 3 セット。PMF を平均（`ssr.get_response_pmfs`） |
| 調査全体の分布 | 個人 PMF の平均 | `get_survey_response_pmf` | `ssr.get_survey_response_pmf` |
| Embedding | OpenAI text-embedding-3-small | sentence-transformers（既定 all-MiniLM-L6-v2） | 既定 text-embedding-3-small（`--embedding-model` で変更可） |
| 期待スコア | 分布の期待値 | （なし） | `ssr.expected_score` = Σ r·p(r) |

注: (1+cos)/2 と素の cos は、ε=0 では最小値の減算と正規化により同じ PMF になる（γ の差が定数倍されるだけ）。ε>0 では結果が異なるため、askusers は**公式実装の定義**に合わせた。

### 一致の検証

`tests/fixtures/ssr_golden.json` は公式 `compute.py`（v1.1.0）に乱数埋め込みを入力して得た PMF（セット別・平均・温度・ε の 5 条件）の固定値。`tests/test_ssr.py` が 1e-12 以内で一致することを確認する。公式パッケージが入っている環境では直接比較するテストも走る（任意）。

## 2. askusers 独自の拡張（論文では検証されていない）

- アンケートの自動生成（質問設計・提示前/提示後の区分）
- 調査対象者の動的な生成（属性スキーマの設計・多様性方針・重複検出）
- **SSR の参照文章セット**: 論文の 6 セットは購入意向用に手で作られたもの。askusers は質問ごとに LLM が 3 セットを生成する。**論文で検証された尺度ではない**。購入意向の参照文を他の概念（分かりやすさ等）に流用しない。
- 自由回答のテーマ分類（定性分析）、言及率の集計
- 発見事項・改善提案の生成
- 集計画面（HTML）、CSV 出力
- 改善前後の比較
- 生成 LLM が Claude であること（論文は GPT-4o / Gemini-2.0-flash）。生成モデルが変わると結果の分布も変わりうる。

## 3. 論文の限界を踏まえた注意

- 論文の検証結果（人間の調査との相関など）は、パーソナルケア製品の購入意向に対するもの。他の製品・教材・新規事業へ一般化できるとは限らない。
- 論文でも、LLM に十分な人口統計情報を与えないと評価が偏る（常に高評価になる）ことが報告されている。askusers は多様な属性を持つペルソナを生成して条件付けするが、その効果は未検証。
