"""Survey Runner: ペルソナごとに独立した LLM コンテキストでアンケートに回答させる。

- 提示前の質問（before_exposure）は評価対象を見せずに 1 回だけ回答させる（compare では全対象で共有）。
- 提示後の質問は評価対象ごとに別コンテキストで回答させる。他ユーザー・他対象の回答は参照しない。
- 失敗は隠さず status=failed で保存する。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from . import prompts as P
from .llm import LLM, LLMError
from .models import ExecError, Question, Response, Survey, Target, User

SEMANTIC_RETRIES = 2


@dataclass
class RunnerOutput:
    # (userId, targetId|None, questionId) -> Response（id は後で採番）
    responses: dict[tuple[str, str | None, str], Response] = field(default_factory=dict)
    errors: list[ExecError] = field(default_factory=list)


def check_answer(q: Question, ans) -> tuple[str, object]:
    """('ok'|'unanswered'|'invalid', 正規化した回答)"""
    if ans is None or (isinstance(ans, str) and not ans.strip()) or ans == []:
        return "unanswered", None
    if q.responseType == "text":
        return ("ok", ans.strip()) if isinstance(ans, str) else ("invalid", None)
    if q.responseType == "single_choice":
        return ("ok", ans) if isinstance(ans, str) and ans in q.options else ("invalid", None)
    if isinstance(ans, list) and all(isinstance(a, str) and a in q.options for a in ans):
        return "ok", list(dict.fromkeys(ans))
    return "invalid", None


async def _ask(llm: LLM, card: str, qs: list[Question], target: Target | None, prior: str,
               ) -> tuple[dict[str, tuple[str, object]], int, str | None]:
    """質問群への回答を取得。{qid: (status, answer)}, 試行回数, 失敗理由。"""
    parts = [f"Your profile:\n{card}"]
    if prior:
        parts.append(f"Earlier you already answered (before seeing anything):\n{prior}")
    if target is not None:
        parts.append("You are now shown the following document. Answer based only on what it contains.\n"
                     + P.fence_target(target))
    parts.append("Answer these questions:\n" + P.format_questions(qs))
    user = "\n\n".join(parts)
    result: dict[str, tuple[str, object]] = {}
    attempts = 0
    reason = None
    for _ in range(SEMANTIC_RETRIES + 1):
        try:
            data, a = await llm.structured(system=P.SYSTEM["answer"], user=user,
                                           schema=P.ANSWER_SCHEMA, temperature=0.7)
        except LLMError as e:
            return result, attempts + e.attempts, f"{e.kind}: {e}"
        attempts += a
        given = {x["questionId"]: x["answer"] for x in data["answers"]}
        bad = []
        for q in qs:
            if q.id in result and result[q.id][0] != "invalid":
                continue
            if q.id not in given:
                result[q.id] = ("invalid", None)
                bad.append(q.id)
                continue
            result[q.id] = check_answer(q, given[q.id])
            if result[q.id][0] == "invalid":
                bad.append(q.id)
        if not bad:
            return result, attempts, None
        reason = f"invalid_output: 回答が欠落または不正: {', '.join(bad)}"
    return result, attempts, reason


async def run_survey(
    llm: LLM, survey: Survey, users: list[User], targets: list[Target], labels: dict[str, str],
    on_user_done: Callable[[str, bool], None] | None = None,
) -> RunnerOutput:
    out = RunnerOutput()
    before = [q for q in survey.questions if q.phase == "before_exposure"]
    after = [q for q in survey.questions if q.phase == "after_exposure"]
    lock = asyncio.Lock()

    def record(u: User, tid: str | None, qs: list[Question], res, attempts: int, reason):
        for q in qs:
            status, ans = res.get(q.id, ("invalid", None))
            if status == "ok":
                r = Response(id="", targetId=tid, userId=u.id, questionId=q.id, status="completed",
                             answer=ans, attempts=attempts)  # type: ignore[arg-type]
            elif status == "unanswered":
                r = Response(id="", targetId=tid, userId=u.id, questionId=q.id,
                             status="unanswered", attempts=attempts)
            else:
                msg = reason or "回答を取得できませんでした"
                r = Response(id="", targetId=tid, userId=u.id, questionId=q.id, status="failed",
                             error=msg, attempts=attempts)
                out.errors.append(ExecError(stage="survey", message=msg, userId=u.id,
                                            questionId=q.id, targetId=tid, attempts=attempts))
            out.responses[(u.id, tid, q.id)] = r

    async def one_user(u: User) -> None:
        card = P.persona_card(u, labels)
        prior = ""
        ok = True
        if before:
            res, att, reason = await _ask(llm, card, before, None, "")
            record(u, None, before, res, att, reason)
            ok &= reason is None
            prior = "\n".join(f"- {q.text}: {res[q.id][1]}" for q in before
                              if res.get(q.id, ("", None))[0] == "ok")
        if after:
            async def per_target(t: Target):
                r, a, why = await _ask(llm, card, after, t, prior)
                return t, r, a, why
            for t, r, a, why in await asyncio.gather(*(per_target(t) for t in targets)):
                record(u, t.id, after, r, a, why)
                ok &= why is None
        async with lock:
            if on_user_done:
                on_user_done(u.id, ok)

    await asyncio.gather(*(one_user(u) for u in users))
    return out
