"""Persona Generator: 調査対象者の定義だけから多様な合成ユーザーを生成する（評価対象は見せない）。"""

from __future__ import annotations

import asyncio
import math
import re

from . import prompts as P
from .llm import LLM, LLMError
from .models import AttributeDef, PersonaDesign, User

BATCH = 10


class PersonaError(Exception):
    pass


def _tokens(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s)
    return {s[i : i + 2] for i in range(len(s) - 1)} or {s}


def find_duplicates(users: list[User], threshold: float = 0.8) -> list[str]:
    """属性が完全一致、または背景の文字 2-gram の Jaccard 類似度が高いユーザーID（後の方）を返す。"""
    dups: list[str] = []
    sigs = [(u.id, tuple(sorted((k, str(v)) for k, v in u.attributes.items())), _tokens(u.background))
            for u in users]
    for i, (uid, sig, tok) in enumerate(sigs):
        for _j, (_, sig2, tok2) in enumerate(sigs[:i]):
            jac = len(tok & tok2) / max(1, len(tok | tok2))
            if sig == sig2 or jac >= threshold:
                dups.append(uid)
                break
    return dups


def _coerce(schema: list[AttributeDef], attrs: dict) -> tuple[dict, list[str]]:
    out, missing = {}, []
    for a in schema:
        v = attrs.get(a.key)
        if v is None:
            missing.append(a.key)
            out[a.key] = None
        elif a.type == "number":
            if isinstance(v, bool) or not isinstance(v, int | float):
                try:
                    v = float(str(v))
                    v = int(v) if v.is_integer() else v
                except ValueError:
                    missing.append(a.key)
                    v = None
            out[a.key] = v
        else:
            out[a.key] = str(v)
    return out, missing


async def design_personas(llm: LLM, audience: str, objective: str) -> PersonaDesign:
    user = f"Audience: {audience}\nObjective of the research: {objective}"
    try:
        data, _ = await llm.structured(system=P.SYSTEM["persona_schema"], user=user,
                                       schema=P.PERSONA_SCHEMA_SCHEMA, temperature=0.7)
    except LLMError as e:
        raise PersonaError(f"ペルソナ属性スキーマの設計に失敗: {e}") from e
    attrs = []
    seen = set()
    for a in data["attributeSchema"]:
        if a["key"] in seen:
            continue
        seen.add(a["key"])
        attrs.append(AttributeDef(key=a["key"], label=a["label"], type=a["type"],
                                  description=a.get("description", "")))
    if len(attrs) < 3 or not any(a.type == "category" for a in attrs):
        raise PersonaError("ペルソナ属性スキーマが不十分です（属性3つ以上・category を含む）")
    return PersonaDesign(attributeSchema=attrs, policy=data["policy"], segments=data["segments"])


async def _batch(llm: LLM, audience: str, objective: str, design: PersonaDesign, slots: list[int],
                 existing: list[str]) -> list[dict]:
    segs = design.segments or ["(general)"]
    assign = "\n".join(f"- slot {s + 1}: segment = {segs[s % len(segs)]}" for s in slots)
    schema_txt = P.dumps([a.model_dump() for a in design.attributeSchema])
    user = (
        f"Audience: {audience}\nObjective of the research (do NOT tailor respondents to it): {objective}\n"
        f"Policy: {design.policy}\nAttribute schema:\n{schema_txt}\n\n"
        f"Generate exactly {len(slots)} respondents, in this slot order:\n{assign}\n\n"
        "Already generated respondents (avoid duplicates):\n" + ("\n".join(existing[-40:]) or "(none)")
    )
    data, _ = await llm.structured(system=P.SYSTEM["personas"], user=user,
                                   schema=P.PERSONAS_SCHEMA, temperature=1.0)
    return data["users"]


def _summ(u: User) -> str:
    return "- " + ", ".join(f"{k}={v}" for k, v in u.attributes.items()) + f" | {u.background[:60]}"


async def generate_personas(
    llm: LLM, design: PersonaDesign, audience: str, objective: str, count: int, warnings: list[str],
    on_progress=None,
) -> list[User]:
    slots = list(range(count))
    chunks = [slots[i : i + BATCH] for i in range(0, count, BATCH)]
    results = await asyncio.gather(
        *(_batch(llm, audience, objective, design, c, []) for c in chunks), return_exceptions=True
    )
    raw: list[dict] = []
    for c, r in zip(chunks, results, strict=True):
        if isinstance(r, Exception):
            if isinstance(r, LLMError) and r.kind == "budget":
                raise r
            warnings.append(f"ペルソナ生成バッチ失敗 (slots {c[0] + 1}-{c[-1] + 1}): {r}")
            continue
        raw.extend(r[: len(c)])
        if len(r) < len(c):
            warnings.append(f"ペルソナが {len(c)} 人中 {len(r)} 人しか返りませんでした")
    users = _build(design, raw, warnings)

    # 重複の検出 → 1 回だけ補充生成
    dups = set(find_duplicates(users))
    if dups:
        keep = [u for u in users if u.id not in dups]
        try:
            extra = await _batch(llm, audience, objective, design, list(range(len(dups))),
                                 [_summ(u) for u in keep])
            keep_raw = [u.model_dump() for u in keep]
            users = _build(design, [{k: v for k, v in r.items() if k != "id"} for r in keep_raw] + extra,
                           warnings)
        except LLMError as e:
            warnings.append(f"重複ペルソナ {len(dups)} 件の補充生成に失敗: {e}")
        remaining = find_duplicates(users)
        if remaining:
            warnings.append(f"類似ペルソナが {len(remaining)} 件残っています: {', '.join(remaining)}")
    if not users:
        raise PersonaError("ペルソナを1人も生成できませんでした")
    return users[:count]


def _build(design: PersonaDesign, raw: list[dict], warnings: list[str]) -> list[User]:
    users = []
    for i, r in enumerate(raw, 1):
        attrs, missing = _coerce(design.attributeSchema, r["attributes"])
        uid = f"user-{i:0{max(3, int(math.log10(max(len(raw), 1))) + 1)}d}"
        if missing:
            warnings.append(f"{uid}: 属性が欠落/不正のため空にしました: {', '.join(missing)}")
        users.append(User(
            id=uid, attributes=attrs, background=r["background"].strip(),
            goals=r.get("goals", []), constraints=r.get("constraints", []),
            currentSolutions=r.get("currentSolutions", []), values=r.get("values", []),
        ))
    return users
