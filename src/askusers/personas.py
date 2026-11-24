"""Persona Generator: 調査対象者の定義だけから多様な合成ユーザーを生成する（評価対象は見せない）。"""

from __future__ import annotations

import asyncio
import math
import re

from . import prompts as P
from .llm import LLM, LLMError
from .models import AttributeDef, PersonaDesign, User
from .names import clean_name, find_name_problems

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
    name = f"{u.displayName}: " if u.displayName else ""
    return "- " + name + ", ".join(f"{k}={v}" for k, v in u.attributes.items()) + f" | {u.background[:60]}"


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
            keep_raw = [{**u.model_dump(), "name": u.displayName} for u in keep]
            users = _build(design, [{k: v for k, v in r.items() if k != "id"} for r in keep_raw] + extra,
                           warnings)
        except LLMError as e:
            warnings.append(f"重複ペルソナ {len(dups)} 件の補充生成に失敗: {e}")
        remaining = find_duplicates(users)
        if remaining:
            warnings.append(f"類似ペルソナが {len(remaining)} 件残っています: {', '.join(remaining)}")
    if not users:
        raise PersonaError("ペルソナを1人も生成できませんでした")
    users = users[:count]
    await ensure_names(llm, design, audience, users, warnings)
    return users


async def ensure_names(llm: LLM, design: PersonaDesign, audience: str, users: list[User],
                       warnings: list[str], rounds: int = 2) -> None:
    """氏名の欠落・重複・名字の偏りを検査し、該当者だけ氏名を付け直す。直らなければ氏名なし（ID 表示）。"""
    for _ in range(rounds):
        redo = find_name_problems(users)
        if not redo:
            return
        targets = [u for u in users if u.id in set(redo)]
        kept = [u for u in users if u.id not in set(redo) and u.displayName]
        used = "\n".join(f"- {u.displayName}" for u in kept) or "(none)"
        for u in targets:
            u.displayName = None
        cards = "\n".join(f"- id: {u.id} | {_summ(u)[2:]}" for u in targets)
        prompt = f"Audience: {audience}\n\nRespondents needing a name:\n{cards}\n\nNames already used:\n{used}"
        try:
            data, _ = await llm.structured(system=P.SYSTEM["names"], user=prompt,
                                           schema=P.NAMES_SCHEMA, temperature=1.0)
        except LLMError as e:
            if e.kind == "budget":
                raise
            warnings.append(f"氏名の付け直しに失敗: {e}")
            break
        by_id = {u.id: u for u in targets}
        for r in data["names"]:
            if r.get("id") in by_id:
                by_id[r["id"]].displayName = clean_name(r.get("name"))
    # 付け直しても残った問題: 重複は氏名なしにして ID 表示へ戻す（同名の別人に見えるより安全）
    for uid in find_name_problems(users, check_surname=False):
        next(x for x in users if x.id == uid).displayName = None
        warnings.append(f"{uid}: 氏名を付与できませんでした（欠落・重複）。ID 表示になります")
    if skew := find_name_problems(users):
        warnings.append(f"名字が偏っています（{len(skew)} 人）: {', '.join(skew)}")


def _build(design: PersonaDesign, raw: list[dict], warnings: list[str]) -> list[User]:
    users = []
    for i, r in enumerate(raw, 1):
        attrs, missing = _coerce(design.attributeSchema, r["attributes"])
        uid = f"user-{i:0{max(3, int(math.log10(max(len(raw), 1))) + 1)}d}"
        if missing:
            warnings.append(f"{uid}: 属性が欠落/不正のため空にしました: {', '.join(missing)}")
        users.append(User(
            id=uid, displayName=clean_name(r.get("name")), attributes=attrs, background=r["background"].strip(),
            goals=r.get("goals", []), constraints=r.get("constraints", []),
            currentSolutions=r.get("currentSolutions", []), values=r.get("values", []),
        ))
    return users
