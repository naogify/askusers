"""Semantic Similarity Rating (SSR)。

論文 Maier et al. (2025) arXiv:2510.08338 付録 A.4.3 と公式実装
pymc-labs/semantic-similarity-rating v1.1.0 (`compute.py` / `response_rater.py`) の計算を
そのまま移植したもの。独自の簡略化はしない。対応関係は docs/paper-mapping.md を参照。

  γ(σ_r, t)  = (1 + cos(σ_r, t)) / 2                                  公式 cosine_similarity_matrix
  p(r)       = (γ_r − γ_min + ε·δ_{r,ℓ}) / (Σ_r γ_r − n·γ_min + ε)   論文 式(8)
  p(r; T)   ∝ p(r)^(1/T)                                              論文 式(9)
  複数の参照文章セット: セットごとの PMF を平均 (公式 'mean')
  調査全体の PMF: 個人 PMF の単純平均 (公式 get_survey_response_pmf)
"""

from __future__ import annotations

import numpy as np

EPSILON = 0.0  # 論文は ε=0, T=1 で検証
TEMPERATURE = 1.0


class SSRError(ValueError):
    pass


def cosine_similarity_matrix(responses: np.ndarray, anchors: np.ndarray) -> np.ndarray:
    """responses: (n, dim), anchors: (dim, 5) → (n, 5) の (1+cos)/2。"""
    if responses.shape[0] == 0:
        return np.empty((0, anchors.shape[1]))
    nr = np.linalg.norm(anchors, axis=0)
    nl = np.linalg.norm(responses, axis=1)
    if np.any(nr == 0) or np.any(nl == 0):
        raise SSRError("ゼロベクトルの Embedding があり類似度を計算できません")
    right = anchors / nr[None, :]
    left = responses / nl[:, None]
    return (1 + left.dot(right)) / 2


def similarities_to_pmf(cos: np.ndarray, epsilon: float = EPSILON) -> np.ndarray:
    cos = np.asarray(cos, dtype=float)
    if cos.shape[0] == 0:
        return np.empty_like(cos)
    cos_min = cos.min(axis=1, keepdims=True)
    cos_sum = cos.sum(axis=1, keepdims=True)
    numerator = cos - cos_min
    if epsilon > 0:
        numerator[np.arange(cos.shape[0]), np.argmin(cos, axis=1)] += epsilon
    denominator = cos_sum - cos.shape[1] * cos_min + epsilon
    if np.any(denominator <= 0):
        # 全参照文章との類似度が完全に等しい → 分布を定義できない（公式実装は 0 除算になる）
        raise SSRError("参照文章との類似度に差がなく確率分布を計算できません")
    return numerator / denominator


def response_embeddings_to_pmf(
    responses: np.ndarray, anchors: np.ndarray, epsilon: float = EPSILON
) -> np.ndarray:
    return similarities_to_pmf(cosine_similarity_matrix(responses, anchors), epsilon)


def scale_pmfs(pmfs: np.ndarray, temperature: float) -> np.ndarray:
    pmfs = np.asarray(pmfs, dtype=float)
    if temperature == 0.0:
        out = np.zeros_like(pmfs)
        out[np.arange(pmfs.shape[0]), np.argmax(pmfs, axis=1)] = 1.0
        uniform = np.all(pmfs == pmfs[:, :1], axis=1)
        out[uniform] = pmfs[uniform]
        return out
    hist = pmfs ** (1 / temperature)
    return hist / hist.sum(axis=1, keepdims=True)


def get_response_pmfs(
    responses: np.ndarray,
    anchor_sets: list[np.ndarray],
    temperature: float = TEMPERATURE,
    epsilon: float = EPSILON,
) -> tuple[np.ndarray, list[np.ndarray]]:
    """(各セット平均の PMF (n,5), セット別 PMF のリスト)。anchor_sets の各要素は (dim, 5)。"""
    if not anchor_sets:
        raise SSRError("参照文章セットがありません")
    per_set = [response_embeddings_to_pmf(responses, m, epsilon) for m in anchor_sets]
    pmfs = np.array(per_set).mean(axis=0)
    if temperature != 1.0:
        pmfs = scale_pmfs(pmfs, temperature)
    return pmfs, per_set


def get_survey_response_pmf(pmfs: np.ndarray) -> np.ndarray:
    return pmfs.mean(axis=0)


def expected_score(pmf: np.ndarray | list[float]) -> float:
    return float(np.dot(np.asarray(pmf, dtype=float), np.arange(1, 6)))
