"""Markdown Renderer: data.json から report.md を作る。LLM への追加問い合わせはしない。"""

from __future__ import annotations

import re

from .models import Finding, SurveyData

PRIORITY = {"high": "高", "medium": "中", "low": "低"}


def md_escape(text: str) -> str:
    """回答・対象由来の文字列を Markdown に埋め込む際の無害化（HTML/リンク/見出し/表の記法を無効化）。"""
    t = text.replace("\\", "\\\\")
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = re.sub(r"([\[\]`*_|])", r"\\\1", t)
    t = re.sub(r"^(\s*)([#>\-+]|\d+\.)", r"\1\\\2", t, flags=re.M)
    return t.replace("\r", "").strip()


def _one_line(text: str) -> str:
    return " ".join(md_escape(text).split())


def _quote(text: str) -> str:
    return "\n".join("> " + line for line in md_escape(text).split("\n"))


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _target_name(d: SurveyData, tid: str | None) -> str:
    if tid is None:
        return "全体"
    t = next(t for t in d.targets if t.id == tid)
    return {"before": "Before", "after": "After"}.get(t.role or "", t.source)


def _finding_stat(d: SurveyData, f: Finding) -> str:
    if not f.stats:
        return ""
    if d.mode == "compare":
        return " / ".join(f"{_target_name(d, s.targetId)}: {s.userCount}人 / {s.denominator}人 ({_pct(s.rate)})"
                          for s in f.stats)
    s = f.stats[0]
    return f"{s.userCount}人 / {s.denominator}人（言及率 {_pct(s.rate)}）"


def render_markdown(d: SurveyData) -> str:
    qmap = {q.id: q for q in d.survey.questions}
    rmap = {r.id: r for r in d.responses}
    a = d.analysis
    src = " vs ".join(md_escape(t.source) for t in d.targets)
    out = [f"# {md_escape(d.survey.title)} 調査レポート", ""]
    out.append(f"対象：{src}  ")
    out.append(f"調査対象者：{md_escape(d.research.audience)}"
               + ("（AIによる推定）" if d.research.audienceInferred else "") + "  ")
    out.append(f"回答者：合成ユーザー{d.research.completedUserCount}人（依頼 {d.research.requestedUserCount}人）  ")
    out.append(f"調査目的：{md_escape(d.research.objective)}"
               + ("（既定値）" if d.research.objectiveInferred else "") + "  ")
    out.append(f"実行ID：{md_escape(d.runId)}")
    if d.status != "completed":
        out += ["", f"> **注意: この調査は不完全です（status: {d.status}）。失敗した回答があります。"
                    "詳細は data.json の execution.errors を参照してください。**"]
    out += ["", "## 1. 結論", "", md_escape(a.conclusion) or "（なし）", ""]

    # 主要スコア（SSR 対象の質問のみ）
    ssr_qs = [s for s in a.questions if s.method == "ssr"]
    if ssr_qs and d.mode == "survey":
        for s in ssr_qs:
            p = s.perTarget[0]
            if p.ssr and p.ssr.meanScore is not None:
                out.append(f"- {md_escape(qmap[s.questionId].text)}（{s.questionId.upper()}）：**{p.ssr.meanScore:.1f} / 5**"
                           f"（SSR 期待スコアの平均、有効回答 {p.ssr.validN}件）")
        out.append("")
    if d.mode == "compare" and a.comparison:
        c = a.comparison
        rows = []
        for qc in c.questions:
            if qc.meanBefore is None or qc.meanAfter is None:
                continue
            pd = f"{qc.pairedMeanDelta:+.2f} ({qc.pairedN})" if qc.pairedMeanDelta is not None else "-"
            rows.append(f"| {_one_line(qmap[qc.questionId].text)}（{qc.questionId.upper()}） | {qc.meanBefore:.2f} | "
                        f"{qc.meanAfter:.2f} | {qc.meanDelta:+.2f} | {qc.validBefore}/{qc.validAfter} | {pd} |")
        if rows:  # SSR 対象の質問が無い（--no-ssr 等）ときは空の表を出さない
            out += ["| 指標 | Before | After | 差分 | 有効回答 B/A | 対応のある差分 (n) |", "|---|---|---|---|---|---|"]
            out += rows + ["", "平均スコアは各対象の有効回答すべての平均。「対応のある差分」は両対象に有効回答がある同一ユーザーだけの差分の平均。", ""]
        for title, items in (("改善された点", c.improved), ("改善されなかった点", c.notImproved),
                             ("新たに発生した課題", c.newIssues)):
            if items:
                out += [f"**{title}**", ""] + [f"- {md_escape(x)}" for x in items] + [""]
        if c.themes:
            out += ["| 課題・テーマ | Before | After | 判定 |", "|---|---|---|---|"]
            tmap = {t.id: t for t in a.themes}
            for tc in c.themes:
                out.append(f"| {_one_line(tmap[tc.themeId].label)}（{tc.themeId}） | {_pct(tc.rateBefore)} | "
                           f"{_pct(tc.rateAfter)} | {tc.verdict} |")
            out.append("")

    out += ["## 2. 発見事項", ""]
    if not a.findings:
        out += ["（発見事項は生成されませんでした）", ""]
    for f in a.findings:
        out += [f"### {_one_line(f.title)}", "", md_escape(f.what), ""]
        out += [f"背景・理由（分析者の解釈）：{md_escape(f.why)}", ""]
        st = _finding_stat(d, f)
        if st:
            out += [f"該当ユーザー数・言及率：{st}", ""]
        for rid in f.evidenceResponseIds:
            r = rmap[rid]
            out += [_quote(str(r.answer)), f"— {r.userId}（{r.id}、{r.questionId.upper()}）", ""]
        refs = ", ".join(x.upper() for x in f.questionIds)
        if f.themeIds:
            refs += "、" + ", ".join(f.themeIds)
        refs += ("、" + ", ".join(f.evidenceResponseIds)) if f.evidenceResponseIds else ""
        out += [f"根拠：{refs}", ""]

    out += ["## 3. 改善提案", ""]
    if a.recommendations:
        out += ["| 優先度 | 改善内容 | 根拠 |", "|---|---|---|"]
        for r in sorted(a.recommendations, key=lambda x: ["high", "medium", "low"].index(x.priority)):
            out.append(f"| {PRIORITY[r.priority]} | {_one_line(r.title)} | {', '.join(r.findingIds)} |")
        out.append("")
        for r in a.recommendations:
            out += [f"### {r.id}: {_one_line(r.title)}（優先度: {PRIORITY[r.priority]}）", "",
                    f"- 何を：{md_escape(r.what)}", f"- なぜ：{md_escape(r.why)}",
                    f"- どのように：{md_escape(r.how)}"]
            if r.location:
                out.append(f"- 該当箇所：{md_escape(r.location)}")
            out += [f"- 根拠となる発見事項：{', '.join(r.findingIds)}", ""]
    else:
        out += ["（改善提案は生成されませんでした）", ""]

    out += ["詳細な回答：survey.html（個別回答は responses.csv / data.json）", "", "---", ""]
    out += [f"- {md_escape(x)}" for x in d.limitations[:1]]
    if d.mode == "compare":
        out.append(f"- {md_escape(d.limitations[-1])}")
    return "\n".join(out).rstrip() + "\n"
