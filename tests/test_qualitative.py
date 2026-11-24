import asyncio

import pytest
from conftest import RecordingLLM, make_llm

from askusers import qualitative as Q
from askusers.models import Question, Response


def resp(i, user, ans, tid="target-001"):
    return Response(id=f"response-{i:04d}", targetId=tid, userId=user, questionId="q1", status="completed", answer=ans)


Q1 = Question(id="q1", text="印象は？", purpose="p", phase="after_exposure", responseType="text", analysisMethod="qualitative")


def test_mention_rate_counts_users_once_and_states_denominator():
    rs = [resp(1, "u1", "a"), resp(2, "u1", "b"), resp(3, "u2", "c"),
          Response(id="response-0004", targetId="target-001", userId="u3", questionId="q1", status="failed", error="x")]
    # u1 の 2 回答が同じテーマに該当しても 1 人として数える。失敗回答は分母から除外
    s = Q.mention_stats(["target-001"], rs, {"response-0001", "response-0002"})[0]
    assert (s.userCount, s.denominator, s.rate) == (1, 2, 0.5) and s.userIds == ["u1"]


def test_themes_computed_by_program_from_classification():
    rs = [resp(i, f"user-{i}", f"必要性を感じない {i}" if i % 2 else f"価格が高い 精度 {i}") for i in range(1, 7)]
    errors = []
    themes = asyncio.run(Q.analyze_question(make_llm(), Q1, rs, ["target-001"], 1, errors))
    assert [t.id for t in themes] == [f"theme-{i:03d}" for i in range(1, len(themes) + 1)]  # 一意 ID
    by_label = {t.label: t for t in themes}
    t = by_label["そもそも必要性を感じない"]
    assert t.stats[0].userCount == 3 and t.stats[0].denominator == 6 and t.stats[0].rate == 0.5
    for r in rs:  # 回答側の themeIds は theme.stats と一致
        for th in themes:
            assert (th.id in r.themeIds) == (r.id in th.stats[0].responseIds)
    assert not errors


def test_unknown_theme_ids_and_foreign_response_ids_from_llm_are_ignored():
    class Bad(RecordingLLM):
        def _classify(self, user):
            return {"assignments": [{"responseId": "response-0001", "themeIds": ["c1", "c99"]},
                                    {"responseId": "response-7777", "themeIds": ["c1"]}]}
    rs = [resp(1, "user-1", "x"), resp(2, "user-2", "y")]
    themes = asyncio.run(Q.analyze_question(make_llm(Bad()), Q1, rs, ["target-001"], 1, []))
    t = themes[0]
    assert t.stats[0].responseIds == ["response-0001"] and rs[1].themeIds == []  # 未分類の回答は themeIds 空のまま


def test_classification_batch_failure_is_recorded_not_hidden():
    from askusers.llm import LLMError
    llm = make_llm(RecordingLLM())
    llm.raw.fail = lambda task, user: LLMError("fatal", "x") if task == "classify" else None
    errors = []
    rs = [resp(i, f"user-{i}", "必要性") for i in range(1, 4)]
    themes = asyncio.run(Q.analyze_question(llm, Q1, rs, ["target-001"], 1, errors))
    assert errors and all(r.themeIds == [] for r in rs) and all(t.stats[0].userCount == 0 for t in themes)


def test_codebook_failure_returns_no_themes_and_records_error():
    from askusers.llm import LLMError
    llm = make_llm(RecordingLLM())
    llm.raw.fail = lambda task, user: LLMError("fatal", "x") if task == "codebook" else None
    errors = []
    assert asyncio.run(Q.analyze_question(llm, Q1, [resp(1, "u", "a")], ["target-001"], 1, errors)) == []
    assert errors[0].stage == "qualitative"


def test_unclassified_responses_are_listed(survey_run):
    q = next(s for s in survey_run.analysis.questions if s.method == "qualitative")
    ids = q.perTarget[0].unclassifiedResponseIds
    rs = {r.id: r for r in survey_run.responses}
    assert all(not rs[i].themeIds and rs[i].status == "completed" for i in ids)


@pytest.mark.parametrize("n", [1, 7])
def test_denominator_is_valid_users_per_question(survey_run, n):
    for th in survey_run.analysis.themes:
        valid = {r.userId for r in survey_run.responses if r.questionId == th.questionId and r.status == "completed"
                 and r.targetId == th.stats[0].targetId}
        assert th.stats[0].denominator == len(valid)
