"""質問・ユーザー・回答・分析結果の参照整合性チェック。"""

from __future__ import annotations

from .models import SurveyData


def check_integrity(d: SurveyData) -> list[str]:
    problems: list[str] = []
    qids = [q.id for q in d.survey.questions]
    uids = [u.id for u in d.users]
    tids = [t.id for t in d.targets]
    rids = [r.id for r in d.responses]
    for name, ids in (("question", qids), ("user", uids), ("target", tids), ("response", rids)):
        if len(ids) != len(set(ids)):
            problems.append(f"{name} id が重複しています")
    qset, uset, tset, rset = set(qids), set(uids), set(tids), set(rids)
    qmap = {q.id: q for q in d.survey.questions}

    for q in d.survey.questions:
        if q.analysisMethod == "ssr" and not q.anchorSets:
            problems.append(f"{q.id}: ssr 質問に anchorSets がありません")
        if q.responseType != "text" and not q.options:
            problems.append(f"{q.id}: 選択式質問に options がありません")

    seen: set[tuple[str, str | None, str]] = set()
    for r in d.responses:
        if r.userId not in uset:
            problems.append(f"{r.id}: 未知の userId {r.userId}")
        if r.questionId not in qset:
            problems.append(f"{r.id}: 未知の questionId {r.questionId}")
            continue
        if r.targetId is not None and r.targetId not in tset:
            problems.append(f"{r.id}: 未知の targetId {r.targetId}")
        q = qmap[r.questionId]
        if q.phase == "before_exposure" and r.targetId is not None:
            problems.append(f"{r.id}: 提示前の質問に targetId が付いています")
        if q.phase == "after_exposure" and r.targetId is None:
            problems.append(f"{r.id}: 提示後の質問に targetId がありません")
        key = (r.userId, r.targetId, r.questionId)
        if key in seen:
            problems.append(f"{r.id}: (user, target, question) が重複しています")
        seen.add(key)
        if r.status != "completed" and r.ssr is not None:
            problems.append(f"{r.id}: 未完了の回答に ssr があります")
        if r.ssr is not None and q.analysisMethod != "ssr":
            problems.append(f"{r.id}: ssr 対象外の質問に ssr があります")

    theme_ids = {t.id for t in d.analysis.themes}
    for t in d.analysis.themes:
        if t.questionId not in qset:
            problems.append(f"{t.id}: 未知の questionId")
        for s in t.stats:
            if s.targetId not in tset:
                problems.append(f"{t.id}: 未知の targetId {s.targetId}")
            if not set(s.userIds) <= uset:
                problems.append(f"{t.id}: 未知の userId")
            if not set(s.responseIds) <= rset:
                problems.append(f"{t.id}: 未知の responseId")
            if s.userCount != len(set(s.userIds)):
                problems.append(f"{t.id}: userCount が userIds と一致しません")
    for r in d.responses:
        for tid in r.themeIds:
            if tid not in theme_ids:
                problems.append(f"{r.id}: 未知の themeId {tid}")
    for qs in d.analysis.questions:
        if qs.questionId not in qset:
            problems.append(f"analysis: 未知の questionId {qs.questionId}")
    fids = {f.id for f in d.analysis.findings}
    for f in d.analysis.findings:
        if not set(f.questionIds) <= qset:
            problems.append(f"{f.id}: 未知の questionId")
        if not set(f.themeIds) <= theme_ids:
            problems.append(f"{f.id}: 未知の themeId")
        if not set(f.evidenceResponseIds) <= rset:
            problems.append(f"{f.id}: 未知の evidenceResponseId")
    for rec in d.analysis.recommendations:
        if not set(rec.findingIds) <= fids:
            problems.append(f"{rec.id}: 未知の findingId")
    return problems
