"""SSR: 公式実装 (pymc-labs/semantic-similarity-rating v1.1.0) との整合性。"""
import json
from pathlib import Path

import numpy as np
import pytest

from askusers import ssr

G = json.loads((Path(__file__).parent / "fixtures" / "ssr_golden.json").read_text())
R = np.array(G["responses"])
SETS = [np.array(s) for s in G["anchor_sets"]]


@pytest.mark.parametrize("case", G["cases"], ids=lambda c: f"eps{c['epsilon']}-T{c['temperature']}")
def test_matches_official_golden(case):
    pmfs, per_set = ssr.get_response_pmfs(R, SETS, case["temperature"], case["epsilon"])
    np.testing.assert_allclose(pmfs, np.array(case["mean"]), atol=1e-12)
    for mine, off in zip(per_set, case["per_set"], strict=True):
        np.testing.assert_allclose(mine, np.array(off), atol=1e-12)
    np.testing.assert_allclose(ssr.get_survey_response_pmf(pmfs), case["survey"], atol=1e-12)


def test_golden_records_official_commit():
    assert G["officialCommit"] == "86dcd2597c7824e4fd6546b884c5500c43a4b022"


def test_pmf_is_normalized_and_nonnegative():
    pmfs, per_set = ssr.get_response_pmfs(R, SETS)
    assert np.allclose(pmfs.sum(axis=1), 1) and (pmfs >= 0).all()
    for p in per_set:
        assert np.allclose(p.sum(axis=1), 1)
        assert (p.min(axis=1) == 0).all()  # ε=0 では最小類似度の選択肢の確率は 0（論文式(8)）


def test_multiple_sets_are_averaged():
    _, per_set = ssr.get_response_pmfs(R, SETS)
    pmfs, _ = ssr.get_response_pmfs(R, SETS)
    np.testing.assert_allclose(pmfs, np.mean(per_set, axis=0))


def test_single_set_differs_from_mean():
    a, _ = ssr.get_response_pmfs(R, SETS[:1])
    b, _ = ssr.get_response_pmfs(R, SETS)
    assert not np.allclose(a, b)


def test_expected_score_and_distribution_aggregation():
    assert ssr.expected_score([0, 0, 0, 0, 1]) == 5
    assert ssr.expected_score([0.2] * 5) == pytest.approx(3.0)
    pmfs = np.array([[1, 0, 0, 0, 0], [0, 0, 0, 0, 1.0]])
    np.testing.assert_allclose(ssr.get_survey_response_pmf(pmfs), [0.5, 0, 0, 0, 0.5])


def test_epsilon_smooths_min_position():
    p0 = ssr.response_embeddings_to_pmf(R, SETS[0], 0.0)
    p1 = ssr.response_embeddings_to_pmf(R, SETS[0], 0.1)
    assert (p0.min(axis=1) == 0).all() and (p1.min(axis=1) > 0).all()
    assert np.allclose(p1.sum(axis=1), 1)


def test_temperature_zero_is_one_hot_and_high_temperature_flattens():
    pmfs, _ = ssr.get_response_pmfs(R, SETS, 0.0)
    assert ((pmfs == 1).sum(axis=1) == 1).all()
    base, _ = ssr.get_response_pmfs(R, SETS, 1.0)
    hot, _ = ssr.get_response_pmfs(R, SETS, 5.0)
    assert hot.std(axis=1).mean() < base.std(axis=1).mean()


def test_empty_responses_and_zero_vector_and_flat_similarity():
    assert ssr.cosine_similarity_matrix(np.empty((0, 32)), SETS[0]).shape == (0, 5)
    with pytest.raises(ssr.SSRError):
        ssr.cosine_similarity_matrix(np.zeros((1, 32)), SETS[0])
    with pytest.raises(ssr.SSRError):  # 全参照文章と同じ類似度 → 計算不能を明示（0 除算にしない）
        ssr.similarities_to_pmf(np.full((1, 5), 0.5))
    with pytest.raises(ssr.SSRError):
        ssr.get_response_pmfs(R, [])


def test_official_package_agrees_if_installed():
    """公式パッケージが入っている環境（任意）では直接比較する。"""
    off = pytest.importorskip("semantic_similarity_rating.compute")
    mine = ssr.response_embeddings_to_pmf(R, SETS[0], 0.05)
    np.testing.assert_allclose(mine, off.response_embeddings_to_pmf(R, SETS[0], 0.05), atol=1e-12)
