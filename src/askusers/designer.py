"""Survey Designer: アンケートと評価基準（SSR 参照文章セット）の設計。"""

from __future__ import annotations

from dataclasses import dataclass

from . import prompts as P
from .llm import LLM, LLMError
from .models import AnchorSet, Question, Survey, Target

DEFAULT_OBJECTIVE = "総合的な評価と改善点"
MIN_Q, MAX_Q = 3, 12


class DesignError(Exception):
    pass


@dataclass
class Design:
    survey: Survey
    audience: str


def validate_questions(raw: list[dict], allow_ssr: bool) -> list[str]:
    problems: list[str] = []
    if not (MIN_Q <= len(raw) <= MAX_Q):
        problems.append(f"質問数が {len(raw)} 問です（{MIN_Q}〜{MAX_Q}問にする）")
    seen_after = False
    for i, q in enumerate(raw, 1):
        tag = f"質問{i}"
        if q["phase"] == "after_exposure":
            seen_after = True
        elif seen_after:
            problems.append(f"{tag}: before_exposure の質問は after_exposure より前に置く")
        m, t = q["analysisMethod"], q["responseType"]
        if m == "ssr":
            if not allow_ssr:
                problems.append(f"{tag}: この実行では ssr を使えない（qualitative/categorical にする）")
            sets = q.get("anchorSets") or []
            if t != "text":
                problems.append(f"{tag}: ssr は responseType=text")
            if len(sets) != 3 or any(len(s) != 5 or not all(x.strip() for x in s) for s in sets):
                problems.append(f"{tag}: ssr には 5 文ずつの anchorSets がちょうど 3 つ必要")
            if not q.get("scaleDescription"):
                problems.append(f"{tag}: ssr には scaleDescription が必要")
        elif m == "categorical":
            if t == "text" or len(q.get("options") or []) < 2:
                problems.append(f"{tag}: categorical は選択式で options が 2 つ以上必要")
        elif m == "qualitative" and t != "text":
            problems.append(f"{tag}: qualitative は responseType=text")
        if t != "text" and m != "categorical":
            problems.append(f"{tag}: 選択式は categorical にする")
    return problems


async def design_survey(
    llm: LLM, target: Target | list[Target], audience: str | None, objective: str,
    allow_ssr: bool = True,
) -> Design:
    targets = target if isinstance(target, list) else [target]
    docs = "\n".join(P.fence_target(t) for t in targets[:1])  # 設計は代表の1件で十分（比較時も同一の質問）
    base = (
        f"Audience: {audience or '(not specified: infer the most plausible audience and return it)'}\n"
        f"Objective: {objective}\n"
        f"SSR scoring available: {'yes' if allow_ssr else 'NO - do not use analysisMethod=ssr'}\n\n{docs}"
    )
    if len(targets) > 1:
        base += (
            "\n\nNOTE: the same questionnaire will be used to compare several versions of this kind of "
            "document. Keep questions neutral and not specific to wording of one version."
        )
    feedback = ""
    for _ in range(3):
        try:
            data, _a = await llm.structured(system=P.SYSTEM["design"], user=base + feedback,
                                            schema=P.DESIGN_SCHEMA, temperature=0.7)
        except LLMError as e:
            raise DesignError(f"アンケート設計に失敗しました: {e}") from e
        problems = validate_questions(data["questions"], allow_ssr)
        if not problems:
            break
        feedback = "\n\nFix these problems in your previous attempt:\n- " + "\n- ".join(problems)
    else:
        raise DesignError("アンケート設計が要件を満たしませんでした: " + "; ".join(problems))
    qs = []
    for i, q in enumerate(data["questions"], 1):
        qs.append(Question(
            id=f"q{i}", text=q["text"].strip(), purpose=q["purpose"].strip(), phase=q["phase"],
            responseType=q["responseType"], analysisMethod=q["analysisMethod"],
            options=[o.strip() for o in q.get("options") or []],
            anchorSets=[AnchorSet(id=f"q{i}-set{j}", statements=[x.strip() for x in s])
                        for j, s in enumerate(q.get("anchorSets") or [], 1)]
            if q["analysisMethod"] == "ssr" else [],
            scaleDescription=q.get("scaleDescription") if q["analysisMethod"] == "ssr" else None,
        ))
    return Design(Survey(title=data["title"].strip(), questions=qs),
                  (audience or data["audience"]).strip())
