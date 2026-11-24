"""改善前後の比較。"""
import re

import pytest
from conftest import make_llm, run_pipeline

from askusers import aggregate
from askusers.integrity import check_integrity


@pytest.fixture
def cmp_run(tmp_path, two_docs):
    llm = make_llm()
    d = run_pipeline(tmp_path, list(two_docs), mode="compare", llm=llm, count=8)
    return d, llm


def test_same_personas_questions_anchors_and_settings(cmp_run):
    d, _ = cmp_run
    assert d.mode == "compare" and len(d.targets) == 2 and check_integrity(d) == []
    for q in d.survey.questions:
        if q.phase == "after_exposure":
            for u in d.users:  # 全ユーザーが両対象に回答している
                assert {r.targetId for r in d.responses if r.userId == u.id and r.questionId == q.id} == {"target-001", "target-002"}
    ssr_q = next(q for q in d.survey.questions if q.analysisMethod == "ssr")
    assert len(ssr_q.anchorSets) == 3  # 参照文章セットは質問に 1 つで両対象に共通
    assert d.execution.models["generation"] and len({d.execution.models["generation"]}) == 1
    assert d.execution.settings["ssrEpsilon"] == 0.0


def test_contexts_are_independent_and_unlabeled(cmp_run):
    d, llm = cmp_run
    post = [(u) for t, _s, u in llm.raw.calls if t == "answer" and "<target_document>" in u]
    assert len(post) == len(d.users) * 2  # 対象ごとに別呼び出し
    for u in post:
        assert u.count("<target_document>") == 1  # 1 呼び出しに 1 対象だけ
        assert not re.search(r"before|after|改善前|改善後|before-v1|after-v2|v1|v2", u, re.I)
    sys_prompts = " ".join(s for t, s, _u in llm.raw.calls if t == "answer")
    assert not re.search(r"before|after|改善", sys_prompts, re.I)
    a = [u for u in post if "APIの使い方。" in u and "【改善】" not in u]
    b = [u for u in post if "【改善】" in u]
    assert len(a) == len(b) == len(d.users)


def test_pre_exposure_answers_shared_across_targets(cmp_run):
    d, _ = cmp_run
    pre = [r for r in d.responses if r.targetId is None]
    before_q = [q for q in d.survey.questions if q.phase == "before_exposure"]
    assert len(pre) == len(d.users) * len(before_q)


def test_delta_computation(cmp_run):
    d, _ = cmp_run
    c = d.analysis.comparison
    assert c and c.beforeTargetId == "target-001" and c.afterTargetId == "target-002"
    qc = c.questions[0]
    sb = next(p for p in next(s for s in d.analysis.questions if s.questionId == qc.questionId).perTarget if p.targetId == "target-001")
    sa = next(p for p in next(s for s in d.analysis.questions if s.questionId == qc.questionId).perTarget if p.targetId == "target-002")
    assert qc.meanDelta == pytest.approx(sa.ssr.meanScore - sb.ssr.meanScore)
    scores = {(r.userId, r.targetId): r.ssr.expectedScore for r in d.responses if r.questionId == qc.questionId and r.ssr}
    paired = [scores[(u.id, "target-002")] - scores[(u.id, "target-001")] for u in d.users]
    assert qc.pairedN == len(d.users) and qc.pairedMeanDelta == pytest.approx(sum(paired) / len(paired))
    assert qc.distributionBefore != qc.distributionAfter and abs(sum(qc.distributionAfter) - 1) < 1e-9
    assert c.improved  # モックは after のほうが高評価になる


def test_paired_and_unpaired_are_distinguished_when_responses_missing(cmp_run):
    d, _ = cmp_run
    d = d.model_copy(deep=True)
    qid = next(q.id for q in d.survey.questions if q.analysisMethod == "ssr")
    victim = next(r for r in d.responses if r.questionId == qid and r.targetId == "target-002")
    victim.ssr, victim.status, victim.answer, victim.error = None, "failed", None, "x"
    d.analysis.questions = [aggregate.summarize_question(d, q) for q in d.survey.questions]
    qc = aggregate.build_comparison(d).questions[0]
    assert qc.validBefore == len(d.users) and qc.validAfter == len(d.users) - 1
    assert qc.pairedN == len(d.users) - 1  # 全有効回答の集計と対応のある比較を区別


def test_common_theme_codebook_and_mention_rate_comparison(cmp_run):
    d, _ = cmp_run
    after_q = [t for t in d.analysis.themes if d.survey.questions[int(t.questionId[1:]) - 1].phase == "after_exposure"]
    assert after_q
    for t in after_q:
        assert [s.targetId for s in t.stats] == ["target-001", "target-002"]  # 同じ分類基準で両対象を数える
        for s in t.stats:
            assert s.userCount == len(set(s.userIds)) and s.rate == pytest.approx(s.userCount / s.denominator)
    qids = {t.questionId for t in after_q}
    for qid in qids:  # 質問ごとにコードブックは 1 つ（両対象の回答を同じ基準で分類）
        labels = [t.label for t in after_q if t.questionId == qid]
        assert len(labels) == len(set(labels))
    tc = d.analysis.comparison.themes
    assert tc and all(abs(x.delta - (x.rateAfter - x.rateBefore)) < 1e-9 for x in tc)


def test_theme_verdicts():
    from askusers.models import MentionStat, SurveyData, Theme
    def theme(tid, val, rb, ra):
        mk = lambda t, r: MentionStat(targetId=t, userIds=[f"u{i}" for i in range(int(r * 10))], responseIds=[], userCount=int(r * 10), denominator=10, rate=r)  # noqa: E731
        return Theme(id=tid, questionId="q1", label=tid, description="", valence=val, stats=[mk("target-001", rb), mk("target-002", ra)])
    import json
    from pathlib import Path
    base = SurveyData.model_validate_json((Path(__file__).parent.parent / "examples/mock-compare/data.json").read_text())
    base.analysis.themes = [theme("a", "negative", 0.7, 0.1), theme("b", "negative", 0.0, 0.3), theme("c", "negative", 0.1, 0.6),
                            theme("d", "positive", 0.1, 0.5), theme("e", "negative", 0.5, 0.52), theme("f", "negative", 0.4, 0.0)]
    v = {t.themeId: t.verdict for t in aggregate.build_comparison(base).themes}
    assert v == {"a": "improved", "b": "new", "c": "worsened", "d": "improved", "e": "unchanged", "f": "resolved"}
    _ = json


def test_compare_limitation_is_stated(cmp_run):
    d, _ = cmp_run
    assert any("実際の顧客における改善効果を保証しません" in x for x in d.limitations)


def test_markdown_has_no_empty_table_without_ssr(tmp_path, two_docs):
    from askusers.render_md import render_markdown
    d = run_pipeline(tmp_path, list(two_docs), mode="compare", embedder=None, no_ssr=True, count=4)
    md = render_markdown(d)
    assert "| 指標 |" not in md and "## 2. 発見事項" in md and d.analysis.comparison.themes
