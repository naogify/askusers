"""Qualitative Analyzer: 自由回答のテーマ分類。言及率は分類結果からプログラムで計算する。"""

from __future__ import annotations

import asyncio
import random

from . import prompts as P
from .llm import LLM, LLMError
from .models import ExecError, MentionStat, Question, Response, Theme

SAMPLE = 60
BATCH = 20


def mention_stats(target_ids: list[str | None], q_responses: list[Response],
                  theme_resp_ids: set[str]) -> list[MentionStat]:
    """テーマに該当する回答 ID 集合から、対象ごとの言及ユーザー数・率を計算する（ユーザー重複排除）。"""
    out = []
    for tid in target_ids:
        valid = [r for r in q_responses if r.targetId == tid and r.status == "completed"]
        denom = len({r.userId for r in valid})
        hit = [r for r in valid if r.id in theme_resp_ids]
        users = sorted({r.userId for r in hit})
        out.append(MentionStat(
            targetId=tid or "", userIds=users, responseIds=sorted(r.id for r in hit),
            userCount=len(users), denominator=denom, rate=(len(users) / denom if denom else 0.0)))
    return out


async def analyze_question(
    llm: LLM, q: Question, responses: list[Response], target_ids: list[str],
    next_theme_no: int, errors: list[ExecError], seed: int = 0,
) -> list[Theme]:
    """質問 q の共通コードブックを作り、全回答（全対象）を分類して Theme を返す。
    responses の themeIds は破壊的に更新する。"""
    valid = [r for r in responses if r.status == "completed" and isinstance(r.answer, str)]
    if not valid:
        return []
    rnd = random.Random(seed)
    sample = valid if len(valid) <= SAMPLE else rnd.sample(valid, SAMPLE)
    body = "\n".join(f"- {r.answer}" for r in sample)
    try:
        cb, _ = await llm.structured(
            system=P.SYSTEM["codebook"], temperature=0.3, schema=P.CODEBOOK_SCHEMA,
            user=f"Question: {q.text}\nPurpose: {q.purpose}\n\nAnswers:\n{body}")
    except LLMError as e:
        errors.append(ExecError(stage="qualitative", questionId=q.id, message=f"コードブック生成失敗: {e}"))
        return []
    book = cb["themes"][:10]
    local = {f"c{i}": t for i, t in enumerate(book, 1)}
    book_txt = "\n".join(f"{k}: {t['label']} — {t['description']}" for k, t in local.items())
    assign: dict[str, set[str]] = {}

    async def classify(chunk: list[Response]):
        ans = "\n".join(f"[{r.id}] {r.answer}" for r in chunk)
        data, _ = await llm.structured(
            system=P.SYSTEM["classify"], temperature=0.0, schema=P.CLASSIFY_SCHEMA,
            user=f"Question: {q.text}\n\nCodebook:\n{book_txt}\n\nAnswers:\n{ans}")
        ids = {r.id for r in chunk}
        for a in data["assignments"]:
            if a["responseId"] in ids:
                assign.setdefault(a["responseId"], set()).update(t for t in a["themeIds"] if t in local)

    results = await asyncio.gather(*(classify(valid[i : i + BATCH]) for i in range(0, len(valid), BATCH)),
                                   return_exceptions=True)
    failed_chunks = [i for i, r in enumerate(results) if isinstance(r, Exception)]
    classified_ids = set(assign)
    for i in failed_chunks:
        errors.append(ExecError(stage="qualitative", questionId=q.id,
                                message=f"分類バッチ失敗 ({i * BATCH + 1}件目〜): {results[i]}"))
    # 失敗バッチの回答は「分類されていない」ままにする（themeIds 空 + errors に記録）
    themes: list[Theme] = []
    by_id = {r.id: r for r in valid}
    for k, t in local.items():
        rids = {rid for rid, ths in assign.items() if k in ths}
        theme_id = f"theme-{next_theme_no + len(themes):03d}"
        for rid in rids:
            by_id[rid].themeIds.append(theme_id)
        themes.append(Theme(
            id=theme_id, questionId=q.id, label=t["label"].strip(), description=t["description"].strip(),
            valence=t["valence"], stats=mention_stats(target_ids, valid, rids)))
    _ = classified_ids
    return themes
