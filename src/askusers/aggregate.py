"""質問ごとの集計と改善前後の比較（すべて data.json の内容からプログラムで計算。LLM 不使用）。"""

from __future__ import annotations

import numpy as np

from . import ssr
from .models import (
    Breakdown,
    BreakdownGroup,
    ChoiceCount,
    Comparison,
    Question,
    QuestionComparison,
    QuestionSummary,
    QuestionTargetSummary,
    Response,
    SSRSummary,
    SurveyData,
    ThemeComparison,
)

ISSUE_VALENCES = {"negative", "request"}
THEME_DELTA = 0.10  # 言及率の差が 10 ポイント以上なら変化ありとみなす
SCORE_DELTA = 0.2  # 平均スコアの差が 0.2 以上なら改善/悪化とみなす（目安であり統計的検定ではない）


def target_ids_for(d: SurveyData, q: Question) -> list[str | None]:
    return [None] if q.phase == "before_exposure" else [t.id for t in d.targets]


def _breakdowns(d: SurveyData, rs: list[Response]) -> list[Breakdown]:
    users = {u.id: u for u in d.users}
    out = []
    for a in d.personaDesign.attributeSchema:
        if a.type != "category":
            continue
        groups: dict[str, list[float]] = {}
        for r in rs:
            if r.ssr is None:
                continue
            v = users[r.userId].attributes.get(a.key)
            if v is not None:
                groups.setdefault(str(v), []).append(r.ssr.expectedScore)
        if 2 <= len(groups) <= 12:
            out.append(Breakdown(attribute=a.key, groups=[
                BreakdownGroup(value=k, n=len(v), meanScore=float(np.mean(v)))
                for k, v in sorted(groups.items())]))
    return out


def summarize_question(d: SurveyData, q: Question) -> QuestionSummary:
    per = []
    for tid in target_ids_for(d, q):
        rs = [r for r in d.responses if r.questionId == q.id and r.targetId == tid]
        valid = [r for r in rs if r.status == "completed"]
        s = QuestionTargetSummary(
            targetId=tid, validN=len(valid),
            failedN=sum(r.status == "failed" for r in rs),
            unansweredN=sum(r.status == "unanswered" for r in rs))
        if q.analysisMethod == "ssr":
            scored = [r for r in valid if r.ssr is not None]
            if scored:
                pmf = ssr.get_survey_response_pmf(np.array([r.ssr.distribution for r in scored]))  # type: ignore[union-attr]
                s.ssr = SSRSummary(
                    meanScore=float(np.mean([r.ssr.expectedScore for r in scored])),  # type: ignore[union-attr]
                    distribution=[float(x) for x in pmf], validN=len(scored))
                s.breakdowns = _breakdowns(d, scored)
            else:
                s.ssr = SSRSummary(meanScore=None, distribution=None, validN=0)
        elif q.analysisMethod == "categorical":
            counts = {o: 0 for o in q.options}
            for r in valid:
                for a in ([r.answer] if isinstance(r.answer, str) else r.answer or []):
                    counts[a] = counts.get(a, 0) + 1
            n = len(valid)
            s.choices = [ChoiceCount(option=o, count=c, rate=(c / n if n else 0.0))
                         for o, c in counts.items()]
        else:
            s.unclassifiedResponseIds = sorted(r.id for r in valid if not r.themeIds)
        per.append(s)
    return QuestionSummary(questionId=q.id, method=q.analysisMethod, perTarget=per)


def build_comparison(d: SurveyData) -> Comparison:
    before = next((t for t in d.targets if t.role == "before"), d.targets[0])
    after = next((t for t in d.targets if t.role == "after"), d.targets[1])
    qsum = {s.questionId: s for s in d.analysis.questions}
    qcomps, improved, not_improved = [], [], []
    users_ok: set[str] = set()
    for q in d.survey.questions:
        if q.analysisMethod != "ssr":
            continue
        sb = next(p for p in qsum[q.id].perTarget if p.targetId == before.id)
        sa = next(p for p in qsum[q.id].perTarget if p.targetId == after.id)
        score = {(r.userId, r.targetId): r.ssr.expectedScore for r in d.responses
                 if r.questionId == q.id and r.ssr is not None}
        paired = sorted({u.id for u in d.users if (u.id, before.id) in score and (u.id, after.id) in score})
        users_ok |= set(paired)
        mb = sb.ssr.meanScore if sb.ssr else None
        ma = sa.ssr.meanScore if sa.ssr else None
        delta = (ma - mb) if (mb is not None and ma is not None) else None
        pdelta = float(np.mean([score[(u, after.id)] - score[(u, before.id)] for u in paired])) if paired else None
        qcomps.append(QuestionComparison(
            questionId=q.id, meanBefore=mb, meanAfter=ma, meanDelta=delta,
            distributionBefore=sb.ssr.distribution if sb.ssr else None,
            distributionAfter=sa.ssr.distribution if sa.ssr else None,
            validBefore=sb.ssr.validN if sb.ssr else 0, validAfter=sa.ssr.validN if sa.ssr else 0,
            pairedN=len(paired), pairedMeanDelta=pdelta))
        if delta is not None:
            (improved if delta >= SCORE_DELTA else not_improved).append(
                f"{q.id}: 平均スコア {mb:.2f} → {ma:.2f} ({delta:+.2f})")
    tcomps, new_issues = [], []
    for t in d.analysis.themes:
        sb = next((s for s in t.stats if s.targetId == before.id), None)
        sa = next((s for s in t.stats if s.targetId == after.id), None)
        if not sb or not sa:
            continue
        delta = sa.rate - sb.rate
        issue = t.valence in ISSUE_VALENCES
        if abs(delta) < THEME_DELTA:
            verdict = "unchanged"
        elif issue:
            verdict = ("new" if sb.userCount == 0 else "worsened") if delta > 0 else (
                "resolved" if sa.userCount == 0 else "improved")
        else:  # 肯定的/中立テーマは増えるほど良い
            verdict = "improved" if delta > 0 else "worsened"
        tcomps.append(ThemeComparison(themeId=t.id, rateBefore=sb.rate, rateAfter=sa.rate,
                                      delta=delta, verdict=verdict))
        label = f"{t.id}「{t.label}」 {sb.rate:.0%} → {sa.rate:.0%}"
        if verdict in ("improved", "resolved"):
            improved.append(label)
        elif verdict in ("worsened", "new"):
            (new_issues if verdict == "new" or sb.rate < 0.05 else not_improved).append(label)
        else:
            if issue and sa.rate >= THEME_DELTA:
                not_improved.append(label + "（変化なし）")
    return Comparison(
        beforeTargetId=before.id, afterTargetId=after.id,
        pairedUserCount=len(users_ok), questions=qcomps, themes=tcomps,
        improved=improved, notImproved=not_improved, newIssues=new_issues)
