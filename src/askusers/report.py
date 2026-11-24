"""Report Analyzer: 発見事項・改善提案の生成。数値はプログラム集計を渡し、引用は実在の回答だけ許す。"""

from __future__ import annotations

from . import prompts as P
from .llm import LLM, LLMError
from .models import ExecError, Finding, Recommendation, SurveyData
from .qualitative import mention_stats


def digest(d: SurveyData) -> str:
    qmap = {q.id: q for q in d.survey.questions}
    tname = {t.id: f"{t.role or 'target'} ({t.source})" for t in d.targets}
    lines = [f"Mode: {d.mode}", f"Audience: {d.research.audience}", f"Objective: {d.research.objective}"]
    for t in d.targets:
        lines.append(f"Target {t.id}: {tname[t.id]}")
    lines.append("\n## Questions and aggregates")
    for s in d.analysis.questions:
        q = qmap[s.questionId]
        lines.append(f"[{q.id}] {q.text} (method={q.analysisMethod}, phase={q.phase}; purpose: {q.purpose})")
        for p in s.perTarget:
            head = f"  target={p.targetId} validN={p.validN} failed={p.failedN} unanswered={p.unansweredN}"
            if p.ssr and p.ssr.meanScore is not None:
                head += f" SSR mean={p.ssr.meanScore:.2f} dist={[round(x, 2) for x in p.ssr.distribution or []]}"
            if p.choices:
                head += " choices=" + ", ".join(f"{c.option}:{c.count}" for c in p.choices)
            lines.append(head)
    lines.append("\n## Themes (mention counts are computed by the program)")
    rmap = {r.id: r for r in d.responses}
    for t in d.analysis.themes:
        st = "; ".join(f"{s.targetId}: {s.userCount}/{s.denominator} ({s.rate:.0%})" for s in t.stats)
        lines.append(f"{t.id} [{t.questionId}] ({t.valence}) {t.label}: {t.description} -> {st}")
        shown = 0
        for rid in t.stats[-1].responseIds if t.stats else []:
            r = rmap[rid]
            if isinstance(r.answer, str) and shown < 3:
                lines.append(f"    {rid} ({r.userId}): {r.answer}")
                shown += 1
    if d.analysis.comparison:
        c = d.analysis.comparison
        lines.append(f"\n## Comparison (before={c.beforeTargetId}, after={c.afterTargetId}, paired users={c.pairedUserCount})")
        lines += [f"improved: {x}" for x in c.improved] + [f"not improved: {x}" for x in c.notImproved]
        lines += [f"new issue: {x}" for x in c.newIssues]
    lines.append("\n## SSR/qualitative answers of SSR questions (sample, for evidence ids)")
    for q in d.survey.questions:
        if q.analysisMethod == "ssr":
            for r in [x for x in d.responses if x.questionId == q.id and x.status == "completed"][:12]:
                lines.append(f"    {r.id} ({r.userId}, {r.targetId}) [{q.id}]: {r.answer}")
    return "\n".join(lines)


async def generate_report(llm: LLM, d: SurveyData, errors: list[ExecError], warnings: list[str]) -> None:
    """d.analysis の conclusion/summary/findings/recommendations を埋める。"""
    if d.mode == "compare":
        extra = ("\nThis is a before/after comparison of two versions evaluated by the same synthetic "
                 "respondents. Differences are differences in synthetic evaluation and do not guarantee "
                 "real-world improvement. Cover: what improved, what did not, new issues, next steps.")
    else:
        extra = ""
    try:
        data, _ = await llm.structured(system=P.SYSTEM["report"] + extra, user=digest(d),
                                       schema=P.REPORT_SCHEMA, temperature=0.3, max_tokens=8192)
    except LLMError as e:
        errors.append(ExecError(stage="report", message=f"レポート生成失敗: {e}", attempts=e.attempts))
        d.analysis.conclusion = "（レポートの生成に失敗しました。集計結果は survey.html / data.json を参照）"
        d.analysis.summary = d.analysis.conclusion
        return
    rmap = {r.id: r for r in d.responses}
    theme_ids = {t.id for t in d.analysis.themes}
    qids = {q.id for q in d.survey.questions}
    findings: list[Finding] = []
    index_map: dict[int, str] = {}
    for i, f in enumerate(data["findings"]):
        ev = [x for x in f["evidenceResponseIds"]
              if x in rmap and rmap[x].status == "completed" and isinstance(rmap[x].answer, str)]
        dropped = len(f["evidenceResponseIds"]) - len(ev)
        if dropped:
            warnings.append(f"発見事項「{f['title'][:20]}」: 存在しない/引用不可の回答ID {dropped} 件を除外")
        themes = [t for t in f.get("themeIds", []) if t in theme_ids]
        fq = [x for x in f["questionIds"] if x in qids]
        if not fq:
            warnings.append(f"発見事項「{f['title'][:20]}」: 根拠の質問IDが無いため除外")
            continue
        hit = {rid for t in d.analysis.themes if t.id in themes for s in t.stats for rid in s.responseIds}
        stats = []
        if themes:
            qr = [r for r in d.responses if r.questionId in {t.questionId for t in d.analysis.themes
                                                              if t.id in themes}]
            first = next(t for t in d.analysis.themes if t.id == themes[0])
            stats = mention_stats([s.targetId for s in first.stats], qr, hit)
        fid = f"finding-{len(findings) + 1:03d}"
        index_map[i] = fid
        findings.append(Finding(id=fid, title=f["title"], what=f["what"], why=f["why"],
                                themeIds=themes, questionIds=fq, evidenceResponseIds=ev, stats=stats))
    recs: list[Recommendation] = []
    for r in data["recommendations"]:
        fids = [index_map[i] for i in r["findingIndexes"] if i in index_map]
        if not fids:
            warnings.append(f"改善提案「{r['title'][:20]}」: 根拠の発見事項が無いため除外")
            continue
        recs.append(Recommendation(
            id=f"rec-{len(recs) + 1:03d}", priority=r["priority"], title=r["title"], what=r["what"],
            why=r["why"], how=r["how"], findingIds=fids, location=r.get("location") or None))
    d.analysis.findings, d.analysis.recommendations = findings, recs
    d.analysis.conclusion, d.analysis.summary = data["conclusion"].strip(), data["summary"].strip()
