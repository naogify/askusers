"""CSV Exporter: data.json から 1 行 = 1 ユーザー（compare は 1 ユーザー × 1 評価対象）の CSV を作る。"""

from __future__ import annotations

import csv
import io
import json

from .models import Response, SurveyData

DANGEROUS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(v):
    """CSV インジェクション対策。文字列のみ先頭に ' を付ける。数値はそのまま。元データ(JSON)は変更しない。"""
    if isinstance(v, str) and v.startswith(DANGEROUS):
        return "'" + v
    return v


def _cell_value(v):
    if isinstance(v, list):
        return json.dumps(v, ensure_ascii=False)
    return v


def build_rows(d: SurveyData) -> tuple[list[str], list[list]]:
    qs = d.survey.questions
    attr_keys = [a.key for a in d.personaDesign.attributeSchema]
    compare = d.mode == "compare"
    header = ["user_id", "display_name"] + (["target_id", "variant"] if compare else [])
    header += ["status"] + attr_keys
    header += ["background", "goals", "constraints", "current_solutions", "values",
               "failed_questions", "unanswered_questions"]
    theme_label = {t.id: t.label for t in d.analysis.themes}
    for q in qs:
        qn = q.id.upper()
        header.append(f"{qn}.{q.text}")
        if q.analysisMethod == "ssr":
            header += [f"{qn}.expected_score"] + [f"{qn}.p{i}" for i in range(1, 6)]
        elif q.analysisMethod == "qualitative":
            header.append(f"{qn}.themes")
    rmap: dict[tuple[str, str | None, str], Response] = {
        (r.userId, r.targetId, r.questionId): r for r in d.responses}
    rows = []
    targets = d.targets if compare else d.targets[:1]
    for u in d.users:
        for t in targets:
            row: list = [u.id, u.displayName or ""]
            if compare:
                row += [t.id, t.role or ""]
            mine = [rmap.get((u.id, None if q.phase == "before_exposure" else t.id, q.id)) for q in qs]
            failed = [q.id for q, r in zip(qs, mine, strict=True) if r is not None and r.status == "failed"]
            unans = [q.id for q, r in zip(qs, mine, strict=True) if r is not None and r.status == "unanswered"]
            missing = [q.id for q, r in zip(qs, mine, strict=True) if r is None]
            ok = [r for r in mine if r is not None and r.status == "completed"]
            status = ("failed" if not ok and (failed or missing) else
                      "partial" if failed or missing else "completed")
            row.append(status)
            row += [_cell_value(u.attributes.get(k)) for k in attr_keys]
            row += [u.background, json.dumps(u.goals, ensure_ascii=False),
                    json.dumps(u.constraints, ensure_ascii=False),
                    json.dumps(u.currentSolutions, ensure_ascii=False),
                    json.dumps(u.values, ensure_ascii=False),
                    json.dumps(failed + missing), json.dumps(unans)]
            for q, r in zip(qs, mine, strict=True):
                row.append(_cell_value(r.answer) if r is not None and r.status == "completed" else "")
                if q.analysisMethod == "ssr":
                    if r is not None and r.ssr is not None:
                        row += [round(r.ssr.expectedScore, 6)] + [round(p, 6) for p in r.ssr.distribution]
                    else:
                        row += [""] * 6
                elif q.analysisMethod == "qualitative":
                    row.append(json.dumps([theme_label[x] for x in r.themeIds], ensure_ascii=False)
                               if r is not None and r.themeIds else "")
            rows.append(row)
    return header, rows


def render_csv(d: SurveyData) -> bytes:
    header, rows = build_rows(d)
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow([safe_cell(h) for h in header])
    for r in rows:
        w.writerow([safe_cell(c) if c is not None else "" for c in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")
